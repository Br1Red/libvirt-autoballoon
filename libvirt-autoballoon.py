#!/usr/bin/env python3

import sys
import json
import argparse
import libvirt

from time import sleep

SZ_1MiB = 1024
SZ_512MiB = 524288

class ExitFailure(Exception):
    pass

class LibVirtAutoBalloon:
    sleep_time = 5
    conn = None
    config = None

    def __init__(self, qemu_addr='qemu:///system', configfile='/etc/libvirt/autoballoon.json'):
        self.configfile = configfile
        self.monitored_vms = set()
        print("Connecting to libvirt", flush=True)
        self.conn = libvirt.open(qemu_addr)
        if self.conn is None:
            raise ExitFailure('Failed to open connection to the hypervisor')
        self.__load_config()
        self.dom_print_names()

    def __load_config(self):
        print("Load config file: {}".format(self.configfile))
        content = open(self.configfile).read(-1)
        self.config = json.loads(content, parse_int=int)

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
        print("Name", "Total", "Actual", "Used", "Usable", "Thrshld", "Units", "Ratio", sep='\t', flush=True)
        for domainID in domainIDs:
            dom = self.conn.lookupByID(domainID)
            if dom.name() in self.monitored_vms:
                self.dom_status(dom)

    def process_domainID(self, dom):
        if dom.name() not in self.monitored_vms:
            return
        keep_usable = self.dom_keep_usable(dom)
        memstat = dom.memoryStats()
        actual = memstat.get("actual", 0)
        usable = memstat.get("usable", 0)

        if actual <=0 or usable <= 0:
            print("Domain {} has invalid memory stats, skipping".format(dom.name()), file=sys.stderr, flush=True)
            return

        if usable < keep_usable or usable > keep_usable * 2:
            delta = keep_usable * 1.5 - usable
            target = max(actual + int(delta), keep_usable * 2)
            dom_balloon(dom, target)

    def dom_print_names(self):
        domainNames = []
        for i in self.conn.listAllDomains():
            domainNames += [i.name()]
        self.monitored_vms = {
            name for name in domainNames
            if self.__vm_config_for_name(name).get("balloon", False) is True
        }
        print("Found domains:", domainNames, flush=True)
        for i in domainNames:
            if i not in self.monitored_vms:
                print("{} not selected for monitoring, ignored".format(i), flush=True)

    def dom_keep_usable(self, dom):
        name = dom.name()
        keep_usable = SZ_512MiB
        keep_free_kb = self.__vm_config_for_name(name).get("keep_free_kb")
        if keep_free_kb:
            keep_usable = int(keep_free_kb)
        return keep_usable

    def daemon(self):
        self.sleep_time = 5
        print("Start daemon", flush=True)

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
                        print("Domain {} stopped or unavailable, skipping: {}".format(domainID, error),
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
    parser.add_argument("action", choices=("start", "status"), help="start daemon or show status")
    args = parser.parse_args(argv[1:])

    lv_ctrl = LibVirtAutoBalloon(configfile=args.config)

    if args.action == "start":
        lv_ctrl.daemon()
    elif args.action == "status":
        lv_ctrl.status()

def main(argv):
    libvirt_autoballoon(argv)

if __name__ == '__main__':
    main(sys.argv)
