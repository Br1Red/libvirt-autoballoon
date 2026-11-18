#!/usr/bin/env python3

import sys
import json
import argparse
import logging
import math
import xml.etree.ElementTree as ET
import libvirt

from time import monotonic

SZ_1MiB = 1024
SZ_256MiB = 256 * 1024
SZ_512MiB = 512 * 1024

class ExitFailure(Exception):
    pass

class LibVirtAutoBalloon:
    sleep_time = 2
    conn = None
    config = None

    def __init__(self, qemu_addr='qemu:///system', configfile='/etc/libvirt/autoballoon.json'):
        self.configfile = configfile
        self.monitored_vms = set()
        self.memory_targets = {}
        self.manual_increase_until = {}
        libvirt.virEventRegisterDefaultImpl()
        print("Connecting to libvirt at {}".format(qemu_addr), flush=True)
        self.conn = libvirt.open(qemu_addr)
        if self.conn is None:
            raise ExitFailure('Failed to open connection to the hypervisor')
        try:
            self.__load_config()
        except (OSError, ValueError, KeyError, ExitFailure):
            self.conn.close()
            raise

    def __load_config(self):
        print("Loading config file: {}".format(self.configfile), flush=True)
        with open(self.configfile) as configfile:
            config = json.load(configfile, parse_int=int)
        self.__validate_config_parameters(config)
        self.config = config

    def __validate_config_parameters(self, config):
        if (not isinstance(config, dict)
                or not isinstance(config.get("default", {}), dict)
                or not isinstance(config.get("vms"), list)
                or any(not isinstance(vm, dict) for vm in config["vms"])):
            raise ExitFailure(
                "Invalid configuration: expected an object with a vms list of objects "
                "and an optional default object")
        configurations = [("default", config.get("default", {}))]
        configurations.extend(
            ("VM {}".format(vm.get("name", "<unnamed>")), vm)
            for vm in config["vms"]
        )

        for scope, parameters in configurations:
            keep_free_kb = parameters.get("keep_free_kb")
            if keep_free_kb is not None and (
                    isinstance(keep_free_kb, bool)
                    or not isinstance(keep_free_kb, int)
                    or keep_free_kb < SZ_256MiB):
                raise ExitFailure(
                    "Invalid {} keep_free_kb: expected an integer of at least 256 MiB".format(scope))

            threshold = parameters.get("threshold")
            if threshold is not None and (
                    isinstance(threshold, bool)
                    or not isinstance(threshold, (int, float))
                    or threshold <= 0
                    or isinstance(threshold, float) and not math.isfinite(threshold)):
                raise ExitFailure(
                    "Invalid {} threshold: expected a finite number greater than 0".format(scope))

            grace = parameters.get("manual_increase_grace_seconds")
            if grace is not None and (
                    isinstance(grace, bool)
                    or not isinstance(grace, int)
                    or grace < 0):
                raise ExitFailure(
                    "Invalid {} manual_increase_grace_seconds: expected a non-negative integer".format(scope))

    def __vm_config(self, vm):
        vm_config = self.config.get("default", {}).copy()
        vm_config.update(vm)
        return vm_config

    def __vm_config_for_name(self, name):
        for vm in self.config["vms"]:
            if vm.get("name") == name:
                return self.__vm_config(vm)
        return self.config.get("default", {})

    def dom_status(self, dom):
        memstat = dom.memoryStats()
        actual = memstat.get("actual", 0)
        usable = memstat.get("usable", 0)
        used = actual - usable
        total_ram = dom.info()[1]
        keep_usable = self.dom_keep_usable(dom)
        print(dom.name(),
              int(total_ram / SZ_1MiB),
              int(actual / SZ_1MiB),
              int(used / SZ_1MiB),
              int(usable / SZ_1MiB),
              int(keep_usable / SZ_1MiB),
              "MiB", sep='\t', flush=True)

    def status(self):
        self.dom_print_names()
        domainIDs = self.conn.listDomainsID()
        if domainIDs is None:
            raise ExitFailure('No active domains')
        print("Domain", "Total", "Actual", "Used", "Usable", "Keep_Usable", "Units", sep='\t', flush=True)
        for domainID in domainIDs:
            dom = self.conn.lookupByID(domainID)
            if dom.name() in self.monitored_vms:
                self.dom_status(dom)

    def process_domainID(self, dom):
        name = dom.name()
        if name not in self.monitored_vms:
            return
        grace = self.__vm_config_for_name(name).get("manual_increase_grace_seconds", 0)
        if grace:
            current_target = dom.info()[2]
            previous_target = self.memory_targets.get(name)
            if previous_target is not None and current_target > previous_target:
                self.manual_increase_until[name] = monotonic() + grace
                print("{} memory increased externally; postponing reductions for {} seconds".format(name, grace), flush=True)
            self.memory_targets[name] = current_target
        memstat = dom.memoryStats()
        actual = memstat.get("actual", 0)
        usable = memstat.get("usable", -1)

        if actual <= 0 or usable < 0:
            logging.debug("%s has invalid memory stats, skipping", dom.name())
            return

        keep_usable = self.dom_keep_usable(dom)
        threshold = float(self.__vm_config_for_name(dom.name()).get("threshold", 0.5))
        logging.debug("%s memory stats: actual=%s KiB, usable=%s KiB, lower_threshold=%s KiB, upper_threshold=%s KiB",
                  dom.name(), actual, usable, keep_usable, keep_usable * (1 + 2 * threshold))

        if usable < keep_usable or usable > keep_usable * (1 + 2 * threshold):
            delta = keep_usable * (1 + threshold) - usable
            target = max(actual + int(delta), keep_usable * 2)
            if target < actual and self.host_has_spare_memory():
                logging.debug("%s reduction skipped: host available memory exceeds 50%%", name)
                return
            if target < actual and monotonic() < self.manual_increase_until.get(name, 0):
                logging.debug("%s reduction postponed after external memory increase", name)
                return
            logging.debug("%s usable memory outside threshold; setting target to %s KiB",
                          dom.name(), target)
            dom_balloon(dom, target)
            if grace:
                self.memory_targets[name] = max(self.memory_targets[name], min(target, dom.info()[1]))

    def host_has_spare_memory(self):
        stats = {}
        with open("/proc/meminfo") as meminfo:
            for line in meminfo.read().splitlines():
                fields = line.split()
                if fields and fields[0] in ("MemTotal:", "MemAvailable:"):
                    stats[fields[0].rstrip(":")] = int(fields[1])
        total = stats.get("MemTotal", 0)
        available = stats.get("MemAvailable", -1)
        if total <= 0 or not 0 <= available <= total:
            raise ExitFailure("Invalid or missing MemTotal/MemAvailable in /proc/meminfo")
        return available * 2 > total

    def dom_print_names(self):
        domains = self.conn.listAllDomains()
        domainNames = [dom.name() for dom in domains]
        self.monitored_vms = set()
        logging.debug("Found domains: %s", domainNames)
        for dom in domains:
            self.__refresh_domain(dom)

    def __forget_domain(self, name):
        self.monitored_vms.discard(name)
        self.memory_targets.pop(name, None)
        self.manual_increase_until.pop(name, None)

    def __refresh_domain(self, dom):
        name = dom.name()
        if self.__can_monitor_domain(dom):
            self.monitored_vms.add(name)
        else:
            self.__forget_domain(name)

    def __can_monitor_domain(self, dom):
        name = dom.name()
        if self.__vm_config_for_name(name).get("balloon", False) is not True:
            print("Not monitoring {}: not selected for monitoring".format(name), flush=True)
            return False

        try:
            domain_xml = ET.fromstring(dom.XMLDesc(0))
        except (libvirt.libvirtError, ET.ParseError) as error:
            print("Not monitoring {}: Cannot inspect balloon definition: {}".format(name, error), flush=True)
            return False

        balloon = domain_xml.find("./devices/memballoon")
        stats = balloon.find("stats") if balloon is not None else None
        raw_period = stats.get("period") if stats is not None else None
        if raw_period is None:
            print("Not monitoring {}: balloon stats period is missing".format(name), flush=True)
            return False

        try:
            stats_period = int(raw_period)
        except ValueError:
            stats_period = None

        if stats_period is None or not 0 < stats_period <= 5:
            print("Not monitoring {}: invalid balloon stats period {!r}, expected 1 to 5".format(
                name, raw_period), flush=True)
            return False

        print("Monitoring {}".format(name), flush=True)
        if balloon.get("autodeflate") != "on":
            print("{}: autodeflate='on' is recommended, but is not set".format(name), flush=True)
        return True

    def __domain_lifecycle(self, conn, dom, event, detail, opaque):
        try:
            if event in (libvirt.VIR_DOMAIN_EVENT_STOPPED, libvirt.VIR_DOMAIN_EVENT_CRASHED):
                self.__forget_domain(dom.name())
            elif event == libvirt.VIR_DOMAIN_EVENT_STARTED:
                try:
                    self.__load_config()
                except (OSError, ValueError, ExitFailure) as error:
                    logging.error("Cannot reload configuration %s; keeping previous configuration: %s",
                                  self.configfile, error)
                    self.__refresh_domain(dom)
                else:
                    self.dom_print_names()
            elif event in (libvirt.VIR_DOMAIN_EVENT_DEFINED,
                           libvirt.VIR_DOMAIN_EVENT_RESUMED):
                self.__refresh_domain(dom)
            elif event == libvirt.VIR_DOMAIN_EVENT_UNDEFINED:
                if dom.ID() >= 0:
                    self.__refresh_domain(dom)
                else:
                    self.__forget_domain(dom.name())
        except libvirt.libvirtError as error:
            logging.warning("Cannot update domain monitoring after lifecycle event: %s", error)

    def __poll_timeout(self, timer, opaque):
        self.poll_due = True

    def dom_keep_usable(self, dom):
        name = dom.name()
        keep_usable = SZ_512MiB
        keep_free_kb = self.__vm_config_for_name(name).get("keep_free_kb")
        if keep_free_kb:
            keep_usable = int(keep_free_kb)
        return keep_usable

    def daemon(self):
        print("Starting daemon", flush=True)
        callback_id = self.conn.domainEventRegisterAny(
            None, libvirt.VIR_DOMAIN_EVENT_ID_LIFECYCLE, self.__domain_lifecycle, None)
        timer_id = None
        try:
            self.dom_print_names()
            self.poll_due = True
            timer_id = libvirt.virEventAddTimeout(
                self.sleep_time * 1000, self.__poll_timeout, None)
            while True:
                if self.poll_due:
                    self.poll_due = False
                    domainIDs = self.conn.listDomainsID()
                    if domainIDs is None:
                        raise ExitFailure('Cannot list active domains')
                    for domainID in domainIDs:
                        try:
                            dom = self.conn.lookupByID(domainID)
                            self.process_domainID(dom)
                        except libvirt.libvirtError as error:
                            print("{} stopped or unavailable, skipping: {}".format(domainID, error),
                                  file=sys.stderr, flush=True)
                if libvirt.virEventRunDefaultImpl() < 0:
                    raise ExitFailure("Libvirt event loop failed")
        finally:
            if timer_id is not None:
                libvirt.virEventRemoveTimeout(timer_id)
            self.conn.domainEventDeregisterAny(callback_id)

def dom_balloon(dom, restrict_to):
    name = dom.name()
    memstat = dom.memoryStats()
    actual = memstat.get("actual", 0)
    restrict_to = int(restrict_to)
    total_ram = dom.info()[1]

    if restrict_to > total_ram:
        restrict_to = total_ram

    actual_m = int(actual / SZ_1MiB)
    restrict_to_m = int(restrict_to / SZ_1MiB)
    diff = abs(actual_m - restrict_to_m)
    if diff > 0:
        if actual > restrict_to:
            print("Shrink dom:",
                  name,
                  actual_m, "-", diff, "=", restrict_to_m, "MiB", flush=True)
        else:
            print("Grow dom:",
                  name,
                  actual_m, "+", diff, "=", restrict_to_m, "MiB", flush=True)
        dom.setMemory(restrict_to)

def libvirt_autoballoon(argv):
    parser = argparse.ArgumentParser(prog="libvirt-autoballoon")
    parser.add_argument("-c", "--config", default="/etc/libvirt/autoballoon.json",
                        help="path to the configuration file (default: %(default)s)")
    parser.add_argument("-d", "--debug", action="store_true",
                        help="enable debug output")
    parser.add_argument("action", choices=("start", "status"), help="start daemon or show status")
    args = parser.parse_args(argv[1:])
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING,
        format="%(levelname)s %(message)s")

    lv_ctrl = LibVirtAutoBalloon(configfile=args.config)

    try:
        if args.action == "start":
            lv_ctrl.daemon()
        elif args.action == "status":
            lv_ctrl.status()
    finally:
        lv_ctrl.conn.close()

def main(argv):
    libvirt_autoballoon(argv)

if __name__ == '__main__':
    main(sys.argv)
