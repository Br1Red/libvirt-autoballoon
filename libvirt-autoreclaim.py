#!/usr/bin/env python3

import sys
import json
import base64
import argparse
import logging
import math
import os
import signal
import xml.etree.ElementTree as ET
import libvirt
from concurrent.futures import ThreadPoolExecutor, wait

from time import monotonic, sleep, time
from typing import NamedTuple, Optional

SZ_4KiB = 4
SZ_1MiB = 1024
SZ_256MiB = 256 * 1024
SZ_512MiB = 512 * 1024
HOST_PRESSURE_UNAVAILABLE = -1.0
RECONNECT_MAX_DELAY = 60
GUEST_SWAP_EMERGENCY_KIB_PER_SEC = 10 * SZ_1MiB
GUEST_SWAP_VETO_KIB_PER_SEC = 64
GUEST_SWAP_VETO_MIN_DROP_KIB = 256
GUEST_OS_RETRY_INTERVAL = 60
WINDOWS_STATUS_TIMEOUT = 5
WINDOWS_MEMORY_COMMAND = (
    "$ErrorActionPreference='Stop'; "
    "$os=Get-CimInstance Win32_OperatingSystem; "
    "$perf=Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory; "
    "$pages=@(Get-CimInstance Win32_PageFileUsage); "
    "$used=($pages | Measure-Object -Property CurrentUsage -Sum).Sum; "
    "$total=($pages | Measure-Object -Property AllocatedBaseSize -Sum).Sum; "
    "if ($null -eq $used) { $used=0 }; "
    "if ($null -eq $total) { $total=0 }; "
    "Write-Output \"$($os.TotalVisibleMemorySize)|$($perf.FreeAndZeroPageListBytes)|"
    "$($perf.CacheBytes)|$($perf.PagesPersec)|$used|$total|"
    "$($perf.SystemCacheResidentBytes)|$($perf.StandbyCacheCoreBytes)|"
    "$($perf.StandbyCacheNormalPriorityBytes)|$($perf.StandbyCacheReserveBytes)\""
)
GUEST_MEMINFO_FIELDS = {
    "MemTotal": "actual",
    "MemAvailable": "usable",
    "AnonPages": "anon_pages",
    "Active(file)": "active_file",
    "Inactive(file)": "inactive_file",
    "SwapTotal": "swap_total",
    "SwapFree": "swap_free",
}

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

def virtio_mem_base_ram_kb(domain_xml, total_size_kb):
    domain_mem_kb = parse_xml_size_kb(domain_xml.find("./memory"))
    if domain_mem_kb <= 0 or domain_mem_kb < total_size_kb:
        raise ValueError("domain <memory> is missing or smaller than virtio-mem capacity")
    return domain_mem_kb - total_size_kb

class MemorySample(NamedTuple):
    used_ewma: float
    used: int
    error: float
    updated: int
    source: Optional[str] = None
    swap_free: Optional[int] = None
    sampled_at: Optional[float] = None
    major_fault: Optional[int] = None
    swap_out: Optional[int] = None

class ExitFailure(Exception):
    pass

def cleanup_libvirt(operation, *args):
    try:
        operation(*args)
    except libvirt.libvirtError as error:
        logging.debug("Libvirt cleanup failed: %s", error)

class LogFormatter(logging.Formatter):
    def formatMessage(self, record):
        if record.levelno == logging.INFO:
            return record.message
        return super().formatMessage(record)

class LibVirtAutoReclaim:
    poll_interval = 5
    conn = None
    config = None
    config_check_interval = 300
    agent_error_level = logging.WARNING

    def __init__(self, qemu_addr='qemu:///system', configfile='/etc/libvirt/autoreclaim.json'):
        self.configfile = configfile
        self.qemu_addr = qemu_addr
        self.monitored_vms = set()
        self.guest_agents = set()
        self.read_only_vms = set()
        self.vm_backends = {}
        self.guest_os = {}
        self.guest_os_retry_at = {}
        self.windows_agent_state = {}
        self.windows_exec_enabled = False
        self.stats_periods = {}
        self.last_poll_times = {}
        self.timer_id = None
        self.polling_active = False
        self.poll_due = False
        self.memory_targets = {}
        self.pending_targets = {}
        self.manual_increase_until = {}
        self.control_samples = {}
        self.config_mtime = None
        self.config_check_at = monotonic()
        libvirt.virEventRegisterDefaultImpl()
        logging.info("Connecting to libvirt at %s", qemu_addr)
        self.conn = libvirt.open(qemu_addr)
        if self.conn is None:
            raise ExitFailure('Failed to open connection to the hypervisor')
        try:
            self.__load_config()
        except (OSError, ValueError, KeyError, ExitFailure, KeyboardInterrupt, SystemExit):
            cleanup_libvirt(self.conn.close)
            raise
        self.agent_executor = ThreadPoolExecutor(
            max_workers=self.config.get("general", {}).get("agent_workers", 4),
            thread_name_prefix="guest-agent")

    def __load_config(self):
        stat_result = os.stat(self.configfile)
        mtime_ns = getattr(stat_result, "st_mtime_ns", None)
        mtime = mtime_ns if isinstance(mtime_ns, int) else stat_result.st_mtime
        if getattr(self, "config_mtime", None) == mtime:
            return False
        with open(self.configfile) as configfile:
            config = json.load(configfile, parse_int=int)
        self.__validate_config_parameters(config)
        changed = self.config != config
        if changed:
            action = "Loading" if self.config is None else "Reloading"
            logging.info("%s config file: %s", action, self.configfile)
            self.control_samples.clear()
        self.config = config
        self.config_mtime = mtime
        return changed

    def __validate_config_parameters(self, config):
        if (not isinstance(config, dict)
                or not isinstance(config.get("general", {}), dict)
                or not isinstance(config.get("default", {}), dict)
                or not isinstance(config.get("vms"), list)
                or any(not isinstance(vm, dict) for vm in config["vms"])):
            raise ExitFailure(
                "Invalid configuration: expected an object with a vms list of objects "
                "and optional general and default objects")
        general_keys = ("psi_threshold", "host_min_free_ratio", "agent_workers")
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
            if "agent_workers" in parameters:
                if scope != "general":
                    raise ExitFailure("agent_workers is only supported in general, not {}".format(scope))
                workers = parameters["agent_workers"]
                if (isinstance(workers, bool) or not isinstance(workers, int)
                        or not 1 <= workers <= 16):
                    raise ExitFailure("Invalid general agent_workers: expected an integer from 1 to 16")
            for key, minimum in (
                    ("windows_agent_interval", 1),
                    ("windows_paging_veto_pages_per_sec", 0),
                    ("balloon_major_fault_veto_per_sec", 0)):
                if key not in parameters:
                    continue
                value = parameters[key]
                if (isinstance(value, bool) or not isinstance(value, int) or value < minimum):
                    raise ExitFailure(
                        "Invalid {} {}: expected an integer >= {}".format(scope, key, minimum))
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
                "idle_reclaim_threshold": (0, 1, True),
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

    def __guest_agent_command(self, dom, command, arguments=None):
        import libvirt_qemu
        request = {"execute": command}
        if arguments is not None:
            request["arguments"] = arguments
        response = json.loads(libvirt_qemu.qemuAgentCommand(dom, json.dumps(request), 1, 0))
        if isinstance(response, dict) and "error" in response:
            raise ValueError("guest-agent {} failed: {}".format(command, response["error"]))
        if not isinstance(response, dict) or "return" not in response:
            raise ValueError("invalid guest-agent response for {}".format(command))
        return response["return"]

    def __identify_guest_os(self, dom):
        name = dom.name()
        now = monotonic()
        if (name not in self.guest_os
                or (self.guest_os[name] is None and now >= self.guest_os_retry_at[name])):
            try:
                info = self.__guest_agent_command(dom, "guest-get-osinfo")
                if not isinstance(info, dict) or not isinstance(info.get("id"), str) or not info["id"]:
                    raise ValueError("guest OS id is missing")
                self.guest_os[name] = "windows" if info["id"].lower() == "mswindows" else "other"
                self.guest_os_retry_at.pop(name, None)
                logging.debug("%s identified guest OS: %s (backend OS: %s)",
                              name, info["id"], self.guest_os[name])
            except (ImportError, libvirt.libvirtError, ValueError) as error:
                logging.log(self.agent_error_level,
                            "%s cannot identify guest OS; using balloon statistics and retrying "
                            "in %s seconds: %s", name, GUEST_OS_RETRY_INTERVAL, error)
                self.guest_os[name] = None
                self.guest_os_retry_at[name] = now + GUEST_OS_RETRY_INTERVAL
        return self.guest_os[name]

    def _get_windows_guest_meminfo(self, dom):
        if not self.windows_exec_enabled:
            return None
        return self._poll_windows_guest_meminfo(dom)

    def _get_windows_status_meminfo(self, dom):
        deadline = monotonic() + WINDOWS_STATUS_TIMEOUT
        name = dom.name()
        while True:
            state = self.windows_agent_state.setdefault(name, {"next_check": 0})
            state["next_check"] = 0
            sample = self._poll_windows_guest_meminfo(dom, single_shot=True)
            if sample is not None:
                return sample
            if state.get("pid") is None or state.get("blocked"):
                return None
            remaining = deadline - monotonic()
            if remaining <= 0:
                logging.debug("%s Windows status memory sample timed out after %s seconds",
                              name, WINDOWS_STATUS_TIMEOUT)
                return None
            sleep(min(0.1, remaining))

    def _poll_windows_guest_meminfo(self, dom, *, single_shot=False):
        name = dom.name()
        error_level = logging.DEBUG if single_shot else self.agent_error_level
        interval = self.__vm_config_for_name(name).get("windows_agent_interval", 10)
        windows_sample_max_age = max(30, interval * 3)
        state = self.windows_agent_state.setdefault(name, {"next_check": 0})
        now = monotonic()
        if now >= state["next_check"] and not state.get("blocked"):
            state["next_check"] = now + interval
            pid = state.get("pid")
            if pid is not None:
                try:
                    result = self.__guest_agent_command(dom, "guest-exec-status", {"pid": pid})
                    if not isinstance(result, dict) or not isinstance(result.get("exited"), bool):
                        raise ValueError("invalid guest-exec-status response")
                    if result["exited"]:
                        state.pop("pid")
                        if result.get("exitcode") != 0 or result.get("out-truncated"):
                            raise ValueError("PowerShell failed or its output was truncated")
                        output = base64.b64decode(result["out-data"], validate=True).decode("utf-8")
                        values = [int(value) for value in output.strip().split("|")]
                        if (len(values) != 10 or values[0] <= 0
                                or not 0 <= values[1] <= values[0] * 1024
                                or any(value < 0 for value in values[2:])
                                or values[4] > values[5]):
                            raise ValueError("invalid Windows memory counters")
                        state["sample"] = {
                            "actual": values[0],
                            "usable": min(values[0], (values[1] + values[8] + values[9]) // 1024),
                            "cache_kb": values[2] // 1024, "pages_per_sec": values[3],
                            "protected_cache_kb": (values[6] + values[7]) // 1024,
                            "standby_cache_kb": (values[8] + values[9]) // 1024,
                            "pagefile_used_kb": values[4] * SZ_1MiB,
                            "swap_total": values[5] * SZ_1MiB,
                            "swap_free": (values[5] - values[4]) * SZ_1MiB,
                            "last_update": state["started_wall"],
                            "sampled_at": state["started_at"], "source": "guest-agent",
                        }
                        logging.debug("%s Windows memory: cache=%s KiB protected_cache=%s KiB "
                                      "standby_cache=%s KiB pages/sec=%s pagefile=%s KiB",
                                      name, values[2] // 1024, (values[6] + values[7]) // 1024,
                                      (values[8] + values[9]) // 1024, values[3], values[4] * SZ_1MiB)
                except (ImportError, libvirt.libvirtError, KeyError, TypeError, ValueError) as error:
                    logging.log(error_level, "%s cannot read Windows guest-exec result: %s", name, error)
            if state.get("pid") is None and not (single_shot and "started_at" in state):
                try:
                    result = self.__guest_agent_command(dom, "guest-exec", {
                        "path": "powershell.exe",
                        "arg": ["-NoProfile", "-NonInteractive", "-Command", WINDOWS_MEMORY_COMMAND],
                        "capture-output": True,
                    })
                    if (not isinstance(result, dict) or isinstance(result.get("pid"), bool)
                            or not isinstance(result.get("pid"), int) or result["pid"] <= 0):
                        raise ValueError("guest-exec did not return a valid PID")
                    state.update(pid=result["pid"], started_at=now, started_wall=int(time()))
                except (ImportError, libvirt.libvirtError, ValueError) as error:
                    state["blocked"] = True
                    logging.log(error_level, "%s cannot start Windows guest-exec; using balloon statistics "
                                "until restart: %s", name, error)
        sample = state.get("sample")
        if sample is not None and 0 <= now - sample["sampled_at"] <= windows_sample_max_age:
            return sample
        return None

    def _read_guest_agent_file(self, dom, path):
        name = dom.name()
        handle = None
        try:
            import libvirt_qemu
            command = json.dumps({
                "execute": "guest-file-open",
                "arguments": {"path": path, "mode": "r"},
            })
            handle = json.loads(libvirt_qemu.qemuAgentCommand(dom, command, 1, 0))["return"]
            count = 16384
            command = json.dumps({
                "execute": "guest-file-read",
                "arguments": {"handle": handle, "count": count},
            })
            result = json.loads(libvirt_qemu.qemuAgentCommand(dom, command, 1, 0))["return"]
            encoded = result.get("buf-b64", "") if isinstance(result, dict) else None
            if not isinstance(encoded, str):
                raise ValueError("invalid guest-file-read response")
            data = base64.b64decode(encoded, validate=True)
            if len(data) >= count and not result.get("eof"):
                raise ValueError("truncated procfs read")
            return data.decode("utf-8")
        except (ImportError, libvirt.libvirtError, KeyError, ValueError) as error:
            logging.debug("%s guest-agent read of %s failed: %s", name, path, error)
        finally:
            if handle is not None:
                try:
                    command = json.dumps({
                        "execute": "guest-file-close",
                        "arguments": {"handle": handle},
                    })
                    libvirt_qemu.qemuAgentCommand(dom, command, 1, 0)
                except (ImportError, libvirt.libvirtError) as error:
                    logging.debug("%s guest-agent close of %s failed: %s", name, path, error)
        return None

    def _get_guest_agent_meminfo(self, dom):
        data = self._read_guest_agent_file(dom, "/proc/meminfo")
        if data is None:
            return None
        stats = {}
        for line in data.splitlines():
            label, _, value = line.partition(":")
            key = GUEST_MEMINFO_FIELDS.get(label)
            if key is not None:
                try:
                    amount, unit = value.split()
                    amount = int(amount)
                    if unit != "kB" or amount < 0:
                        raise ValueError("expected nonnegative kB")
                    stats[key] = amount
                except ValueError as error:
                    logging.debug("%s invalid guest %s: %s", dom.name(), label, error)
        total, available = stats.get("actual", 0), stats.get("usable", -1)
        if total <= 0 or not 0 <= available <= total:
            logging.debug("%s guest meminfo lacks valid MemTotal/MemAvailable", dom.name())
            return None
        return {**stats, "last_update": int(time()), "source": "guest-agent",
                "sampled_at": monotonic()}

    def _get_guest_agent_memory_pressure(self, dom):
        data = self._read_guest_agent_file(dom, "/proc/pressure/memory")
        averages = {}
        if data is not None:
            for line in data.splitlines():
                fields = line.split()
                if not fields or fields[0] not in ("some", "full"):
                    continue
                try:
                    value = next(field[6:] for field in fields[1:] if field.startswith("avg10="))
                    average = float(value)
                    if not 0 <= average <= 100:
                        raise ValueError("avg10 out of range")
                    averages[fields[0]] = average
                except (StopIteration, ValueError):
                    logging.debug("%s invalid guest memory PSI %s avg10", dom.name(), fields[0])
        return averages.get("some"), averages.get("full")

    def _get_guest_memstats(self, dom):
        name = dom.name()
        backend = self.vm_backends.get(name, {})
        guest_os = backend.get("os", "other")
        guest_stats = None
        if backend.get("has_guest_agent") or name in self.guest_agents or name in self.read_only_vms:
            if guest_os is None:
                guest_os = self.__identify_guest_os(dom)
                backend["os"] = guest_os
            if guest_os == "windows":
                guest_stats = self._get_windows_guest_meminfo(dom)
            elif guest_os is not None:
                guest_stats = self._get_guest_agent_meminfo(dom)
        return guest_stats

    def _get_domain_memstats(self, dom, *, guest_stats=None, query_guest=True):
        name = dom.name()
        backend = self.vm_backends.get(name, {})
        if query_guest:
            guest_stats = self._get_guest_memstats(dom)
        if name in self.read_only_vms:
            return guest_stats or {}
        allocated_actual = None
        if backend.get("type") == "virtio-mem":
            vmem_info = self._inspect_virtio_mem_devices(dom)
            if vmem_info is not None:
                allocated_actual = vmem_info["base_ram_kb"] + vmem_info["total_current_kb"]
        if guest_stats is not None:
            if backend.get("os") == "windows" and allocated_actual is None:
                try:
                    allocated_actual = dom.memoryStats().get("actual", 0)
                except libvirt.libvirtError as error:
                    logging.warning("%s cannot read Windows hypervisor memory allocation: %s", name, error)
                    return {}
                if allocated_actual <= 0:
                    logging.warning("%s Windows hypervisor memory allocation is missing", name)
                    return {}
            if allocated_actual is None:
                return guest_stats
            return {
                **guest_stats,
                "actual": allocated_actual,
                "usable": min(allocated_actual, guest_stats["usable"]),
            }
        try:
            memstat = dom.memoryStats()
        except libvirt.libvirtError as error:
            logging.debug("%s balloon memory stats failed: %s", name, error)
            memstat = {}
        actual = allocated_actual if allocated_actual is not None else memstat.get("actual", 0)
        usable = memstat.get("usable", -1)
        updated = memstat.get("last_update", 0)
        if actual > 0 and 0 <= usable <= actual and updated > 0 and 0 <= time() - updated <= 15:
            return {
                "actual": actual,
                "usable": usable,
                "last_update": updated,
                "source": "balloon",
                **{key: memstat[key] for key in ("swap_in", "swap_out", "major_fault")
                   if key in memstat},
            }
        return {**memstat, "actual": actual} if memstat else {}

    def _inspect_virtio_mem_devices(self, dom):
        try:
            domain_xml = ET.fromstring(dom.XMLDesc(0))
            vmem_nodes = domain_xml.findall("./devices/memory[@model='virtio-mem']")
            if not vmem_nodes:
                return None
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
            base_ram_kb = virtio_mem_base_ram_kb(domain_xml, total_size_kb)
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

    def _memory_bounds(self, dom, backend_type, maximum_ram):
        if backend_type == "virtio-mem":
            vmem_info = self._inspect_virtio_mem_devices(dom)
            if vmem_info is not None:
                return vmem_info["base_ram_kb"], vmem_info["max_ram_kb"]
        return 0, maximum_ram

    def _memory_floors(self, dom, memstat, parameters, backend_type, bounds):
        minimum_ram, maximum_ram = bounds
        min_reserve = self.dom_keep_usable(dom, 0)
        denominator = max(0.1, 1 - parameters.get("keep_free_ratio", 0.1))
        static_floor = min(maximum_ram, max(minimum_ram, int(2 * min_reserve / denominator)))
        anon_pages, active_file = memstat.get("anon_pages"), memstat.get("active_file")
        if anon_pages is not None and active_file is not None:
            protected_kb = anon_pages + active_file
        elif memstat.get("actual", 0) > 0 and memstat.get("usable", -1) >= 0:
            protected_kb = max(0, memstat["actual"] - memstat["usable"])
        else:
            return static_floor, static_floor
        guest_floor = min(maximum_ram, math.ceil((protected_kb + min_reserve) / denominator))
        if guest_floor > static_floor and backend_type == "balloon":
            guest_floor = min((maximum_ram // SZ_4KiB) * SZ_4KiB,
                              ((guest_floor + SZ_4KiB - 1) // SZ_4KiB) * SZ_4KiB)
        return static_floor, max(static_floor, guest_floor)

    def dom_status(self, dom, *, memstat=None):
        name = dom.name()
        backend = self.vm_backends.get(name, {"type": "balloon"})
        if memstat is None:
            if backend.get("os") == "windows" and (
                    backend.get("has_guest_agent") or name in self.guest_agents or name in self.read_only_vms):
                guest_stats = self._get_windows_status_meminfo(dom)
                memstat = self._get_domain_memstats(dom, guest_stats=guest_stats, query_guest=False)
            else:
                memstat = self._get_domain_memstats(dom)
        actual = memstat.get("actual", 0)
        usable = memstat.get("usable", 0)
        used = actual - usable
        total_ram = dom.info()[1]
        keep_usable = self.dom_keep_usable(dom, actual)
        parameters = self.__vm_config_for_name(name)
        memory_weight = parameters.get("memory_weight", 100)
        stats_method = memstat.get("source", "unavailable")
        memory_method = "read-only" if name in self.read_only_vms else backend["type"]
        stats_label = "agent" if stats_method == "guest-agent" else stats_method
        backend_label = (
            "balloon" if stats_label == memory_method == "balloon"
            else f"{stats_label}/{memory_method}")
        floor = psi = swap_used = "n/a"
        if memory_method != "read-only":
            bounds = self._memory_bounds(dom, memory_method, total_ram)
            _, reclaim_floor = self._memory_floors(dom, memstat, parameters, memory_method, bounds)
            floor = str(reclaim_floor // SZ_1MiB)
        if stats_method == "guest-agent":
            if backend.get("os") != "windows":
                psi_some, _ = self._get_guest_agent_memory_pressure(dom)
                if psi_some is not None:
                    psi = f"{psi_some:.2f}".rstrip("0").rstrip(".") + "%"
            swap_total, swap_free = memstat.get("swap_total"), memstat.get("swap_free")
            if swap_total is not None and swap_free is not None and 0 <= swap_free <= swap_total:
                swap_used = str((swap_total - swap_free) // SZ_1MiB)
            elif swap_total is not None and swap_free is not None:
                logging.debug("%s invalid guest swap totals", name)
        return (
            name,
            backend_label,
            str(int(total_ram / SZ_1MiB)),
            str(int(actual / SZ_1MiB)),
            str(int(used / SZ_1MiB)),
            str(int(usable / SZ_1MiB)),
            str(int(keep_usable / SZ_1MiB)),
            str(memory_weight),
            floor,
            psi,
            swap_used,
        )

    def status(self):
        self.dom_print_names()
        domainIDs = self.conn.listDomainsID()
        if domainIDs is None:
            raise ExitFailure('No active domains')
        rows = [(
            "Domain", "Backend", "Total", "Actual",
            "Used", "Usable", "Keep_Usable", "Weight", "Floor", "PSI", "Swap_Used",
        )]
        domains = []
        for domainID in domainIDs:
            dom = self.conn.lookupByID(domainID)
            if dom.name() in self.monitored_vms:
                domains.append(dom)
        futures = {}
        windows_samples = {}
        try:
            for dom in domains:
                name = dom.name()
                backend = self.vm_backends.get(name, {})
                if name not in futures and backend.get("os") == "windows" and (
                        backend.get("has_guest_agent") or name in self.guest_agents
                        or name in self.read_only_vms):
                    futures[name] = self.agent_executor.submit(self._get_windows_status_meminfo, dom)
            for name, future in futures.items():
                windows_samples[name] = future.result()
        finally:
            for future in futures.values():
                future.cancel()
            wait(futures.values())
        for dom in domains:
            name = dom.name()
            if name in windows_samples:
                memstat = self._get_domain_memstats(
                    dom, guest_stats=windows_samples[name], query_guest=False)
                rows.append(self.dom_status(dom, memstat=memstat))
            else:
                rows.append(self.dom_status(dom))
        widths = [max(len(value) for value in column) for column in zip(*rows)]
        for row in rows:
            print("  ".join(
                value.ljust(width) if index < 2 else value.rjust(width)
                for index, (value, width) in enumerate(zip(row, widths))
            ).rstrip(), flush=True)

    def process_domainID(self, dom, host_pressure=None, memstat=None):
        name = dom.name()
        if name not in self.monitored_vms or name in self.read_only_vms:
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
        windows_sample_max_age = max(30, parameters.get("windows_agent_interval", 10) * 3)
        max_age = (windows_sample_max_age
                   if backend.get("os") == "windows" and memstat.get("source") == "guest-agent"
                   else 15)
        if not 0 <= time() - updated <= max_age:
            logging.warning("%s has stale or future-dated memory stats, skipping", name)
            self.control_samples.pop(name, None)
            return
        previous = self.control_samples.get(name)
        source = memstat.get("source")
        sampled_at = memstat.get("sampled_at") if source == "guest-agent" else None
        source_changed = previous is not None and previous.source != source
        if source_changed:
            previous = None
        sample_interval = 0.0
        if previous is not None:
            if sampled_at is not None and previous.sampled_at is not None:
                sample_interval = sampled_at - previous.sampled_at
            else:
                sample_interval = updated - previous.updated
            if sample_interval == 0:
                logging.debug("%s memory stats have not updated, skipping", name)
                return
        bounds = self._memory_bounds(dom, backend["type"], dom.info()[1])
        _, max_allowed_ram = bounds
        previous_memory = self.memory_targets.get(name)
        own_growth = pending_target is not None and actual <= pending_target + SZ_4KiB
        external_increase = (
            previous_memory is not None
            and not source_changed
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
            logging.info(
                "%s unattributed memory increase: %s -> %s KiB; postponing reductions for %s seconds",
                name, previous_memory, actual, grace)
        swap_total, swap_free = memstat.get("swap_total"), memstat.get("swap_free")
        if swap_total is None or swap_free is None or not 0 <= swap_free <= swap_total:
            swap_free = None
        swap_drop = 0
        swap_rate = 0.0
        if (previous is not None and sample_interval > 0
                and swap_free is not None and previous.swap_free is not None):
            swap_drop = previous.swap_free - swap_free
            swap_rate = swap_drop / sample_interval
        if external_increase or sample_interval < 0:
            previous = None
        major_fault = memstat.get("major_fault") if source == "balloon" else None
        swap_out = memstat.get("swap_out") if source == "balloon" else None
        major_fault_rate = 0.0
        swap_out_delta = 0
        if previous is not None and sample_interval > 0:
            if major_fault is not None and previous.major_fault is not None:
                major_fault_rate = max(0, major_fault - previous.major_fault) / sample_interval
            if swap_out is not None and previous.swap_out is not None:
                swap_out_delta = max(0, swap_out - previous.swap_out)
        used = actual - usable
        alpha = parameters.get("ewma_alpha", 0.3)
        used_ewma = used if previous is None else alpha * used + (1 - alpha) * previous.used_ewma
        reserve = self.dom_keep_usable(dom, actual)
        error = reserve - (actual - used_ewma)
        derivative = 0
        used_rate = 0
        elapsed = self.stats_periods.get(name, self.poll_interval)
        if previous is not None:
            elapsed = max(1, sample_interval)
            derivative = (error - previous.error) / elapsed
            used_rate = (used - previous.used) / elapsed
        self.control_samples[name] = MemorySample(
            used_ewma, used, error, updated, source, swap_free, sampled_at, major_fault, swap_out)
        derivative_term = max(
            -abs(error), min(abs(error), parameters.get("pd_kd", 1.0) * derivative))
        delta = parameters.get("pd_kp", 0.5) * error + derivative_term
        pages_per_sec = memstat.get("pages_per_sec") if backend.get("os") == "windows" else None
        emergency = (usable < reserve or used_rate > actual * 0.05
                     or swap_rate > GUEST_SWAP_EMERGENCY_KIB_PER_SEC)
        reduction_postponed = now < self.manual_increase_until.get(name, 0)
        reclaim_pressure = 0.0
        if delta < 0 and not emergency and not reduction_postponed:
            if host_pressure is None:
                host_pressure = self.host_memory_pressure()
            if (host_pressure != HOST_PRESSURE_UNAVAILABLE
                    and usable - reserve > actual * parameters.get("idle_reclaim_threshold", 0.65)):
                host_pressure = 1.0
            reclaim_pressure = max(0.0, host_pressure)
        psi_some = psi_full = None
        low_headroom = usable < 2 * reserve
        if (source == "guest-agent" and backend.get("os") != "windows"
                and (low_headroom or reclaim_pressure > 0)):
            psi_some, psi_full = self._get_guest_agent_memory_pressure(dom)
        emergency = emergency or (low_headroom and psi_full is not None and psi_full > 1.0)
        if emergency:
            delta = max(delta, reserve - usable + max(0, used_rate) * self.poll_interval,
                        actual * 0.25)
        elif low_headroom and psi_some is not None and psi_some > 10.0:
            delta = min(actual * 0.5, max(delta, actual * 0.1))
        elif error <= 0:
            delta = min(delta, 0)
        elif delta > 0:
            delta = min(delta, actual * 0.5)
        floor, reclaim_floor = self._memory_floors(dom, memstat, parameters, backend["type"], bounds)
        dynamic_floor = 0
        if delta < 0:
            if reduction_postponed:
                logging.debug("%s reduction postponed after an unattributed memory increase", name)
                return
            idle_ratio = usable / actual
            weight_multiplier = min(2.0, 100.0 / parameters.get("memory_weight", 100))
            reclaim_limit = (
                actual * parameters.get("reclaim_ratio", 0.015) * elapsed * reclaim_pressure * idle_ratio
                * weight_multiplier)
            guest_factor = 1.0
            swap_active = (swap_drop >= GUEST_SWAP_VETO_MIN_DROP_KIB
                           and swap_rate > GUEST_SWAP_VETO_KIB_PER_SEC)
            if (swap_active or swap_out_delta > 0
                    or major_fault_rate > parameters.get("balloon_major_fault_veto_per_sec", 50)
                    or (psi_full is not None and psi_full > 0.1)
                    or (psi_some is not None and psi_some > 2.0)
                    or (pages_per_sec is not None
                        and pages_per_sec > parameters.get("windows_paging_veto_pages_per_sec", 1000))):
                guest_factor = 0.0
            elif psi_some is not None and psi_some > 0.5:
                guest_factor = 1 - psi_some / 2.0
            if guest_factor == 0:
                logging.debug("%s reclaim blocked by guest: swap_drop=%s psi_some=%s psi_full=%s "
                              "pages_per_sec=%s swap_out_delta=%s major_fault_rate=%.1f/s",
                              name, swap_drop, psi_some, psi_full, pages_per_sec,
                              swap_out_delta, major_fault_rate)
                return
            surplus = max(0, min(usable - reserve, -error))
            delta = -min(-delta, reclaim_limit * guest_factor, surplus)
            if delta < 0 and floor <= actual and reclaim_floor > floor:
                if reclaim_floor >= actual:
                    logging.debug("%s guest floor prevents reclaim: %s KiB", name, reclaim_floor)
                    return
                floor = dynamic_floor = reclaim_floor
        target = min(max_allowed_ram, max(floor, actual + int(delta)))
        if pending_target is not None and not (emergency and target > max(actual, pending_target)):
            logging.debug("%s waiting for memory target %s KiB; actual=%s KiB",
                          name, pending_target, actual)
            return
        logging.debug(
            "%s [%s/%s] actual=%s usable=%s used_ewma=%.1f reserve=%.1f error=%.1f "
            "derivative=%.1f used_rate=%.1f swap_drop_rate=%.1f psi_some=%s psi_full=%s "
            "emergency=%s floor=%s target=%s KiB",
            name, source or "unavailable", backend["type"], actual, usable, used_ewma, reserve, error,
            derivative, used_rate, swap_rate, psi_some, psi_full, emergency, floor, target)
        if backend["type"] == "virtio-mem":
            requested = dom_virtio_mem(dom, target, actual, minimum_ram_kb=dynamic_floor)
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
            return HOST_PRESSURE_UNAVAILABLE
        parameters = self.config.get("general", {})
        psi_threshold = parameters.get("psi_threshold", 5)
        host_min_free_ratio = parameters.get("host_min_free_ratio", 0.2)
        return max(min(pressure / psi_threshold, 1),
                   max(0, 1 - available / (total * host_min_free_ratio)))

    def __sample_domain(self, dom, name):
        started = monotonic()
        try:
            return started, self._get_guest_memstats(dom)
        except libvirt.libvirtError as error:
            logging.warning("%s stopped or unavailable, skipping: %s", name, error)
            return started, None

    def process_domains(self):
        domainIDs = self.conn.listDomainsID()
        if domainIDs is None:
            raise ExitFailure('Cannot list active domains')
        domains = []
        scheduled_names = set()
        for domainID in domainIDs:
            try:
                dom = self.conn.lookupByID(domainID)
                name = dom.name()
                if name not in self.monitored_vms or name in self.read_only_vms:
                    continue
                period = self.stats_periods.get(name)
                if period is None:
                    logging.warning("%s has no stats period, skipping", name)
                    continue
                last_poll = self.last_poll_times.get(name)
                if last_poll is not None and monotonic() - last_poll < period:
                    continue
                if name in scheduled_names:
                    continue
                scheduled_names.add(name)
                domains.append((name, dom))
            except libvirt.libvirtError as error:
                logging.warning("%s stopped or unavailable, skipping: %s", domainID, error)

        sampled_domains = []
        if domains:
            futures = []
            guest_samples = {}
            try:
                for name, dom in domains:
                    if self.vm_backends.get(name, {}).get("has_guest_agent") or name in self.guest_agents:
                        futures.append((name, self.agent_executor.submit(self.__sample_domain, dom, name)))
                for name, future in futures:
                    guest_samples[name] = future.result()
            finally:
                # Drain workers before any connection cleanup or other domain RPCs.
                for _, future in futures:
                    future.cancel()
                wait([future for _, future in futures])
            for name, dom in domains:
                started, guest_stats = guest_samples.get(name, (monotonic(), None))
                self.last_poll_times[name] = started
                try:
                    memstat = self._get_domain_memstats(dom, guest_stats=guest_stats, query_guest=False)
                except libvirt.libvirtError as error:
                    logging.warning("%s stopped or unavailable, skipping: %s", name, error)
                    continue
                actual = memstat.get("actual", 0)
                idle_ratio = max(0, memstat.get("usable", 0)) / actual if actual > 0 else 0
                memory_weight = self.__vm_config_for_name(name).get("memory_weight", 100)
                yield_score = idle_ratio / (memory_weight / 100.0)
                sampled_domains.append((yield_score, dom, memstat))
        pressure = self.host_memory_pressure() if sampled_domains else 0.0
        for _, dom, memstat in sorted(sampled_domains, key=lambda entry: entry[0], reverse=True):
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
        interval = math.gcd(*self.stats_periods.values()) if self.stats_periods else 5
        active = bool(self.stats_periods)
        timer_changed = interval != self.poll_interval or active != self.polling_active
        if active != self.polling_active:
            self.polling_active = active
            self.poll_due = active
            logging.info("VM polling %s", "resumed" if active else "suspended")
        if interval != self.poll_interval:
            self.poll_interval = interval
            logging.info("Polling interval changed to %s seconds", interval)
        if timer_changed and self.timer_id is not None:
            libvirt.virEventUpdateTimeout(
                self.timer_id, interval * 1000 if active else -1)

    def __forget_domain(self, name):
        if name in self.monitored_vms and name not in self.read_only_vms:
            logging.info("Stopped monitoring %s", name)
        self.monitored_vms.discard(name)
        self.guest_agents.discard(name)
        self.read_only_vms.discard(name)
        self.vm_backends.pop(name, None)
        self.stats_periods.pop(name, None)
        self.last_poll_times.pop(name, None)
        self.memory_targets.pop(name, None)
        self.pending_targets.pop(name, None)
        self.manual_increase_until.pop(name, None)
        self.control_samples.pop(name, None)

    def __refresh_domain(self, dom):
        name = dom.name()
        was_polled = name in self.monitored_vms and name not in self.read_only_vms
        try:
            active = dom.isActive()
        except libvirt.libvirtError as error:
            logging.debug("%s cannot determine active state for monitoring: %s", name, error)
            return
        if active and self.__can_monitor_domain(dom):
            if not was_polled and name not in self.read_only_vms:
                logging.info("Monitoring %s", name)
            if name in self.read_only_vms:
                self.last_poll_times.pop(name, None)
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
            logging.warning("Not monitoring %s: Cannot inspect balloon definition: %s", name, error)
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
                logging.warning(
                    "Not monitoring %s: virtio-mem requires valid balloon stats or the QEMU Guest Agent",
                    name)
                return False
            self.vm_backends[name] = {
                "type": "virtio-mem",
                "device_count": len(vmem_devices),
                "has_balloon_stats": stats_period is not None,
                "has_guest_agent": has_guest_agent,
                "os": self.__identify_guest_os(dom) if has_guest_agent else None,
            }
            if has_guest_agent:
                self.guest_agents.add(name)
            self.__set_stats_period_if_active(dom, stats_period if stats_period is not None else 5)
            return True
        if not balloon_enabled:
            if has_spice_channel and has_guest_agent:
                self.guest_agents.add(name)
                self.read_only_vms.add(name)
                self.vm_backends[name] = {
                    "type": "balloon", "has_guest_agent": True,
                    "os": self.__identify_guest_os(dom),
                }
                self.memory_targets.pop(name, None)
                self.pending_targets.pop(name, None)
                self.manual_increase_until.pop(name, None)
                self.control_samples.pop(name, None)
                return True
            logging.warning("Not monitoring %s: balloon device is missing or disabled", name)
            return False
        if stats_period is None or not 0 < stats_period <= 5:
            if not has_guest_agent:
                if raw_period is not None:
                    logging.warning("Not monitoring %s: invalid balloon stats period %r, expected 1 to 5",
                                    name, raw_period)
                return False
            stats_period = 5
        if balloon.get("autodeflate") != "on":
            logging.warning("%s: autodeflate='on' is recommended, but is not set", name)
        if has_guest_agent:
            self.guest_agents.add(name)
        self.vm_backends[name] = {
            "type": "balloon",
            "device_count": 1,
            "has_balloon_stats": stats_period is not None,
            "has_guest_agent": has_guest_agent,
            "os": self.__identify_guest_os(dom) if has_guest_agent else None,
        }
        self.__set_stats_period_if_active(dom, stats_period)
        return True

    def __domain_lifecycle(self, conn, dom, event, detail, opaque):
        try:
            if event in (libvirt.VIR_DOMAIN_EVENT_STOPPED, libvirt.VIR_DOMAIN_EVENT_CRASHED):
                self.guest_os.pop(dom.name(), None)
                self.guest_os_retry_at.pop(dom.name(), None)
                self.windows_agent_state.pop(dom.name(), None)
                self.__forget_domain(dom.name())
            elif event == libvirt.VIR_DOMAIN_EVENT_STARTED:
                self.guest_os.pop(dom.name(), None)
                self.guest_os_retry_at.pop(dom.name(), None)
                self.windows_agent_state.pop(dom.name(), None)
                self.control_samples.pop(dom.name(), None)
                self.last_poll_times.pop(dom.name(), None)
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
                if event == libvirt.VIR_DOMAIN_EVENT_RESUMED:
                    self.last_poll_times.pop(dom.name(), None)
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
        self.poll_due = self.polling_active
        if not self.polling_active:
            return
        now = monotonic()
        if now - getattr(self, "config_check_at", 0) >= self.config_check_interval:
            self.config_check_at = now
            try:
                changed = self.__load_config()
            except (OSError, ValueError, KeyError, ExitFailure) as error:
                logging.error("Cannot reload configuration %s; keeping previous configuration: %s",
                              self.configfile, error)
            else:
                if changed:
                    self.dom_print_names()

    def dom_keep_usable(self, dom, actual=0):
        name = dom.name()
        keep_usable = SZ_512MiB
        keep_free_kb = self.__vm_config_for_name(name).get("keep_free_kb")
        if keep_free_kb:
            keep_usable = int(keep_free_kb)
        return max(keep_usable, int(actual * self.__vm_config_for_name(name).get("keep_free_ratio", 0.1)))

    def daemon(self):
        logging.info("Starting daemon")
        self.reconnect_delay = 1
        self.windows_exec_enabled = True
        try:
            while True:
                try:
                    if self.conn is None:
                        self.__reconnect()
                    self.__run_connection()
                except libvirt.libvirtError as error:
                    if self.conn is not None:
                        cleanup_libvirt(self.conn.close)
                        self.conn = None
                    logging.error("Libvirt connection failed: %s; reconnecting in %s seconds",
                                  error, self.reconnect_delay)
                    sleep(self.reconnect_delay)
                    self.reconnect_delay = min(RECONNECT_MAX_DELAY, self.reconnect_delay * 2)
        finally:
            self.windows_exec_enabled = False
            self.agent_executor.shutdown(wait=True)
            if self.timer_id is not None:
                cleanup_libvirt(libvirt.virEventRemoveTimeout, self.timer_id)
                self.timer_id = None

    def __reconnect(self):
        conn = libvirt.open(self.qemu_addr)
        if conn is None:
            raise libvirt.libvirtError("Failed to reopen connection to the hypervisor")
        self.conn = conn
        logging.info("Reconnected to libvirt at %s", self.qemu_addr)

    def __run_connection(self):
        callback_ids = []
        try:
            callback_ids.append(self.conn.domainEventRegisterAny(
                None, libvirt.VIR_DOMAIN_EVENT_ID_LIFECYCLE, self.__domain_lifecycle, None))
            if hasattr(libvirt, "VIR_DOMAIN_EVENT_ID_MEMORY_DEVICE_SIZE_CHANGE"):
                callback_ids.append(self.conn.domainEventRegisterAny(
                    None,
                    libvirt.VIR_DOMAIN_EVENT_ID_MEMORY_DEVICE_SIZE_CHANGE,
                    self.__memory_device_size_change,
                    None,
                ))
            # Lifecycle events may have been missed while disconnected.
            self.last_poll_times.clear()
            self.dom_print_names()
            self.poll_due = self.polling_active
            if self.timer_id is None:
                self.timer_id = libvirt.virEventAddTimeout(
                    self.poll_interval * 1000 if self.polling_active else -1,
                    self.__poll_timeout, None)
            self.reconnect_delay = 1
            while True:
                if self.poll_due:
                    self.poll_due = False
                    self.process_domains()
                    self.reconnect_delay = 1
                if libvirt.virEventRunDefaultImpl() < 0:
                    raise ExitFailure("Libvirt event loop failed")
        finally:
            for callback_id in callback_ids:
                cleanup_libvirt(self.conn.domainEventDeregisterAny, callback_id)

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
        logging.info("Shrink dom (balloon): %s %s - %s = %s MiB", name, actual_m, diff_m, restrict_to_m)
    else:
        logging.info("Grow dom (balloon): %s %s + %s = %s MiB", name, actual_m, diff_m, restrict_to_m)
    dom.setMemory(restrict_to)
    return restrict_to

def dom_virtio_mem(dom, target_total_kb, actual_total_kb, *, minimum_ram_kb=0):
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
        if total_size_kb <= 0 or not devices:
            return None
        base_ram_kb = virtio_mem_base_ram_kb(domain_xml, total_size_kb)
    except (TypeError, ValueError) as error:
        logging.error("%s cannot parse virtio-mem sizes: %s", name, error)
        return None
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
    if new_total_ram < minimum_ram_kb:
        # Rounding to the nearest blocks must not lower a guest-backed floor.
        for index, (device, requested_kb) in enumerate(updates):
            block = device["block_kb"]
            if requested_kb + block <= device["size_kb"]:
                updates[index] = (device, requested_kb + block)
                new_total_ram += block
                if new_total_ram >= minimum_ram_kb:
                    break
        else:
            logging.debug("%s cannot reach guest memory floor at device block granularity", name)
            return None
        if target_total_kb < actual_total_kb < new_total_ram:
            return None
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
        logging.info("Shrink dom (virtio-mem, %s dev): %s %s - %s = %s MiB",
                     len(devices), name, actual_mib, difference_mib, target_mib)
    else:
        logging.info("Grow dom (virtio-mem, %s dev): %s %s + %s = %s MiB",
                     len(devices), name, actual_mib, difference_mib, target_mib)
    return new_total_ram

def handle_sigterm(signum, _frame):
    raise SystemExit(128 + signum)

def report_libvirt_error(context, error):
    code, _, message, level = error[:4]
    if (code == libvirt.VIR_ERR_AGENT_UNRESPONSIVE
            or code == getattr(libvirt, "VIR_ERR_AGENT_COMMAND_TIMEOUT", None)
            or (context == "status"
                and code == getattr(libvirt, "VIR_ERR_AGENT_COMMAND_FAILED", None))):
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
        libvirt.registerErrorHandler(report_libvirt_error, args.action)
        lv_ctrl = LibVirtAutoReclaim(configfile=args.config)
        try:
            if args.action == "start":
                lv_ctrl.daemon()
            elif args.action == "status":
                lv_ctrl.agent_error_level = logging.DEBUG
                lv_ctrl.status()
        finally:
            lv_ctrl.agent_executor.shutdown(wait=True)
            if lv_ctrl.conn is not None:
                cleanup_libvirt(lv_ctrl.conn.close)
    except KeyboardInterrupt:
        raise SystemExit(128 + signal.SIGINT) from None
    finally:
        libvirt.registerErrorHandler(None, None)
        signal.signal(signal.SIGTERM, previous_sigterm)

def main(argv):
    libvirt_autoreclaim(argv)

if __name__ == '__main__':
    main(sys.argv)
