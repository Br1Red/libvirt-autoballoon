#!/usr/bin/env python3

import sys
import json
import argparse
import logging
import math
import signal
import xml.etree.ElementTree as ET
import libvirt

from time import monotonic, time
from typing import NamedTuple

SZ_4KiB = 4
SZ_1MiB = 1024
SZ_256MiB = 256 * 1024
SZ_512MiB = 512 * 1024

class MemorySample(NamedTuple):
    used_ewma: float
    used: int
    error: float
    updated: int

class ExitFailure(Exception):
    pass

class LibVirtAutoBalloon:
    poll_interval = 5
    conn = None
    config = None

    def __init__(self, qemu_addr='qemu:///system', configfile='/etc/libvirt/autoballoon.json'):
        self.configfile = configfile
        self.monitored_vms = set()
        self.stats_periods = {}
        self.timer_id = None
        self.memory_targets = {}
        self.pending_targets = {}
        self.manual_increase_until = {}
        self.control_samples = {}
        libvirt.virEventRegisterDefaultImpl()
        print("Connecting to libvirt at {}".format(qemu_addr), flush=True)
        self.conn = libvirt.open(qemu_addr)
        if self.conn is None:
            raise ExitFailure('Failed to open connection to the hypervisor')
        try:
            self.__load_config()
        except (OSError, ValueError, KeyError, ExitFailure, KeyboardInterrupt, SystemExit):
            self.conn.close()
            raise

    def __load_config(self):
        print("Loading config file: {}".format(self.configfile), flush=True)
        with open(self.configfile) as configfile:
            config = json.load(configfile, parse_int=int)
        self.__validate_config_parameters(config)
        if self.config != config:
            self.control_samples.clear()
        self.config = config

    def __validate_config_parameters(self, config):
        if (not isinstance(config, dict)
                or not isinstance(config.get("general", {}), dict)
                or not isinstance(config.get("default", {}), dict)
                or not isinstance(config.get("vms"), list)
                or any(not isinstance(vm, dict) for vm in config["vms"])):
            raise ExitFailure(
                "Invalid configuration: expected an object with a vms list of objects "
                "and optional general and default objects")
        general_keys = ("psi_threshold", "host_min_free_ratio")
        unknown_general = set(config.get("general", {})) - set(general_keys)
        if unknown_general:
            raise ExitFailure("Unsupported general parameters: {}".format(", ".join(sorted(unknown_general))))
        configurations = [
            ("general", config.get("general", {})),
            ("default", config.get("default", {})),
        ]
        configurations.extend(
            ("VM {}".format(vm.get("name", "<unnamed>")), vm)
            for vm in config["vms"]
        )
        for scope, parameters in configurations:
            memory_weight = parameters.get("memory_weight", 100)
            if (isinstance(memory_weight, bool)
                    or not isinstance(memory_weight, int)
                    or not 1 <= memory_weight <= 1000):
                raise ExitFailure(
                    "Invalid {} memory_weight: expected an integer from 1 to 1000".format(scope))
            keep_free_kb = parameters.get("keep_free_kb")
            if keep_free_kb is not None and (
                    isinstance(keep_free_kb, bool)
                    or not isinstance(keep_free_kb, int)
                    or keep_free_kb < SZ_256MiB):
                raise ExitFailure(
                    "Invalid {} keep_free_kb: expected an integer of at least 256 MiB".format(scope))
            bounds = {
                "ewma_alpha": (0, 1, False),
                "keep_free_ratio": (0, 1, True),
                "reclaim_ratio": (0, 0.05, False),
                "pd_kp": (0, 1, False),
                "pd_kd": (0, 10, True),
                "psi_threshold": (0, 100, False),
                "host_min_free_ratio": (0, 1, False),
            }
            for key, (lower, upper, include_lower) in bounds.items():
                value = parameters.get(key)
                if key not in parameters:
                    continue
                if scope != "general" and key in general_keys:
                    raise ExitFailure("{} is only supported in general, not {}".format(key, scope))
                if (isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or isinstance(value, float) and not math.isfinite(value)
                        or not lower <= value <= upper
                        or not include_lower and value == lower):
                    raise ExitFailure(
                        "Invalid {} {}: expected a finite number {} {} and <= {}".format(
                            scope, key, ">=" if include_lower else ">", lower, upper))
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
        keep_usable = self.dom_keep_usable(dom, actual)
        name = dom.name()
        memory_weight = self.__vm_config_for_name(name).get("memory_weight", 100)
        print(name,
              int(total_ram / SZ_1MiB),
              int(actual / SZ_1MiB),
              int(used / SZ_1MiB),
              int(usable / SZ_1MiB),
              int(keep_usable / SZ_1MiB),
              memory_weight,
              "MiB", sep='\t', flush=True)

    def status(self):
        self.dom_print_names()
        domainIDs = self.conn.listDomainsID()
        if domainIDs is None:
            raise ExitFailure('No active domains')
        print("Domain", "Total", "Actual", "Used", "Usable", "Keep_Usable", "Weight", "Units",
              sep='\t', flush=True)
        for domainID in domainIDs:
            dom = self.conn.lookupByID(domainID)
            if dom.name() in self.monitored_vms:
                self.dom_status(dom)

    def process_domainID(self, dom, host_pressure=None, memstat=None):
        name = dom.name()
        if name not in self.monitored_vms:
            return
        parameters = self.__vm_config_for_name(name)
        grace = parameters.get("manual_increase_grace_seconds", 0)
        if memstat is None:
            memstat = dom.memoryStats()
        actual = memstat.get("actual", 0)
        usable = memstat.get("usable", -1)
        updated = memstat.get("last_update", 0)
        now = monotonic()
        pending_request = self.pending_targets.get(name)
        pending_target = pending_request[0] if pending_request is not None else None
        pending_expired = (
            pending_request is not None
            and now - pending_request[1] >= 30
            and abs(actual - pending_request[0]) > SZ_4KiB)
        if pending_expired:
            logging.warning("%s balloon target %s KiB timed out after 30 seconds; allowing retries",
                            name, pending_target)
            self.pending_targets.pop(name, None)
        if actual <= 0 or not 0 <= usable <= actual or updated <= 0:
            logging.debug("%s has invalid memory stats, skipping", name)
            return
        if not 0 <= time() - updated <= 15:
            logging.warning("%s has stale or future-dated memory stats, skipping", name)
            self.control_samples.pop(name, None)
            return
        previous = self.control_samples.get(name)
        if previous is not None and updated == previous.updated:
            logging.debug("%s memory stats have not updated, skipping", name)
            return
        info = dom.info()
        previous_memory = self.memory_targets.get(name)
        own_growth = pending_target is not None and actual <= pending_target + SZ_4KiB
        external_increase = (
            previous_memory is not None
            and actual > previous_memory + SZ_4KiB
            and not own_growth)
        self.memory_targets[name] = actual
        if pending_target is not None and abs(actual - pending_target) <= SZ_4KiB:
            self.memory_targets[name] = max(actual, pending_target)
            self.pending_targets.pop(name, None)
            pending_target = None
        elif external_increase or pending_expired:
            self.pending_targets.pop(name, None)
            pending_target = None
        if external_increase and grace:
            self.manual_increase_until[name] = now + grace
            print(
                "{} unattributed memory increase: {} -> {} KiB; "
                "postponing reductions for {} seconds".format(name, previous_memory, actual, grace),
                flush=True)
        if external_increase or previous is not None and updated < previous.updated:
            previous = None
        used = actual - usable
        alpha = parameters.get("ewma_alpha", 0.3)
        used_ewma = used if previous is None else alpha * used + (1 - alpha) * previous.used_ewma
        reserve = self.dom_keep_usable(dom, actual)
        error = reserve - (actual - used_ewma)
        derivative = 0
        used_rate = 0
        elapsed = self.stats_periods.get(name, self.poll_interval)
        if previous is not None:
            elapsed = max(1, updated - previous.updated)
            derivative = (error - previous.error) / elapsed
            used_rate = (used - previous.used) / elapsed
        self.control_samples[name] = MemorySample(used_ewma, used, error, updated)
        derivative_term = max(
            -abs(error), min(abs(error), parameters.get("pd_kd", 1.0) * derivative))
        delta = parameters.get("pd_kp", 0.5) * error + derivative_term
        emergency = usable < reserve or used_rate > actual * 0.05
        if emergency:
            delta = max(delta, reserve - usable + max(0, used_rate) * self.poll_interval,
                        actual * 0.25)
        elif error <= 0:
            delta = min(delta, 0)
        elif delta > 0:
            delta = min(delta, actual * 0.5)
        min_reserve = self.dom_keep_usable(dom, 0)
        floor = min(info[1], int(
            min_reserve * 2 / max(0.1, 1 - parameters.get("keep_free_ratio", 0.1))))
        if delta < 0:
            if now < self.manual_increase_until.get(name, 0):
                logging.debug("%s reduction postponed after an unattributed memory increase", name)
                return
            if host_pressure is None:
                host_pressure = self.host_memory_pressure()
            idle_ratio = usable / actual
            weight_multiplier = min(2.0, 100.0 / parameters.get("memory_weight", 100))
            reclaim_limit = (
                actual * parameters.get("reclaim_ratio", 0.015) * elapsed * host_pressure * idle_ratio
                * weight_multiplier)
            surplus = max(0, min(usable - reserve, -error))
            delta = -min(-delta, reclaim_limit, surplus)
        target = min(info[1], max(floor, actual + int(delta)))
        if pending_target is not None and not (emergency and target > max(actual, pending_target)):
            logging.debug("%s waiting for balloon target %s KiB; actual=%s KiB",
                          name, pending_target, actual)
            return
        logging.debug(
            "%s actual=%s usable=%s used_ewma=%.1f reserve=%.1f error=%.1f "
            "derivative=%.1f used_rate=%.1f emergency=%s target=%s KiB",
            name, actual, usable, used_ewma, reserve, error, derivative, used_rate, emergency, target)
        requested = dom_balloon(dom, target, actual, info[1])
        if requested is not None:
            self.pending_targets[name] = (requested, monotonic())

    def host_memory_pressure(self):
        try:
            stats = {}
            with open("/proc/meminfo") as meminfo:
                for line in meminfo:
                    fields = line.split()
                    if fields and fields[0] in ("MemTotal:", "MemAvailable:"):
                        stats[fields[0].rstrip(":")] = int(fields[1])
            total = stats.get("MemTotal", 0)
            available = stats.get("MemAvailable", -1)
            if total <= 0 or not 0 <= available <= total:
                raise ExitFailure("Invalid or missing MemTotal/MemAvailable in /proc/meminfo")
            pressure = None
            with open("/proc/pressure/memory") as psi:
                for line in psi:
                    fields = line.split()
                    if fields and fields[0] == "some":
                        for field in fields[1:]:
                            if field.startswith("avg10="):
                                pressure = float(field.split("=", 1)[1])
            if pressure is None or not math.isfinite(pressure) or not 0 <= pressure <= 100:
                raise ExitFailure("Invalid or missing some avg10 in /proc/pressure/memory")
        except (OSError, ValueError, IndexError, ExitFailure) as error:
            logging.error("Cannot read host memory pressure; reclaim disabled for this cycle: %s", error)
            return 0.0
        parameters = self.config.get("general", {})
        psi_threshold = parameters.get("psi_threshold", 5)
        host_min_free_ratio = parameters.get("host_min_free_ratio", 0.2)
        return max(min(pressure / psi_threshold, 1),
                   max(0, 1 - available / (total * host_min_free_ratio)))

    def process_domains(self):
        domainIDs = self.conn.listDomainsID()
        if domainIDs is None:
            raise ExitFailure('Cannot list active domains')
        domains = []
        for domainID in domainIDs:
            try:
                dom = self.conn.lookupByID(domainID)
                name = dom.name()
                if name not in self.monitored_vms:
                    continue
                memstat = dom.memoryStats()
                actual = memstat.get("actual", 0)
                idle_ratio = memstat.get("usable", 0) / actual if actual > 0 else 0
                memory_weight = self.__vm_config_for_name(name).get("memory_weight", 100)
                yield_score = idle_ratio / (memory_weight / 100.0)
                domains.append((yield_score, dom, memstat))
            except libvirt.libvirtError as error:
                logging.warning("%s stopped or unavailable, skipping: %s", domainID, error)
        pressure = self.host_memory_pressure() if domains else 0.0
        for _, dom, memstat in sorted(domains, key=lambda entry: entry[0], reverse=True):
            try:
                self.process_domainID(dom, pressure, memstat)
            except libvirt.libvirtError as error:
                logging.warning("Domain stopped or unavailable, skipping: %s", error)

    def dom_print_names(self):
        domains = self.conn.listAllDomains()
        domainNames = [dom.name() for dom in domains]
        self.monitored_vms = set()
        self.stats_periods.clear()
        logging.debug("Found domains: %s", domainNames)
        for dom in domains:
            self.__refresh_domain(dom)
        self.__update_poll_interval()

    def __update_poll_interval(self):
        interval = min(self.stats_periods.values(), default=5)
        if interval != self.poll_interval:
            self.poll_interval = interval
            logging.info("Polling interval changed to %s seconds", interval)
            if self.timer_id is not None:
                libvirt.virEventUpdateTimeout(self.timer_id, interval * 1000)

    def __forget_domain(self, name):
        self.monitored_vms.discard(name)
        self.stats_periods.pop(name, None)
        self.memory_targets.pop(name, None)
        self.pending_targets.pop(name, None)
        self.manual_increase_until.pop(name, None)
        self.control_samples.pop(name, None)

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
        self.stats_periods[name] = stats_period
        return True

    def __domain_lifecycle(self, conn, dom, event, detail, opaque):
        try:
            if event in (libvirt.VIR_DOMAIN_EVENT_STOPPED, libvirt.VIR_DOMAIN_EVENT_CRASHED):
                self.__forget_domain(dom.name())
            elif event == libvirt.VIR_DOMAIN_EVENT_STARTED:
                self.control_samples.pop(dom.name(), None)
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
                self.control_samples.pop(dom.name(), None)
                self.__refresh_domain(dom)
            elif event == libvirt.VIR_DOMAIN_EVENT_UNDEFINED:
                if dom.ID() >= 0:
                    self.__refresh_domain(dom)
                else:
                    self.__forget_domain(dom.name())
            self.__update_poll_interval()
        except libvirt.libvirtError as error:
            logging.warning("Cannot update domain monitoring after lifecycle event: %s", error)

    def __poll_timeout(self, timer, opaque):
        self.poll_due = True

    def dom_keep_usable(self, dom, actual=0):
        name = dom.name()
        keep_usable = SZ_512MiB
        keep_free_kb = self.__vm_config_for_name(name).get("keep_free_kb")
        if keep_free_kb:
            keep_usable = int(keep_free_kb)
        return max(keep_usable, int(actual * self.__vm_config_for_name(name).get("keep_free_ratio", 0.1)))

    def daemon(self):
        print("Starting daemon", flush=True)
        callback_id = self.conn.domainEventRegisterAny(
            None, libvirt.VIR_DOMAIN_EVENT_ID_LIFECYCLE, self.__domain_lifecycle, None)
        try:
            self.dom_print_names()
            self.poll_due = True
            self.timer_id = libvirt.virEventAddTimeout(
                self.poll_interval * 1000, self.__poll_timeout, None)
            while True:
                if self.poll_due:
                    self.poll_due = False
                    self.process_domains()
                if libvirt.virEventRunDefaultImpl() < 0:
                    raise ExitFailure("Libvirt event loop failed")
        finally:
            if self.timer_id is not None:
                libvirt.virEventRemoveTimeout(self.timer_id)
                self.timer_id = None
            self.conn.domainEventDeregisterAny(callback_id)

def dom_balloon(dom, restrict_to, actual, total_ram):
    name = dom.name()
    restrict_to = int(restrict_to)
    if restrict_to > total_ram:
        restrict_to = total_ram
    restrict_to = (restrict_to // SZ_4KiB) * SZ_4KiB
    diff_kb = abs(actual - restrict_to)
    if diff_kb < SZ_1MiB:
        return None
    actual_m = "{:.3f}".format(actual / SZ_1MiB)
    restrict_to_m = "{:.3f}".format(restrict_to / SZ_1MiB)
    diff_m = "{:.3f}".format(diff_kb / SZ_1MiB)
    if actual > restrict_to:
        print("Shrink dom:",
              name,
              actual_m, "-", diff_m, "=", restrict_to_m, "MiB", flush=True)
    else:
        print("Grow dom:",
              name,
              actual_m, "+", diff_m, "=", restrict_to_m, "MiB", flush=True)
    dom.setMemory(restrict_to)
    return restrict_to

def handle_sigterm(signum, _frame):
    raise SystemExit(128 + signum)

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
    previous_sigterm = signal.signal(signal.SIGTERM, handle_sigterm)
    try:
        lv_ctrl = LibVirtAutoBalloon(configfile=args.config)
        try:
            if args.action == "start":
                lv_ctrl.daemon()
            elif args.action == "status":
                lv_ctrl.status()
        finally:
            lv_ctrl.conn.close()
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)

def main(argv):
    libvirt_autoballoon(argv)

if __name__ == '__main__':
    main(sys.argv)
