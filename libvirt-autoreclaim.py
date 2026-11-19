#!/usr/bin/env python3

import sys
import json
import base64
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

UNIT_MULTIPLIERS = {
    "b": 1 / 1024.0,
    "bytes": 1 / 1024.0,
    "k": 1,
    "kb": 1000 / 1024.0,
    "kib": 1,
    "m": 1024,
    "mb": (1000 ** 2) / 1024.0,
    "mib": 1024,
    "g": 1024 ** 2,
    "gb": (1000 ** 3) / 1024.0,
    "gib": 1024 ** 2,
    "t": 1024 ** 3,
    "tb": (1000 ** 4) / 1024.0,
    "tib": 1024 ** 3,
}

def parse_xml_size_kb(element, default=0):
    if element is None or element.text is None:
        return default
    raw_value = int(element.text.strip())
    unit = element.get("unit", "KiB").strip().lower()
    return int(raw_value * UNIT_MULTIPLIERS.get(unit, 1))

class MemorySample(NamedTuple):
    used_ewma: float
    used: int
    error: float
    updated: int

class ExitFailure(Exception):
    pass

class LogFormatter(logging.Formatter):
    def formatMessage(self, record):
        if record.levelno == logging.INFO:
            return record.message
        return super().formatMessage(record)

class LibVirtAutoReclaim:
    poll_interval = 5
    conn = None
    config = None

    def __init__(self, qemu_addr='qemu:///system', configfile='/etc/libvirt/autoreclaim.json'):
        self.configfile = configfile
        self.monitored_vms = set()
        self.guest_agents = set()
        self.read_only_vms = set()
        self.vm_backends = {}
        self.stats_periods = {}
        self.timer_id = None
        self.memory_targets = {}
        self.pending_targets = {}
        self.manual_increase_until = {}
        self.control_samples = {}
        libvirt.virEventRegisterDefaultImpl()
        logging.info("Connecting to libvirt at %s", qemu_addr)
        self.conn = libvirt.open(qemu_addr)
        if self.conn is None:
            raise ExitFailure('Failed to open connection to the hypervisor')
        try:
            self.__load_config()
        except (OSError, ValueError, KeyError, ExitFailure, KeyboardInterrupt, SystemExit):
            self.conn.close()
            raise

    def __load_config(self):
        with open(self.configfile) as configfile:
            config = json.load(configfile, parse_int=int)
        self.__validate_config_parameters(config)
        if self.config != config:
            action = "Loading" if self.config is None else "Reloading"
            logging.info("%s config file: %s", action, self.configfile)
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

    def _get_guest_agent_meminfo(self, dom):
        name = dom.name()
        handle = None
        try:
            import libvirt_qemu
            command = json.dumps({
                "execute": "guest-file-open",
                "arguments": {"path": "/proc/meminfo", "mode": "r"},
            })
            handle = json.loads(libvirt_qemu.qemuAgentCommand(dom, command, 1, 0))["return"]
            chunks = []
            while True:
                command = json.dumps({
                    "execute": "guest-file-read",
                    "arguments": {"handle": handle, "count": 4096},
                })
                response = json.loads(libvirt_qemu.qemuAgentCommand(dom, command, 1, 0))
                result = response.get("return", {})
                encoded = result.get("buf-b64", "")
                if encoded:
                    chunks.append(base64.b64decode(encoded).decode("utf-8", errors="ignore"))
                if result.get("eof", True) or not encoded:
                    break
            mem_total = 0
            mem_available = -1
            for line in "".join(chunks).splitlines():
                fields = line.split()
                if len(fields) >= 2:
                    if fields[0] == "MemTotal:":
                        mem_total = int(fields[1])
                    elif fields[0] == "MemAvailable:":
                        mem_available = int(fields[1])
            if mem_total > 0 and mem_available >= 0:
                return {
                    "actual": mem_total,
                    "usable": mem_available,
                    "last_update": int(time()),
                    "source": "guest-agent",
                }
        except (ImportError, libvirt.libvirtError, KeyError, ValueError) as error:
            logging.debug("%s guest-agent meminfo failed: %s", name, error)
        finally:
            if handle is not None:
                try:
                    import libvirt_qemu
                    command = json.dumps({
                        "execute": "guest-file-close",
                        "arguments": {"handle": handle},
                    })
                    libvirt_qemu.qemuAgentCommand(dom, command, 1, 0)
                except (ImportError, libvirt.libvirtError) as error:
                    logging.debug("%s guest-agent meminfo close failed: %s", name, error)
        return None

    def _get_domain_memstats(self, dom):
        name = dom.name()
        if name in self.read_only_vms:
            return self._get_guest_agent_meminfo(dom) or {}
        backend = self.vm_backends.get(name, {})
        try:
            memstat = dom.memoryStats()
        except libvirt.libvirtError as error:
            logging.debug("%s balloon memory stats failed: %s", name, error)
            memstat = {}
        actual = memstat.get("actual", 0)
        if backend.get("type") == "virtio-mem":
            vmem_info = self._inspect_virtio_mem_devices(dom)
            if vmem_info is not None:
                actual = vmem_info["base_ram_kb"] + vmem_info["total_current_kb"]
        usable = memstat.get("usable", -1)
        updated = memstat.get("last_update", 0)
        if actual > 0 and 0 <= usable <= actual and updated > 0 and 0 <= time() - updated <= 15:
            return {
                "actual": actual,
                "usable": usable,
                "last_update": updated,
                "source": "balloon",
            }
        if backend.get("has_guest_agent") or name in self.guest_agents:
            guest_stats = self._get_guest_agent_meminfo(dom)
            if guest_stats is not None:
                effective_actual = actual if actual > 0 else guest_stats["actual"]
                return {
                    "actual": effective_actual,
                    "usable": min(effective_actual, guest_stats["usable"]),
                    "last_update": guest_stats["last_update"],
                    "source": "guest-agent",
                }
        return memstat

    def _inspect_virtio_mem_devices(self, dom):
        try:
            domain_xml = ET.fromstring(dom.XMLDesc(0))
            vmem_nodes = domain_xml.findall("./devices/memory[@model='virtio-mem']")
            if not vmem_nodes:
                return None
            total_domain_mem_kb = parse_xml_size_kb(domain_xml.find("./memory"), 0)
            current_domain_mem_kb = parse_xml_size_kb(
                domain_xml.find("./currentMemory"), total_domain_mem_kb)
            devices = []
            total_size_kb = 0
            total_requested_kb = 0
            total_current_kb = 0
            for index, device in enumerate(vmem_nodes):
                target = device.find("target")
                if target is None:
                    continue
                size_kb = parse_xml_size_kb(target.find("size"), 0)
                block_kb = parse_xml_size_kb(target.find("block"), 2048)
                requested_kb = parse_xml_size_kb(target.find("requested"), 0)
                current_kb = parse_xml_size_kb(target.find("current"), requested_kb)
                node = target.find("node")
                numa_node = int(node.text.strip()) if node is not None and node.text else 0
                alias = device.find("alias")
                devices.append({
                    "index": index,
                    "alias": alias.get("name") if alias is not None else "virtiomem{}".format(index),
                    "node": numa_node,
                    "size_kb": size_kb,
                    "block_kb": max(SZ_4KiB, block_kb),
                    "requested_kb": requested_kb,
                    "current_kb": current_kb,
                })
                total_size_kb += size_kb
                total_requested_kb += requested_kb
                total_current_kb += current_kb
            base_ram_kb = max(0, current_domain_mem_kb - total_size_kb)
            return {
                "devices": devices,
                "base_ram_kb": base_ram_kb,
                "max_ram_kb": base_ram_kb + total_size_kb,
                "total_size_kb": total_size_kb,
                "total_requested_kb": total_requested_kb,
                "total_current_kb": total_current_kb,
            }
        except (libvirt.libvirtError, ET.ParseError, ValueError) as error:
            logging.warning("%s cannot inspect virtio-mem configuration: %s", dom.name(), error)
            return None

    def dom_status(self, dom):
        memstat = self._get_domain_memstats(dom)
        actual = memstat.get("actual", 0)
        usable = memstat.get("usable", 0)
        used = actual - usable
        total_ram = dom.info()[1]
        keep_usable = self.dom_keep_usable(dom, actual)
        name = dom.name()
        memory_weight = self.__vm_config_for_name(name).get("memory_weight", 100)
        backend = self.vm_backends.get(name, {"type": "balloon"})
        stats_method = memstat.get("source", "unavailable")
        memory_method = "read-only" if name in self.read_only_vms else backend["type"]
        return (
            name,
            stats_method,
            memory_method,
            str(int(total_ram / SZ_1MiB)),
            str(int(actual / SZ_1MiB)),
            str(int(used / SZ_1MiB)),
            str(int(usable / SZ_1MiB)),
            str(int(keep_usable / SZ_1MiB)),
            str(memory_weight),
            "MiB",
        )

    def status(self):
        self.dom_print_names()
        domainIDs = self.conn.listDomainsID()
        if domainIDs is None:
            raise ExitFailure('No active domains')
        rows = [(
            "Domain", "Stats_Source", "Memory_Method", "Total", "Actual",
            "Used", "Usable", "Keep_Usable", "Weight", "Units",
        )]
        for domainID in domainIDs:
            dom = self.conn.lookupByID(domainID)
            if dom.name() in self.monitored_vms:
                rows.append(self.dom_status(dom))
        widths = [max(len(value) for value in column) for column in zip(*rows)]
        for row in rows:
            print("  ".join(
                value.ljust(width) if index in (0, 1, 2, len(row) - 1) else value.rjust(width)
                for index, (value, width) in enumerate(zip(row, widths))
            ).rstrip(), flush=True)

    def process_domainID(self, dom, host_pressure=None, memstat=None):
        name = dom.name()
        if name not in self.monitored_vms:
            return
        backend = self.vm_backends.get(name, {"type": "balloon"})
        parameters = self.__vm_config_for_name(name)
        grace = parameters.get("manual_increase_grace_seconds", 0)
        if memstat is None:
            memstat = self._get_domain_memstats(dom)
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
            logging.warning("%s memory target %s KiB timed out after 30 seconds; allowing retries",
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
        if name in self.read_only_vms:
            logging.debug("%s is monitored read-only because it has no balloon device", name)
            return
        info = dom.info()
        max_allowed_ram = info[1]
        min_allowed_ram = 0
        if backend["type"] == "virtio-mem":
            vmem_info = self._inspect_virtio_mem_devices(dom)
            if vmem_info is not None:
                max_allowed_ram = vmem_info["max_ram_kb"]
                min_allowed_ram = vmem_info["base_ram_kb"]
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
        pd_floor = int(min_reserve * 2 / max(0.1, 1 - parameters.get("keep_free_ratio", 0.1)))
        floor = min(max_allowed_ram, max(min_allowed_ram, pd_floor))
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
        target = min(max_allowed_ram, max(floor, actual + int(delta)))
        if pending_target is not None and not (emergency and target > max(actual, pending_target)):
            logging.debug("%s waiting for memory target %s KiB; actual=%s KiB",
                          name, pending_target, actual)
            return
        logging.debug(
            "%s [%s] actual=%s usable=%s used_ewma=%.1f reserve=%.1f error=%.1f "
            "derivative=%.1f used_rate=%.1f emergency=%s target=%s KiB",
            name, backend["type"], actual, usable, used_ewma, reserve, error,
            derivative, used_rate, emergency, target)
        if backend["type"] == "virtio-mem":
            requested = dom_virtio_mem(dom, target, actual)
        else:
            requested = dom_balloon(dom, target, actual, max_allowed_ram)
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
                memstat = self._get_domain_memstats(dom)
                actual = memstat.get("actual", 0)
                idle_ratio = max(0, memstat.get("usable", 0)) / actual if actual > 0 else 0
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
        for name in self.monitored_vms - set(domainNames):
            self.__forget_domain(name)
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
        if name in self.monitored_vms:
            logging.info("Stopped monitoring %s", name)
        self.monitored_vms.discard(name)
        self.guest_agents.discard(name)
        self.read_only_vms.discard(name)
        self.vm_backends.pop(name, None)
        self.stats_periods.pop(name, None)
        self.memory_targets.pop(name, None)
        self.pending_targets.pop(name, None)
        self.manual_increase_until.pop(name, None)
        self.control_samples.pop(name, None)

    def __refresh_domain(self, dom):
        name = dom.name()
        try:
            active = dom.isActive()
        except libvirt.libvirtError as error:
            logging.debug("%s cannot determine active state for monitoring: %s", name, error)
            return
        if active and self.__can_monitor_domain(dom):
            if name not in self.monitored_vms:
                logging.info("Monitoring %s", name)
            self.monitored_vms.add(name)
        else:
            self.__forget_domain(name)

    def __set_stats_period_if_active(self, dom, period):
        name = dom.name()
        try:
            active = dom.isActive()
        except libvirt.libvirtError as error:
            logging.debug("%s cannot determine active state for polling interval: %s", name, error)
            return
        if active:
            self.stats_periods[name] = period

    def __can_monitor_domain(self, dom):
        name = dom.name()
        self.guest_agents.discard(name)
        self.read_only_vms.discard(name)
        self.stats_periods.pop(name, None)
        if self.__vm_config_for_name(name).get("auto_reclaim", False) is not True:
            return False
        try:
            domain_xml = ET.fromstring(dom.XMLDesc(0))
        except (libvirt.libvirtError, ET.ParseError) as error:
            print("Not monitoring {}: Cannot inspect balloon definition: {}".format(name, error), flush=True)
            return False
        has_guest_agent = domain_xml.find(
            "./devices/channel[@type='unix']/target[@name='org.qemu.guest_agent.0']"
        ) is not None
        has_spice_channel = domain_xml.find("./devices/channel[@type='spicevmc']") is not None
        vmem_devices = domain_xml.findall("./devices/memory[@model='virtio-mem']")
        balloon = domain_xml.find("./devices/memballoon")
        balloon_enabled = balloon is not None and balloon.get("model") != "none"
        stats = balloon.find("stats") if balloon_enabled else None
        raw_period = stats.get("period") if stats is not None else None
        try:
            stats_period = int(raw_period) if raw_period is not None else None
        except (TypeError, ValueError):
            stats_period = None
        if stats_period is not None and not 0 < stats_period <= 5:
            stats_period = None
        if balloon is not None and balloon.get("model") != "none" and raw_period is None:
            logging.warning("%s balloon stats period is missing", name)
        if vmem_devices:
            if stats_period is None and not has_guest_agent:
                print(
                    "Not monitoring {}: virtio-mem requires valid balloon stats or "
                    "the QEMU Guest Agent".format(name),
                    flush=True)
                return False
            self.vm_backends[name] = {
                "type": "virtio-mem",
                "device_count": len(vmem_devices),
                "has_balloon_stats": stats_period is not None,
                "has_guest_agent": has_guest_agent,
            }
            if has_guest_agent:
                self.guest_agents.add(name)
            self.__set_stats_period_if_active(dom, stats_period if stats_period is not None else 5)
            return True
        if not balloon_enabled:
            if has_spice_channel and has_guest_agent:
                self.guest_agents.add(name)
                self.read_only_vms.add(name)
                self.memory_targets.pop(name, None)
                self.pending_targets.pop(name, None)
                self.manual_increase_until.pop(name, None)
                self.control_samples.pop(name, None)
                self.__set_stats_period_if_active(dom, 5)
                return True
            print("Not monitoring {}: balloon device is missing or disabled".format(name), flush=True)
            return False
        if stats_period is None or not 0 < stats_period <= 5:
            if not has_guest_agent:
                if raw_period is not None:
                    print("Not monitoring {}: invalid balloon stats period {!r}, expected 1 to 5".format(
                        name, raw_period), flush=True)
                return False
            stats_period = 5
        if balloon.get("autodeflate") != "on":
            print("{}: autodeflate='on' is recommended, but is not set".format(name), flush=True)
        if has_guest_agent:
            self.guest_agents.add(name)
        self.vm_backends[name] = {
            "type": "balloon",
            "device_count": 1,
            "has_balloon_stats": stats_period is not None,
            "has_guest_agent": has_guest_agent,
        }
        self.__set_stats_period_if_active(dom, stats_period)
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

    def __memory_device_size_change(self, conn, dom, alias, size, opaque):
        name = dom.name()
        logging.debug("%s virtio-mem device %s changed size to %s KiB", name, alias, size)
        vmem_info = self._inspect_virtio_mem_devices(dom)
        if vmem_info is not None:
            actual_total = vmem_info["base_ram_kb"] + vmem_info["total_current_kb"]
            pending = self.pending_targets.get(name)
            if pending is not None and abs(actual_total - pending[0]) <= SZ_4KiB:
                self.memory_targets[name] = actual_total
                self.pending_targets.pop(name, None)

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
        memdev_callback_id = None
        try:
            if hasattr(libvirt, "VIR_DOMAIN_EVENT_ID_MEMORY_DEVICE_SIZE_CHANGE"):
                memdev_callback_id = self.conn.domainEventRegisterAny(
                    None,
                    libvirt.VIR_DOMAIN_EVENT_ID_MEMORY_DEVICE_SIZE_CHANGE,
                    self.__memory_device_size_change,
                    None,
                )
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
            if memdev_callback_id is not None:
                self.conn.domainEventDeregisterAny(memdev_callback_id)

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
        print("Shrink dom (balloon):",
              name,
              actual_m, "-", diff_m, "=", restrict_to_m, "MiB", flush=True)
    else:
        print("Grow dom (balloon):",
              name,
              actual_m, "+", diff_m, "=", restrict_to_m, "MiB", flush=True)
    dom.setMemory(restrict_to)
    return restrict_to

def dom_virtio_mem(dom, target_total_kb, actual_total_kb):
    name = dom.name()
    try:
        domain_xml = ET.fromstring(dom.XMLDesc(0))
    except (libvirt.libvirtError, ET.ParseError) as error:
        logging.error("%s cannot parse XML for virtio-mem update: %s", name, error)
        return None
    vmem_nodes = domain_xml.findall("./devices/memory[@model='virtio-mem']")
    if not vmem_nodes:
        return None
    try:
        current_domain_mem_kb = parse_xml_size_kb(
            domain_xml.find("./currentMemory"), actual_total_kb)
        devices = []
        total_size_kb = 0
        for device in vmem_nodes:
            target = device.find("target")
            if target is None:
                continue
            size_kb = parse_xml_size_kb(target.find("size"), 0)
            block_kb = max(SZ_4KiB, parse_xml_size_kb(target.find("block"), 2048))
            requested_kb = parse_xml_size_kb(target.find("requested"), 0)
            current_kb = parse_xml_size_kb(target.find("current"), requested_kb)
            node = target.find("node")
            numa_node = int(node.text.strip()) if node is not None and node.text else 0
            devices.append({
                "xml": device,
                "target": target,
                "node": numa_node,
                "size_kb": size_kb,
                "block_kb": block_kb,
                "requested_kb": requested_kb,
                "current_kb": current_kb,
            })
            total_size_kb += size_kb
    except (TypeError, ValueError) as error:
        logging.error("%s cannot parse virtio-mem sizes: %s", name, error)
        return None
    if total_size_kb <= 0 or not devices:
        return None
    base_ram_kb = max(0, current_domain_mem_kb - total_size_kb)
    desired_vmem_total = max(0, min(total_size_kb, int(target_total_kb) - base_ram_kb))
    ratio = desired_vmem_total / float(total_size_kb)
    updates = []
    new_total_requested = 0
    for device in devices:
        raw_requested = int(device["size_kb"] * ratio)
        aligned_requested = int(round(raw_requested / device["block_kb"])) * device["block_kb"]
        aligned_requested = max(0, min(device["size_kb"], aligned_requested))
        updates.append((device, aligned_requested))
        new_total_requested += aligned_requested
    new_total_ram = base_ram_kb + new_total_requested
    difference = abs(actual_total_kb - new_total_ram)
    min_block = min(device["block_kb"] for device in devices)
    if difference < max(SZ_1MiB, min_block):
        return None
    changed = False
    for device, requested_kb in updates:
        if requested_kb == device["requested_kb"]:
            continue
        target = device["target"]
        requested = target.find("requested")
        if requested is None:
            requested = ET.SubElement(target, "requested")
        requested.set("unit", "KiB")
        requested.text = str(requested_kb)
        current = target.find("current")
        if current is not None:
            target.remove(current)
        device_xml = ET.tostring(device["xml"], encoding="unicode")
        try:
            dom.updateDeviceFlags(device_xml, libvirt.VIR_DOMAIN_AFFECT_LIVE)
            changed = True
        except libvirt.libvirtError as error:
            logging.error(
                "%s failed updating virtio-mem on node %s to %s KiB: %s",
                name, device["node"], requested_kb, error)
    if not changed:
        return None
    actual_mib = "{:.3f}".format(actual_total_kb / SZ_1MiB)
    target_mib = "{:.3f}".format(new_total_ram / SZ_1MiB)
    difference_mib = "{:.3f}".format(difference / SZ_1MiB)
    if actual_total_kb > new_total_ram:
        print("Shrink dom (virtio-mem, {} dev): {} {} - {} = {} MiB".format(
            len(devices), name, actual_mib, difference_mib, target_mib), flush=True)
    else:
        print("Grow dom (virtio-mem, {} dev): {} {} + {} = {} MiB".format(
            len(devices), name, actual_mib, difference_mib, target_mib), flush=True)
    return new_total_ram

def handle_sigterm(signum, _frame):
    raise SystemExit(128 + signum)

def report_libvirt_error(_context, error):
    code, _, message, level = error[:4]
    if (code == libvirt.VIR_ERR_AGENT_UNRESPONSIVE
            or code == getattr(libvirt, "VIR_ERR_AGENT_COMMAND_TIMEOUT", None)):
        # The guest-agent reader already reports the exception at debug level.
        return
    log = logging.warning if level == libvirt.VIR_ERR_WARNING else logging.error
    log("libvirt: %s", message)

def libvirt_autoreclaim(argv):
    parser = argparse.ArgumentParser(prog="libvirt-autoreclaim")
    parser.add_argument("-c", "--config", default="/etc/libvirt/autoreclaim.json",
                        help="path to the configuration file (default: %(default)s)")
    parser.add_argument("-d", "--debug", action="store_true",
                        help="enable debug output")
    parser.add_argument("action", choices=("start", "status"), help="start daemon or show status")
    args = parser.parse_args(argv[1:])
    handler = logging.StreamHandler()
    handler.setFormatter(LogFormatter("%(levelname)s %(message)s"))
    logging.basicConfig(
        level=(logging.DEBUG if args.debug else
               logging.INFO if args.action == "start" else logging.WARNING),
        handlers=[handler])
    previous_sigterm = signal.signal(signal.SIGTERM, handle_sigterm)
    try:
        libvirt.registerErrorHandler(report_libvirt_error, None)
        lv_ctrl = LibVirtAutoReclaim(configfile=args.config)
        try:
            if args.action == "start":
                lv_ctrl.daemon()
            elif args.action == "status":
                lv_ctrl.status()
        finally:
            lv_ctrl.conn.close()
    finally:
        libvirt.registerErrorHandler(None, None)
        signal.signal(signal.SIGTERM, previous_sigterm)

def main(argv):
    libvirt_autoreclaim(argv)

if __name__ == '__main__':
    main(sys.argv)
