#!/usr/bin/env python3

import sys
import json
import argparse
import logging
import math
import xml.etree.ElementTree as ET
import libvirt

from time import sleep

SZ_1MiB = 1024
SZ_256MiB = 256 * 1024
SZ_512MiB = 512 * 1024

class ExitFailure(Exception):
    pass

class LibVirtAutoBalloon:
    sleep_time = 5
    conn = None
    config = None

    def __init__(self, qemu_addr='qemu:///system', configfile='/etc/libvirt/autoballoon.json'):
        self.configfile = configfile
        self.monitored_vms = set()
        print("Connecting to libvirt at {}".format(qemu_addr), flush=True)
        self.conn = libvirt.open(qemu_addr)
        if self.conn is None:
            raise ExitFailure('Failed to open connection to the hypervisor')
        self.__load_config()
        self.dom_print_names()

    def __load_config(self):
        print("Loading config file: {}".format(self.configfile), flush=True)
        content = open(self.configfile).read(-1)
        self.config = json.loads(content, parse_int=int)
        self.__validate_config_parameters()

    def __validate_config_parameters(self):
        configurations = [("default", self.config.get("default", {}))]
        configurations.extend(
            ("VM {}".format(vm.get("name", "<unnamed>")), vm)
            for vm in self.config["vms"]
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
        domainIDs = self.conn.listDomainsID()
        if domainIDs is None:
            raise ExitFailure('No active domains')
        print("Domain", "Total", "Actual", "Used", "Usable", "Keep_Usable", "Units", sep='\t', flush=True)
        for domainID in domainIDs:
            dom = self.conn.lookupByID(domainID)
            if dom.name() in self.monitored_vms:
                self.dom_status(dom)

    def process_domainID(self, dom):
        if dom.name() not in self.monitored_vms:
            return
        memstat = dom.memoryStats()
        actual = memstat.get("actual", 0)
        usable = memstat.get("usable", 0)

        if actual <=0 or usable <= 0:
            print("{} has invalid memory stats, skipping".format(dom.name()), flush=True)
            return

        keep_usable = self.dom_keep_usable(dom)
        threshold = float(self.__vm_config_for_name(dom.name()).get("threshold", 0.5))
        logging.debug("%s memory stats: actual=%s KiB, usable=%s KiB, lower_threshold=%s KiB, upper_threshold=%s KiB",
                  dom.name(), actual, usable, keep_usable, keep_usable * (1 + 2 * threshold))

        if usable < keep_usable or usable > keep_usable * (1 + 2 * threshold):
            delta = keep_usable * (1 + threshold) - usable
            target = max(actual + int(delta), keep_usable * 2)
            logging.debug("%s usable memory outside threshold; setting target to %s KiB",
                          dom.name(), target)
            dom_balloon(dom, target)

    def dom_print_names(self):
        domains = self.conn.listAllDomains()
        domainNames = [dom.name() for dom in domains]
        self.monitored_vms = set()
        logging.debug("Found domains: %s", domainNames)
        for dom in domains:
            name = dom.name()
            if self.__vm_config_for_name(name).get("balloon", False) is not True:
                print("Not monitoring {}: not selected for monitoring".format(name), flush=True)
                continue

            try:
                domain_xml = ET.fromstring(dom.XMLDesc(0))
            except (libvirt.libvirtError, ET.ParseError) as error:
                print("Not monitoring {}: Cannot inspect balloon definition: {}".format(name, error), flush=True)
                continue

            balloon = domain_xml.find("./devices/memballoon")
            stats = balloon.find("stats") if balloon is not None else None
            raw_period = stats.get("period") if stats is not None else None
            if raw_period is None:
                print("Not monitoring {}: balloon stats period is missing".format(name), flush=True)
                continue

            try:
                stats_period = int(raw_period)
            except ValueError:
                stats_period = None

            if stats_period is None or not 0 < stats_period <= 5:
                print("Not monitoring {}: invalid balloon stats period {!r}, expected 1 to 5".format(
                    name, raw_period))
                continue

            self.monitored_vms.add(name)
            print("Monitoring {}".format(name), flush=True)
            if balloon is None or balloon.get("autodeflate") != "on":
                print("{}: autodeflate='on' is recommended, but is not set".format(name), flush=True)

    def dom_keep_usable(self, dom):
        name = dom.name()
        keep_usable = SZ_512MiB
        keep_free_kb = self.__vm_config_for_name(name).get("keep_free_kb")
        if keep_free_kb:
            keep_usable = int(keep_free_kb)
        return keep_usable

    def daemon(self):
        self.sleep_time = 5
        print("Starting daemon", flush=True)

        while True:
            domainIDs = self.conn.listDomainsID()

            if domainIDs is None:
                print('No active domains', file=sys.stderr, flush=True)
                if self.sleep_time < 10:
                    self.sleep_time += 1
            else:
                if self.sleep_time > 5:
                    self.sleep_time -= 1

                for domainID in domainIDs:
                    try:
                        dom = self.conn.lookupByID(domainID)
                        self.process_domainID(dom)
                    except libvirt.libvirtError as error:
                        print("{} stopped or unavailable, skipping: {}".format(domainID, error),
                              file=sys.stderr, flush=True)

            sleep(self.sleep_time)

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

    if args.action == "start":
        lv_ctrl.daemon()
    elif args.action == "status":
        lv_ctrl.status()

def main(argv):
    libvirt_autoballoon(argv)

if __name__ == '__main__':
    main(sys.argv)
