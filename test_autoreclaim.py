import importlib.util
import io
import base64
import json
import sys
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import Mock, mock_open, patch


sys.modules.setdefault("libvirt", types.SimpleNamespace(
    libvirtError=type("LibvirtError", (Exception,), {})))
spec = importlib.util.spec_from_file_location("autoreclaim", "libvirt-autoreclaim.py")
autoreclaim = importlib.util.module_from_spec(spec)
spec.loader.exec_module(autoreclaim)


class Domain:
    def __init__(self, name="guest-a"):
        self.domain_name = name
        self.memory = 1048576
        self.usable = 524288
        self.maximum = 8388608
        self.updated = 100
        self.calls = []

    def name(self):
        return self.domain_name

    def info(self):
        return (1, self.maximum, self.memory, 1, 0)

    def memoryStats(self):
        return {"actual": self.memory, "usable": self.usable, "last_update": self.updated}

    def setMemory(self, target):
        self.calls.append(target)
        self.usable += target - self.memory
        self.memory = target


class LogFormattingTests(unittest.TestCase):
    def test_only_info_messages_omit_level_prefix(self):
        formatter = autoreclaim.LogFormatter("%(levelname)s %(message)s")
        for level, expected in (
                (autoreclaim.logging.INFO, "Monitoring guest-a"),
                (autoreclaim.logging.DEBUG, "DEBUG Monitoring guest-a"),
                (autoreclaim.logging.WARNING, "WARNING Monitoring guest-a"),
                (autoreclaim.logging.ERROR, "ERROR Monitoring guest-a")):
            with self.subTest(level=level):
                record = autoreclaim.logging.LogRecord(
                    "autoreclaim", level, __file__, 1, "Monitoring %s", ("guest-a",), None)
                self.assertEqual(formatter.format(record), expected)


class BalloonAdjustmentTests(unittest.TestCase):
    def test_sub_mib_changes_are_skipped_even_across_mib_boundaries(self):
        actual = 1048576 + 1000
        for delta in (-1020, -900, -1, 0, 1, 1023):
            with self.subTest(delta=delta):
                domain = Domain()
                domain.memory = actual
                requested = autoreclaim.dom_balloon(domain, actual + delta, actual, domain.maximum)
                self.assertIsNone(requested)
                self.assertEqual(domain.calls, [])

    def test_changes_at_or_above_one_mib_use_page_aligned_target(self):
        actual = 1048576 + 1000
        for delta, aligned_delta in (
                (-1025, -1028), (-1024, -1024), (-1023, -1024),
                (1024, 1024), (1025, 1024), (1026, 1024), (1027, 1024)):
            with self.subTest(delta=delta):
                domain = Domain()
                domain.memory = actual
                target = actual + delta
                requested = autoreclaim.dom_balloon(domain, target, actual, domain.maximum)
                self.assertEqual(requested, actual + aligned_delta)
                self.assertEqual(domain.calls, [actual + aligned_delta])

    def test_minimum_change_is_checked_after_capping_to_maximum(self):
        actual = 1048576 + 1000
        domain = Domain()
        domain.memory = actual
        domain.maximum = actual + 1023
        requested = autoreclaim.dom_balloon(domain, actual + 2048, actual, domain.maximum)
        self.assertIsNone(requested)
        self.assertEqual(domain.calls, [])

    def test_target_is_page_aligned_after_capping_to_unaligned_maximum(self):
        domain = Domain()
        domain.maximum = domain.memory + 2047
        expected = domain.memory + 2044
        requested = autoreclaim.dom_balloon(
            domain, domain.memory + 4096, domain.memory, domain.maximum)
        self.assertEqual(requested, expected)
        self.assertEqual(domain.calls, [expected])


class VirtioMemAdjustmentTests(unittest.TestCase):
    def test_dom_virtio_mem_updates_live_device_request(self):
        xml = (
            "<domain><memory unit='KiB'>3145728</memory>"
            "<currentMemory unit='KiB'>1572864</currentMemory><devices>"
            "<memory model='virtio-mem'><target>"
            "<size unit='KiB'>2097152</size><block unit='KiB'>262144</block>"
            "<node>0</node><requested unit='KiB'>524288</requested>"
            "<current unit='KiB'>524288</current>"
            "</target></memory></devices></domain>")
        domain = Mock()
        domain.name.return_value = "guest-vmem"
        domain.XMLDesc.return_value = xml
        libvirt_mock = types.SimpleNamespace(
            libvirtError=type("LibvirtError", (Exception,), {}),
            VIR_DOMAIN_AFFECT_LIVE=1,
        )

        with patch.object(autoreclaim, "libvirt", libvirt_mock):
            requested = autoreclaim.dom_virtio_mem(domain, 2097152, 1572864)

        self.assertEqual(requested, 2097152)
        domain.updateDeviceFlags.assert_called_once()
        updated_xml, flags = domain.updateDeviceFlags.call_args.args
        updated_device = autoreclaim.ET.fromstring(updated_xml)
        self.assertEqual(flags, libvirt_mock.VIR_DOMAIN_AFFECT_LIVE)
        self.assertEqual(updated_device.findtext("./target/requested"), "1048576")
        self.assertIsNone(updated_device.find("./target/current"))

    def test_dom_virtio_mem_block_rounding_does_not_cross_guest_floor(self):
        domain = Mock()
        domain.XMLDesc.return_value = (
            "<domain><memory unit='KiB'>3145728</memory><devices>"
            "<memory model='virtio-mem'><target><size>2097152</size><block>262144</block>"
            "<requested>1048576</requested><current>1048576</current>"
            "</target></memory></devices></domain>")
        with patch.object(autoreclaim.libvirt, "VIR_DOMAIN_AFFECT_LIVE", 1, create=True):
            requested = autoreclaim.dom_virtio_mem(
                domain, 1350000, 2097152, minimum_ram_kb=1350000)

        self.assertEqual(requested, 1572864)
        device_xml = domain.updateDeviceFlags.call_args.args[0]
        self.assertEqual(autoreclaim.ET.fromstring(device_xml).findtext("./target/requested"), "524288")


class AdaptiveControllerTests(unittest.TestCase):
    def setUp(self):
        self.controller = autoreclaim.LibVirtAutoReclaim.__new__(autoreclaim.LibVirtAutoReclaim)
        self.controller.config = {"default": {}, "vms": [
            {"name": "guest-a", "auto_reclaim": True, "manual_increase_grace_seconds": 3600}
        ]}
        self.controller.monitored_vms = {"guest-a"}
        self.controller.guest_agents = set()
        self.controller.read_only_vms = set()
        self.controller.vm_backends = {"guest-a": {"type": "balloon"}}
        self.controller.stats_periods = {"guest-a": 2}
        self.controller.poll_interval = 2
        self.controller.memory_targets = {}
        self.controller.pending_targets = {}
        self.controller.manual_increase_until = {}
        self.controller.control_samples = {}
        self.domain = Domain()
        self.now = 100
        for clock in ("monotonic", "time"):
            clock_patch = patch.object(autoreclaim, clock, side_effect=lambda: self.now)
            clock_patch.start()
            self.addCleanup(clock_patch.stop)

    def step(self, seconds=2, pressure=1):
        self.now += seconds
        self.domain.updated = self.now
        self.controller.process_domainID(self.domain, pressure)

    def test_status_shows_effective_memory_weight(self):
        for default_weight, vm_weight, expected in (
                (None, None, 100), (500, None, 500), (500, 200, 200)):
            with self.subTest(default_weight=default_weight, vm_weight=vm_weight):
                self.controller.config["default"] = (
                    {} if default_weight is None else {"memory_weight": default_weight})
                vm = self.controller.config["vms"][0]
                if vm_weight is None:
                    vm.pop("memory_weight", None)
                else:
                    vm["memory_weight"] = vm_weight
                self.controller.conn = Mock()
                self.controller.conn.listDomainsID.return_value = [1]
                self.controller.conn.lookupByID.return_value = self.domain
                output = io.StringIO()
                with patch.object(self.controller, "dom_print_names"), redirect_stdout(output):
                    self.controller.status()
                self.assertEqual([line.split() for line in output.getvalue().splitlines()], [
                    ["Domain", "Backend", "Total", "Actual", "Used",
                     "Usable", "Keep_Usable", "Weight", "Floor", "PSI", "Swap_Used"],
                    ["guest-a", "balloon", "8192", "1024", "512", "512", "512",
                     str(expected), "1137", "n/a", "n/a"],
                ])

    def test_status_combines_statistics_source_and_memory_method(self):
        self.controller.conn = Mock()
        self.controller.conn.listDomainsID.return_value = [1]
        self.controller.conn.lookupByID.return_value = self.domain

        for backend, read_only, stats_source, expected_backend in (
                ("balloon", False, "balloon", "balloon"),
                ("virtio-mem", False, "guest-agent", "agent/virtio-mem"),
                ("balloon", True, "guest-agent", "agent/read-only"),
                ("balloon", True, "unavailable", "unavailable/read-only")):
            with self.subTest(backend=backend, read_only=read_only, source=stats_source):
                self.controller.vm_backends["guest-a"] = {"type": backend}
                if read_only:
                    self.controller.read_only_vms.add("guest-a")
                else:
                    self.controller.read_only_vms.discard("guest-a")
                with patch.object(self.controller, "_get_domain_memstats", return_value={
                        "actual": self.domain.memory,
                        "usable": self.domain.usable,
                        "source": stats_source}):
                    output = io.StringIO()
                    with patch.object(self.controller, "dom_print_names"), \
                            patch.object(self.controller, "_inspect_virtio_mem_devices", return_value=None), \
                            patch.object(self.controller, "_get_guest_agent_memory_pressure",
                                         return_value=(None, None)), redirect_stdout(output):
                        self.controller.status()

                row = output.getvalue().splitlines()[1].split()
                self.assertEqual(row[1], expected_backend)

    def test_status_aligns_columns_for_long_names_and_large_numbers(self):
        long_domain = Domain("Windows11Professional-Lavoro")
        long_domain.maximum = 1048576 * 1024
        self.controller.monitored_vms.add(long_domain.name())
        self.controller.conn = Mock()
        self.controller.conn.listDomainsID.return_value = [1, 2]
        self.controller.conn.lookupByID.side_effect = [self.domain, long_domain]
        output = io.StringIO()
        with patch.object(self.controller, "dom_print_names"), redirect_stdout(output):
            self.controller.status()
        lines = output.getvalue().splitlines()
        self.assertNotIn("\t", output.getvalue())
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[1].index("8192"), lines[2].index("1048576") + 3)
        self.assertEqual(lines[0].index("Total") + 5, lines[2].index("1048576") + 7)
        for line in lines[1:]:
            self.assertTrue(line.endswith("n/a"))
            self.assertEqual(len(line), len(lines[0]))
        self.assertTrue(lines[2].startswith(long_domain.name() + "  "))

    def test_status_with_no_active_domains_prints_only_header(self):
        self.controller.conn = Mock()
        self.controller.conn.listDomainsID.return_value = []
        output = io.StringIO()
        with patch.object(self.controller, "dom_print_names"), redirect_stdout(output):
            self.controller.status()
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertEqual(output.getvalue().split(), [
            "Domain", "Backend", "Total", "Actual", "Used",
            "Usable", "Keep_Usable", "Weight", "Floor", "PSI", "Swap_Used",
        ])

    def test_status_shows_guest_reclaim_floor_psi_and_swap(self):
        stats = {"actual": 4194304, "usable": 2097152, "source": "guest-agent",
                 "anon_pages": 2097152, "active_file": 524288, "inactive_file": 1048576,
                 "swap_total": 524288, "swap_free": 393216}
        with patch.object(self.controller, "_get_domain_memstats", return_value=stats), \
                patch.object(self.controller, "_read_guest_agent_file",
                             return_value="some avg10=0.40\nfull avg10=0.01\n") as read_file:
            row = self.controller.dom_status(self.domain)
        read_file.assert_called_once_with(self.domain, "/proc/pressure/memory")
        self.assertEqual(row[-3:], ("3413", "0.4%", "128"))

    def test_status_shows_virtio_mem_base_floor_without_guest_metrics(self):
        self.controller.vm_backends["guest-a"] = {"type": "virtio-mem"}
        stats = {"actual": 4194304, "usable": 2097152, "source": "balloon"}
        with patch.object(self.controller, "_get_domain_memstats", return_value=stats), \
                patch.object(self.controller, "_inspect_virtio_mem_devices", return_value={
                    "base_ram_kb": 2097152, "max_ram_kb": 6291456}), \
                patch.object(self.controller, "_get_guest_agent_memory_pressure") as psi:
            row = self.controller.dom_status(self.domain)
        psi.assert_not_called()
        self.assertEqual(row[-3:], ("2048", "n/a", "n/a"))

    def test_guest_agent_meminfo_reads_and_closes_guest_file(self):
        meminfo = ("MemTotal: 1048576 kB\nMemAvailable: 524288 kB\nAnonPages: 300000 kB\n"
                   "Active(file): 70000 kB\nInactive(file): 120000 kB\n"
                   "SwapTotal: 200000 kB\nSwapFree: 150000 kB\n")
        response = json.dumps({
            "return": {
                "buf-b64": base64.b64encode(meminfo.encode()).decode(),
                "eof": True,
            },
        })
        commands = []

        def qemu_agent_command(domain, command, timeout, flags):
            parsed = json.loads(command)
            commands.append(parsed["execute"])
            if parsed["execute"] == "guest-file-open":
                return '{"return": 7}'
            if parsed["execute"] == "guest-file-read":
                return response
            return '{"return": {}}'

        qemu_module = types.SimpleNamespace(qemuAgentCommand=qemu_agent_command)
        with patch.dict(sys.modules, {"libvirt_qemu": qemu_module}):
            stats = self.controller._get_guest_agent_meminfo(self.domain)

        self.assertEqual(stats, {
            "actual": 1048576,
            "usable": 524288,
            "last_update": 100,
            "source": "guest-agent",
            "sampled_at": 100,
            "anon_pages": 300000,
            "active_file": 70000,
            "inactive_file": 120000,
            "swap_total": 200000,
            "swap_free": 150000,
        })
        self.assertEqual(commands, ["guest-file-open", "guest-file-read", "guest-file-close"])

    def test_control_reads_guest_meminfo_once_and_only_reads_psi_on_demand(self):
        self.controller.guest_agents.add("guest-a")
        self.domain.memory = 4194304
        self.controller.config["default"].update({"pd_kp": 0, "pd_kd": 0})
        files = {
            "/proc/meminfo": "MemTotal: 4194304 kB\nMemAvailable: 1100000 kB\n",
            "/proc/pressure/memory": "some avg10=0.00\nfull avg10=1.01\n",
        }
        with patch.object(self.controller, "_read_guest_agent_file", side_effect=(
                lambda dom, path: files[path])) as read_file:
            self.step(pressure=0)
            self.assertEqual([call.args[1] for call in read_file.call_args_list], ["/proc/meminfo"])
            self.assertEqual(self.domain.calls, [])
            read_file.reset_mock()
            files["/proc/meminfo"] = "MemTotal: 4194304 kB\nMemAvailable: 1000000 kB\n"
            self.step(pressure=0)
            self.assertEqual([call.args[1] for call in read_file.call_args_list],
                             ["/proc/meminfo", "/proc/pressure/memory"])
        self.assertEqual(self.domain.calls, [5242880])

    def test_guest_psi_overrides_negative_pd_and_controls_reclaim(self):
        for some, full, usable, expected in (
                ("0.50", "0.00", 2097152, 4131388), ("1.00", "0.00", 2097152, 4162844),
                ("2.00", "0.00", 2097152, None), ("2.01", "0.00", 2097152, None),
                ("0.00", "0.10", 2097152, 4131388), ("0.00", "0.11", 2097152, None),
                ("0.00", "1.00", 1000000, None), ("0.00", "1.01", 1000000, 5242880),
                ("0.00", "1.01", 1048576, None), ("2.19", "1.19", 3334820, None),
                ("10.00", "0.00", 1000000, None), ("10.01", "0.00", 1000000, 4613732),
                ("10.01", "0.00", 2097152, None), ("nan", "bad", 2097152, 4131388)):
            with self.subTest(some=some, full=full, usable=usable):
                self.domain = Domain()
                self.domain.memory, self.domain.usable = 4194304, usable
                self.controller.memory_targets.clear()
                self.controller.pending_targets.clear()
                self.controller.control_samples.clear()
                stats = {**self.domain.memoryStats(), "last_update": self.now, "source": "guest-agent"}
                with patch.object(self.controller, "_read_guest_agent_file", return_value=(
                        f"some avg10={some} avg60=56.00\nfull avg10={full} avg60=12.00\n")) as read_file:
                    self.controller.process_domainID(self.domain, 1, stats)
                read_file.assert_called_once_with(self.domain, "/proc/pressure/memory")
                self.assertEqual(self.domain.calls, [] if expected is None else [expected])

    def test_guest_swap_drop_veto_and_emergency_use_elapsed_sample_time(self):
        for drop, elapsed, expected in (
                (1, 2, 4131388), (255, 2, 4131388), (256, 2, None), (256, 4, 4068472),
                (20480, 2, None), (20481, 2, 5242880),
                (20481, 4, None), (-1, 2, 4131388)):
            with self.subTest(drop=drop, elapsed=elapsed):
                self.domain = Domain()
                self.domain.memory, self.domain.usable = 4194304, 2097152
                self.controller.control_samples.clear()
                self.controller.memory_targets.clear()
                self.controller.pending_targets.clear()
                stats = {**self.domain.memoryStats(), "source": "guest-agent",
                         "last_update": self.now, "sampled_at": self.now,
                         "swap_total": 131072, "swap_free": 65536}
                with patch.object(self.controller, "_get_guest_agent_memory_pressure",
                                  return_value=(None, None)):
                    self.controller.process_domainID(self.domain, 0, stats)
                    # The wall-clock sample spacing differs from the monotonic spacing.
                    self.now += 2
                    stats.update(last_update=self.now, sampled_at=stats["sampled_at"] + elapsed,
                                 swap_free=65536 - drop)
                    self.controller.process_domainID(self.domain, 1, stats)
                self.assertEqual(self.domain.calls, [] if expected is None else [expected])

    def test_guest_floor_limits_reclaim_without_protecting_inactive_cache(self):
        self.domain.memory, self.domain.usable = 4194304, 2097152
        # ceil((2500000 + 701001 + 524288) / 0.9), then page aligned upwards.
        stats = {**self.domain.memoryStats(), "source": "guest-agent", "anon_pages": 2500000,
                 "active_file": 701001, "inactive_file": 900000}
        with patch.object(self.controller, "_get_guest_agent_memory_pressure", return_value=(0, 0)):
            self.controller.process_domainID(self.domain, 1, stats)
        self.assertEqual(self.domain.calls, [4139212])

    def test_guest_floor_above_actual_does_not_cause_growth(self):
        for some, kp in ((0, 0.5), (0, 0), (2, 0.5), (2.01, 0.5)):
            with self.subTest(some=some, pd_kp=kp):
                self.domain = Domain()
                self.domain.memory, self.domain.usable = 8388608, 2097152
                self.domain.maximum = 16777216
                self.controller.control_samples.clear()
                self.controller.memory_targets.clear()
                self.controller.pending_targets.clear()
                self.controller.config["default"]["pd_kp"] = kp
                stats = {**self.domain.memoryStats(), "source": "guest-agent",
                         "anon_pages": 1048576, "active_file": 6291456}
                with patch.object(self.controller, "_get_guest_agent_memory_pressure", return_value=(some, 0)):
                    self.controller.process_domainID(self.domain, 1, stats)
                self.assertEqual(self.domain.calls, [])

    def test_guest_reclaim_veto_does_not_request_growth_to_static_floor(self):
        self.domain.usable = 655360
        stats = {**self.domain.memoryStats(), "source": "guest-agent"}
        with patch.object(self.controller, "_get_guest_agent_memory_pressure", return_value=(2, 0)):
            self.controller.process_domainID(self.domain, 1, stats)
        self.assertEqual(self.domain.calls, [])

    def test_guest_agent_uses_monotonic_time_for_new_samples_and_swap_rate(self):
        self.domain.memory, self.domain.usable = 4194304, 2097152
        stats = {**self.domain.memoryStats(), "source": "guest-agent", "sampled_at": 100.1,
                 "swap_total": 131072, "swap_free": 65536}
        with patch.object(self.controller, "_get_guest_agent_memory_pressure", return_value=(0, 0)) as psi:
            self.controller.process_domainID(self.domain, 0, stats)
            self.controller.process_domainID(self.domain, 1, stats)
            psi.assert_called_once()
            self.assertEqual(self.domain.calls, [])
            stats.update(sampled_at=100.6, swap_free=65536 - 5121)
            self.controller.process_domainID(self.domain, 1, stats)
        self.assertEqual(psi.call_count, 2)
        self.assertEqual(self.domain.calls, [5242880])

    def test_guest_agent_stats_drive_memory_growth(self):
        self.controller.guest_agents.add("guest-a")
        self.domain.usable = 100000
        guest_stats = {
            "actual": self.domain.memory,
            "usable": self.domain.usable,
            "last_update": self.now,
            "source": "guest-agent",
        }
        with patch.object(self.domain, "memoryStats") as memory_stats, \
                patch.object(self.controller, "_get_guest_agent_meminfo", return_value=guest_stats), \
                patch.object(self.controller, "_read_guest_agent_file", return_value=None):
            self.controller.process_domainID(self.domain, 0)

        memory_stats.assert_not_called()
        self.assertGreater(self.domain.memory, 1048576)

    def test_virtio_mem_backend_uses_virtio_adjustments_not_balloon(self):
        self.controller.vm_backends["guest-a"] = {"type": "virtio-mem"}
        self.domain.usable = 100000
        stats = {
            "actual": self.domain.memory,
            "usable": self.domain.usable,
            "last_update": self.now,
        }
        vmem_info = {"max_ram_kb": self.domain.maximum, "base_ram_kb": 262144}
        with patch.object(self.controller, "_inspect_virtio_mem_devices", return_value=vmem_info):
            with patch.object(autoreclaim, "dom_virtio_mem", return_value=2097152) as adjust_vmem:
                with patch.object(autoreclaim, "dom_balloon") as adjust_balloon:
                    self.controller.process_domainID(self.domain, 0, stats)

        adjust_vmem.assert_called_once()
        args = adjust_vmem.call_args.args
        self.assertIs(args[0], self.domain)
        self.assertEqual(args[2], self.domain.memory)
        self.assertGreater(args[1], self.domain.memory)
        adjust_balloon.assert_not_called()
        self.assertEqual(self.controller.pending_targets["guest-a"][0], 2097152)

    def test_virtio_mem_reclaim_is_limited_by_host_pressure(self):
        self.controller.vm_backends["guest-a"] = {"type": "virtio-mem"}
        self.controller.conn = Mock()
        self.controller.conn.listDomainsID.return_value = [1]
        self.controller.conn.lookupByID.return_value = self.domain
        self.domain.memory = 4194304
        self.domain.usable = 2097152
        vmem_info = {"max_ram_kb": self.domain.maximum, "base_ram_kb": 1048576}
        requested_targets = []

        def request_virtio_mem(domain, target, actual, *, minimum_ram_kb=0):
            requested_targets.append(target)
            return target if target != actual else None

        for pressure, usable in ((0, 2097152), (0.5, 2097152), (1, 2097152),
                                 (0, 3407872), (1, 3407872)):
            with self.subTest(pressure=pressure, usable=usable):
                self.domain.usable = usable
                self.controller.memory_targets.clear()
                self.controller.pending_targets.clear()
                self.controller.control_samples.clear()
                with patch.object(self.controller, "_get_domain_memstats", return_value={
                        "actual": self.domain.memory,
                        "usable": self.domain.usable,
                        "last_update": self.now}):
                    with patch.object(self.controller, "host_memory_pressure", return_value=pressure):
                        with patch.object(
                                self.controller, "_inspect_virtio_mem_devices", return_value=vmem_info):
                            with patch.object(
                                    autoreclaim, "dom_virtio_mem", side_effect=request_virtio_mem):
                                self.controller.process_domains()

        self.assertEqual(requested_targets[0], self.domain.memory)
        self.assertGreater(requested_targets[1], requested_targets[2])
        self.assertLess(requested_targets[2], self.domain.memory)
        self.assertLess(requested_targets[3], self.domain.memory)
        self.assertEqual(requested_targets[3], requested_targets[4])

    def test_virtio_mem_statistics_use_current_device_memory(self):
        self.controller.vm_backends["guest-a"] = {
            "type": "virtio-mem",
            "has_guest_agent": True,
        }
        self.domain.XMLDesc = Mock(return_value=(
            "<domain><memory unit='KiB'>3145728</memory>"
            "<currentMemory unit='KiB'>3145728</currentMemory><devices>"
            "<memory model='virtio-mem'><target>"
            "<size unit='KiB'>2097152</size><block unit='KiB'>262144</block>"
            "<requested unit='KiB'>524288</requested><current unit='KiB'>524288</current>"
            "</target></memory></devices></domain>"))
        guest_stats = {
            "actual": 3145728,
            "usable": 2097152,
            "last_update": self.now,
            "source": "guest-agent",
        }
        with patch.object(self.controller, "_get_guest_agent_meminfo", return_value=guest_stats):
            with patch.object(self.domain, "memoryStats") as memory_stats:
                stats = self.controller._get_domain_memstats(self.domain)

        memory_stats.assert_not_called()
        self.assertEqual(stats["actual"], 1572864)
        self.assertEqual(stats["usable"], 1572864)
        self.assertEqual(stats["source"], "guest-agent")

    def test_virtio_mem_status_includes_base_ram_with_balloon_stats(self):
        self.controller.vm_backends["guest-a"] = {"type": "virtio-mem"}
        self.domain.XMLDesc = Mock(return_value=(
            "<domain><memory unit='KiB'>8388608</memory>"
            "<currentMemory unit='KiB'>4325376</currentMemory><devices>"
            "<memory model='virtio-mem'><target>"
            "<size unit='KiB'>7340032</size><requested unit='KiB'>3407872</requested>"
            "<current unit='KiB'>3276800</current>"
            "</target></memory></devices></domain>"))
        with patch.object(self.domain, "memoryStats", return_value={
                "actual": 4325376, "usable": 2602964, "last_update": self.now}):
            row = self.controller.dom_status(self.domain)

        self.assertEqual(row[:6], (
            "guest-a", "balloon/virtio-mem", "8192", "4224", "1682", "2541"))

    def test_read_only_guest_agent_monitoring_does_not_change_memory(self):
        self.controller.read_only_vms.add("guest-a")
        guest_stats = {
            "actual": self.domain.memory,
            "usable": self.domain.usable,
            "last_update": self.now,
            "source": "guest-agent",
        }

        with patch.object(self.domain, "info") as info:
            self.controller.process_domainID(self.domain, 1, guest_stats)

        info.assert_not_called()
        self.assertEqual(self.domain.calls, [])

    def test_read_only_guest_agent_reads_stats_without_balloon_api(self):
        self.controller.read_only_vms.add("guest-a")
        guest_stats = {
            "actual": self.domain.memory,
            "usable": self.domain.usable,
            "last_update": self.now,
            "source": "guest-agent",
        }

        with patch.object(self.controller, "_get_guest_agent_meminfo", return_value=guest_stats) as get_stats:
            with patch.object(self.domain, "memoryStats") as memory_stats:
                stats = self.controller._get_domain_memstats(self.domain)

        self.assertEqual(stats, guest_stats)
        get_stats.assert_called_once_with(self.domain)
        memory_stats.assert_not_called()

    def test_status_uses_guest_agent_without_querying_fresh_balloon_stats(self):
        self.controller.guest_agents.add("guest-a")
        guest_stats = {"actual": 983040, "usable": 393216, "last_update": self.now,
                       "source": "guest-agent"}
        with patch.object(self.controller, "_get_guest_agent_meminfo", return_value=guest_stats), \
                patch.object(self.controller, "_get_guest_agent_memory_pressure", return_value=(None, None)), \
                patch.object(self.domain, "memoryStats", wraps=self.domain.memoryStats) as balloon:
            row = self.controller.dom_status(self.domain)

        balloon.assert_not_called()
        self.assertEqual(row[:6], ("guest-a", "agent/balloon", "8192", "960", "576", "384"))

    def test_guest_agent_open_failure_falls_back_to_balloon_without_retrying_psi(self):
        self.controller.guest_agents.add("guest-a")
        self.domain.memory, self.domain.usable = 4194304, 2097152
        operations = Mock()
        operations.agent.side_effect = autoreclaim.libvirt.libvirtError("cannot open /proc/meminfo")
        operations.balloon.side_effect = self.domain.memoryStats
        qemu_module = types.SimpleNamespace(qemuAgentCommand=operations.agent)
        with patch.dict(sys.modules, {"libvirt_qemu": qemu_module}), \
                patch.object(self.domain, "memoryStats", operations.balloon):
            self.controller.process_domainID(self.domain, 1)

        self.assertEqual([call[0] for call in operations.mock_calls], ["agent", "balloon"])
        self.assertEqual(json.loads(operations.agent.call_args.args[1])["arguments"]["path"],
                         "/proc/meminfo")
        self.assertEqual(self.controller.control_samples["guest-a"].source, "balloon")
        self.assertEqual(self.domain.calls, [4131388])

    def test_no_change_at_reserve(self):
        self.domain.memory = 1310720
        self.step()
        self.assertEqual(self.domain.calls, [])

    def test_reclaim_scales_with_pressure_and_idle_ratio_and_is_capped(self):
        reductions = []
        for pressure, expected in ((0, 0), (0.5, 31460), (1, 62916)):
            with self.subTest(pressure=pressure):
                self.domain = Domain()
                self.controller.control_samples.clear()
                self.controller.memory_targets.clear()
                self.controller.pending_targets.clear()
                self.domain.memory = 4194304
                self.domain.usable = 2097152
                self.step(pressure=pressure)
                reduction = 4194304 - self.domain.memory
                self.assertLessEqual(reduction, 4194304 * 0.03)
                self.assertEqual(reduction, expected)
                reductions.append(reduction)
        self.assertEqual(reductions[0], 0)
        self.assertLess(reductions[1], reductions[2])

    def test_large_surplus_enables_reclaim_without_host_pressure(self):
        actual = 4194300
        reserve = 524288
        threshold = actual * 13 // 20
        for surplus, pressure in (
                (threshold - 4096, 0), (threshold, 0),
                (threshold + 4096, 0), (threshold + 4096, 0.25)):
            with self.subTest(surplus=surplus, pressure=pressure):
                reductions = []
                for current_pressure in (pressure, 1):
                    self.domain = Domain()
                    self.domain.memory = actual
                    self.domain.usable = reserve + surplus
                    self.controller.control_samples.clear()
                    self.controller.memory_targets.clear()
                    self.controller.pending_targets.clear()
                    self.step(pressure=current_pressure)
                    reductions.append(actual - self.domain.memory)
                if surplus > threshold:
                    self.assertGreater(reductions[0], 0)
                    self.assertEqual(reductions[0], reductions[1])
                    self.assertLessEqual(reductions[0], surplus)
                else:
                    self.assertEqual(reductions[0], 0)
                    self.assertGreater(reductions[1], 0)

    def test_large_surplus_threshold_uses_proportional_reserve(self):
        self.controller.config["default"]["keep_free_ratio"] = 0.3
        self.domain.memory, self.domain.usable = 4194304, 3145728
        self.step(pressure=0)
        self.assertEqual(self.domain.memory, 4194304)

    def test_idle_reclaim_threshold_inherits_and_can_be_overridden(self):
        for default, override, reclaims in (
                (0, None, True), (0.25, None, True), (0.375, None, False),
                (1, None, False), (0.25, 0.5, False), (0.5, 0.25, True)):
            with self.subTest(default=default, override=override):
                self.controller.config["default"]["idle_reclaim_threshold"] = default
                vm = self.controller.config["vms"][0]
                if override is None:
                    vm.pop("idle_reclaim_threshold", None)
                else:
                    vm["idle_reclaim_threshold"] = override
                self.controller._LibVirtAutoReclaim__validate_config_parameters(self.controller.config)
                self.domain = Domain()
                self.domain.memory, self.domain.usable = 4194304, 2097152
                self.controller.control_samples.clear()
                self.controller.memory_targets.clear()
                self.controller.pending_targets.clear()
                self.step(pressure=0)
                self.assertEqual(self.domain.memory < 4194304, reclaims)

    def test_memory_weight_scales_reclaim_with_a_twofold_cap(self):
        self.controller.config["default"]["memory_weight"] = 500
        for weight, expected in (
                (None, 18876), (1, 188744), (10, 188744), (50, 188744),
                (100, 94372), (500, 18876), (1000, 9440)):
            with self.subTest(weight=weight):
                self.domain = Domain()
                self.domain.memory = 4194304
                self.domain.usable = 3145728
                self.controller.memory_targets.clear()
                self.controller.pending_targets.clear()
                self.controller.control_samples.clear()
                vm = self.controller.config["vms"][0]
                if weight is None:
                    vm.pop("memory_weight", None)
                else:
                    vm["memory_weight"] = weight
                self.step()
                self.assertEqual(4194304 - self.domain.memory, expected)

    def test_initial_reclaim_uses_vm_stats_period_and_per_second_rate(self):
        self.controller.poll_interval = 1
        for rate, expected_reductions in (
                (None, (47188, 94372, 235932)), (0.02, (62916, 125832, 314572))):
            for period, expected in zip((1, 2, 5), expected_reductions):
                with self.subTest(rate=rate, period=period):
                    self.domain = Domain()
                    self.domain.memory, self.domain.usable = 4194304, 3145728
                    self.controller.control_samples.clear()
                    self.controller.memory_targets.clear()
                    self.controller.pending_targets.clear()
                    self.controller.stats_periods["guest-a"] = period
                    self.controller.config["default"] = (
                        {} if rate is None else {"reclaim_ratio": rate})
                    self.step(seconds=1)
                    self.assertEqual(4194304 - self.domain.memory, expected)

    def test_reclaim_uses_fresh_sample_elapsed_not_poll_interval(self):
        self.controller.poll_interval = 1
        self.controller.stats_periods["guest-a"] = 2
        for elapsed, expected in ((1, 47188), (2, 94372), (5, 235932)):
            with self.subTest(elapsed=elapsed):
                self.domain = Domain()
                self.domain.memory, self.domain.usable = 4194304, 2097152
                self.controller.control_samples.clear()
                self.controller.memory_targets.clear()
                self.controller.pending_targets.clear()
                self.step(pressure=0)
                self.domain.usable = 3145728
                self.step(seconds=elapsed)
                self.assertEqual(4194304 - self.domain.memory, expected)

    def test_emergency_growth_anticipates_next_dynamic_poll(self):
        self.controller.poll_interval = 5
        self.controller.stats_periods["guest-a"] = 5
        self.domain.memory, self.domain.usable = 4194304, 2097152
        self.step(pressure=0)
        self.domain.usable = 1310720
        self.step(pressure=0)
        self.assertEqual(self.domain.memory, 5373952)

    def test_memory_weight_does_not_change_emergency_growth(self):
        for usable in (102400, 1572864):
            targets = []
            for weight in (1, 100, 1000):
                with self.subTest(usable=usable, weight=weight):
                    self.domain = Domain()
                    self.domain.memory = 4194304
                    self.domain.usable = 2097152
                    self.controller.memory_targets.clear()
                    self.controller.pending_targets.clear()
                    self.controller.control_samples.clear()
                    self.controller.config["vms"][0]["memory_weight"] = weight
                    self.step(pressure=0)
                    self.domain.usable = usable
                    self.step(pressure=0)
                    self.assertGreaterEqual(self.domain.memory, 5242880)
                    targets.append(self.domain.memory)
            self.assertEqual(targets, [targets[0]] * 3)

    def test_memory_weight_does_not_lower_floor_or_reserve(self):
        for weight in (1, 1000):
            with self.subTest(weight=weight):
                self.domain = Domain()
                self.domain.memory = 1169180
                self.domain.usable = self.domain.memory - 262144
                self.controller.memory_targets.clear()
                self.controller.pending_targets.clear()
                self.controller.control_samples.clear()
                self.controller.config["vms"][0]["memory_weight"] = weight
                for _ in range(5):
                    self.step()
                    self.assertGreaterEqual(self.domain.memory, 1165084)
                    self.assertGreaterEqual(self.domain.usable, 524288)
                self.assertEqual(self.domain.memory, 1165084)

    def test_ewma_smooths_non_emergency_spike(self):
        self.domain.memory = 4194304
        self.domain.usable = 1048576
        self.step(pressure=0)
        self.domain.usable = 786432
        self.step(pressure=0)
        sample = self.controller.control_samples["guest-a"]
        self.assertAlmostEqual(sample.used_ewma, 0.3 * 3407872 + 0.7 * 3145728)
        self.assertLess(sample.used_ewma, sample.used)
        self.assertEqual(self.domain.calls, [])

    def test_low_instantaneous_reserve_grows_without_waiting_for_ewma_or_psi(self):
        self.controller.config["default"]["ewma_alpha"] = 0.01
        self.domain.memory = 4194304
        self.domain.usable = 2097152
        self.step(pressure=0)
        self.domain.usable = 102400
        self.step(pressure=0)
        self.assertGreaterEqual(self.domain.memory, 4194304 * 1.25)
        self.assertGreaterEqual(self.domain.usable, 524288)

    def test_fast_used_memory_trend_grows_before_reserve_is_exhausted(self):
        self.domain.memory = 4194304
        self.domain.usable = 2097152
        self.step(pressure=0)
        self.domain.usable = 1572864
        self.step(pressure=0)
        self.assertGreaterEqual(self.domain.memory, 4194304 * 1.25)

    def test_proportional_reserve_and_maximum_limit(self):
        self.controller.config["default"]["keep_free_ratio"] = 0.25
        self.domain.memory = 4194304
        self.domain.usable = 786432
        self.domain.maximum = 4456448
        self.assertEqual(self.controller.dom_keep_usable(self.domain, self.domain.memory), 1048576)
        self.step(pressure=0)
        self.assertEqual(self.domain.calls, [4456448])

    def test_guest_smaller_than_minimum_reserve_stays_at_its_maximum(self):
        self.domain.memory = self.domain.maximum = 262144
        self.domain.usable = 65536
        self.step()
        self.assertEqual(self.domain.calls, [])

    def test_floor_accounts_for_ratio_and_absolute_reserve_and_caps_at_maximum(self):
        for minimum, ratio, maximum, expected in (
                (524288, 0, 67108864, 1048576),
                (524288, 0.1, 67108864, 1165084),
                (524288, 0.5, 67108864, 2097152),
                (524288, 0.95, 67108864, 10485760),
                (524288, 1, 67108864, 10485760),
                (1048576, 0.5, 67108864, 4194304),
                (524288, 0.5, 1572864, 1572864)):
            with self.subTest(minimum=minimum, ratio=ratio, maximum=maximum):
                self.domain = Domain()
                self.domain.maximum = maximum
                self.controller.memory_targets.clear()
                self.controller.pending_targets.clear()
                self.controller.control_samples.clear()
                self.controller.config["default"].update(
                    {"keep_free_kb": minimum, "keep_free_ratio": ratio})
                self.step(pressure=0)
                self.assertEqual(self.domain.memory, expected)

    def test_reclaim_never_crosses_floor_or_consumes_raw_reserve(self):
        self.domain.memory = 2097152
        self.domain.usable = 1835008
        for _ in range(60):
            before = self.domain.memory
            self.step()
            self.assertGreaterEqual(self.domain.memory, 1165084)
            self.assertGreaterEqual(self.domain.usable, 524288)
            self.assertLessEqual(before - self.domain.memory, before * 0.03)
        self.assertLess(self.domain.memory, 2097152)

    def test_pd_derivative_brakes_reclaim_as_free_memory_falls(self):
        for kd in (0, 1):
            with self.subTest(kd=kd):
                self.domain = Domain()
                self.domain.memory = 4194304
                self.domain.usable = 1048576
                self.controller.control_samples.clear()
                self.controller.memory_targets.clear()
                self.controller.pending_targets.clear()
                self.controller.config["default"].update({"ewma_alpha": 1, "pd_kd": kd})
                self.step(pressure=0)
                self.domain.usable = 786432
                self.step()
                if kd == 0:
                    self.assertLess(self.domain.memory, 4194304)
                else:
                    self.assertEqual(self.domain.calls, [])

    def test_derivative_contribution_is_capped_by_current_error(self):
        self.domain.memory = 2097152
        self.domain.usable = 614400
        self.controller.config["default"].update({"ewma_alpha": 0.5, "pd_kd": 10})
        self.controller.control_samples["guest-a"] = autoreclaim.MemorySample(
            1925120, 1925120, -131072, 99, source="balloon")
        self.controller.process_domainID(self.domain, 1)
        self.assertEqual(self.domain.calls, [2097152 + 196608])

    def test_subsecond_intervals_use_one_second_for_derivative(self):
        for updated in (99.25, 99.5, 100):
            with self.subTest(updated=updated):
                self.domain = Domain()
                self.domain.memory = 2097152
                self.domain.usable = 614400
                self.controller.memory_targets.clear()
                self.controller.pending_targets.clear()
                self.controller.config["default"].update({"ewma_alpha": 0.5, "pd_kd": 0.5})
                self.controller.control_samples["guest-a"] = autoreclaim.MemorySample(
                    1925120, 1925120, 65536, 99)
                stats = self.domain.memoryStats()
                stats["last_update"] = updated
                self.controller.process_domainID(self.domain, 1, stats)
                self.assertEqual(self.domain.calls, [2097152 + 98304])

    def test_derivative_does_not_reverse_reclaim_when_reserve_is_satisfied(self):
        self.controller.config["default"]["ewma_alpha"] = 1
        self.domain.memory = 4194304
        self.domain.usable = 1048576
        self.step(pressure=0)
        self.domain.usable = 655360
        self.step()
        self.assertEqual(self.domain.calls, [])

    def test_constant_working_set_converges_without_growth_reclaim_oscillation(self):
        self.domain.memory = 4194304
        self.domain.usable = 3407872
        for _ in range(120):
            before = self.domain.memory
            self.step()
            self.assertLessEqual(self.domain.memory, before)
            self.assertGreaterEqual(self.domain.usable, 524288)
        self.assertLessEqual(self.domain.usable - 524288, 2048)
        settled = self.domain.memory
        for _ in range(10):
            self.step()
        self.assertEqual(self.domain.memory, settled)

    def test_unchanged_stats_do_not_repeat_growth_or_update_ewma(self):
        self.domain.usable = 100000
        self.step()
        sample = self.controller.control_samples["guest-a"]
        self.now += 2
        self.controller.process_domainID(self.domain, 1)
        self.assertEqual(len(self.domain.calls), 1)
        self.assertIs(self.controller.control_samples["guest-a"], sample)

    def test_stale_future_or_invalid_stats_do_not_change_memory(self):
        for stats in (
                {"actual": 1048576, "usable": 100000, "last_update": 80},
                {"actual": 1048576, "usable": 100000, "last_update": 101},
                {"actual": 1048576, "usable": 100000},
                {"actual": 0, "usable": 0, "last_update": 100},
                {"actual": 1048576, "usable": 2097152, "last_update": 100}):
            with self.subTest(stats=stats), patch.object(self.domain, "memoryStats", return_value=stats):
                self.controller.process_domainID(self.domain, 1)
                self.assertEqual(self.domain.calls, [])

    def test_backward_sample_timestamp_resets_derivative(self):
        self.domain.memory = 1310720
        self.step(pressure=0)
        self.domain.updated -= 1
        self.controller.process_domainID(self.domain, 1)
        self.assertEqual(self.domain.calls, [])
        self.assertEqual(self.controller.control_samples["guest-a"].updated, self.domain.updated)

    def test_manual_increase_defers_reduction_until_grace_expires(self):
        self.domain.memory = 1310720
        self.step()
        self.domain.memory = 2097152
        self.domain.usable = 1572864
        self.step()
        self.assertEqual(self.domain.calls, [])
        self.step(seconds=3601)
        self.assertEqual(len(self.domain.calls), 1)
        self.assertLess(self.domain.calls[0], 2097152)

    def test_own_increase_does_not_start_grace_period(self):
        self.domain.usable = 100000
        self.step()
        self.assertGreater(self.domain.calls[0], 1048576)
        self.domain.usable = 1048576
        self.step()
        self.assertEqual(len(self.domain.calls), 2)
        self.assertEqual(self.controller.manual_increase_until, {})

    def test_partial_asynchronous_growth_does_not_start_manual_grace(self):
        self.domain.usable = 100000
        with patch.object(self.domain, "setMemory", side_effect=self.domain.calls.append):
            self.step()
            requested = self.domain.calls[0]
            for observed in (1114112, 1179648, requested - 512, requested):
                self.now += 1
                self.domain.usable += observed - self.domain.memory
                self.domain.memory = observed
                self.controller.process_domainID(self.domain, 1)
        self.assertEqual(len(self.domain.calls), 1)
        self.assertEqual(self.controller.manual_increase_until, {})

    def test_fresh_partial_growth_is_not_reversed_or_mistaken_for_external_growth(self):
        self.domain.usable = 100000
        with patch.object(self.domain, "setMemory", side_effect=self.domain.calls.append):
            self.step()
            requested = self.domain.calls[0]
            for observed in (requested - 131072, requested - 65536):
                self.domain.memory = observed
                self.domain.usable = observed - 300000
                self.step()
                self.assertEqual(self.domain.calls, [requested])
                self.assertEqual(self.controller.manual_increase_until, {})
            self.domain.memory = requested
            self.domain.usable = requested - 300000
            self.step()
        self.assertEqual(self.controller.manual_increase_until, {})
        self.assertLess(self.domain.calls[-1], requested)

    def test_page_granularity_difference_does_not_start_manual_grace(self):
        self.domain.usable = 100001
        with patch.object(self.domain, "setMemory", side_effect=self.domain.calls.append):
            self.step()
            requested = self.domain.calls[0]
            self.domain.memory = requested + 4
            self.domain.usable = 524288
            self.step()
        self.assertEqual(self.controller.manual_increase_until, {})

    def test_unsynchronized_info_memory_does_not_trigger_manual_grace(self):
        self.step()
        with patch.object(self.domain, "info", return_value=(1, 8388608, 2097152, 1, 0)):
            self.step()
        self.assertEqual(self.controller.manual_increase_until, {})

    def test_increase_beyond_pending_growth_still_starts_manual_grace(self):
        self.domain.usable = 100000
        with patch.object(self.domain, "setMemory", side_effect=self.domain.calls.append):
            self.step()
            requested = self.domain.calls[0]
            self.domain.memory = requested + 262144
            self.domain.usable = self.domain.memory - 300000
            self.step()
        self.assertEqual(self.controller.manual_increase_until["guest-a"], self.now + 3600)
        self.assertEqual(self.domain.calls, [requested])

    def test_emergency_growth_can_interrupt_pending_reclaim(self):
        self.domain.memory = 2097152
        self.domain.usable = 1572864
        with patch.object(self.domain, "setMemory", side_effect=self.domain.calls.append):
            self.step()
            self.assertLess(self.domain.calls[0], self.domain.memory)
            self.domain.usable = 100000
            self.step()
        self.assertEqual(len(self.domain.calls), 2)
        self.assertGreater(self.domain.calls[1], self.domain.memory)
        self.assertEqual(self.controller.manual_increase_until, {})

    def test_pending_reclaim_expires_at_30_seconds_and_is_retried(self):
        self.domain.memory = 2097152
        self.domain.usable = 1572864
        with patch.object(self.domain, "setMemory", side_effect=self.domain.calls.append):
            self.step()
            self.step(seconds=29)
            self.assertEqual(len(self.domain.calls), 1)
            with self.assertLogs(level="WARNING") as logs:
                self.step(seconds=1)
        self.assertIn("timed out", logs.output[0])
        self.assertEqual(len(self.domain.calls), 2)
        self.assertEqual(self.controller.pending_targets["guest-a"], (self.domain.calls[-1], self.now))

    def test_pending_request_expires_even_when_guest_statistics_are_stale(self):
        self.domain.memory = 2097152
        self.domain.usable = 1572864
        with patch.object(self.domain, "setMemory", side_effect=self.domain.calls.append):
            self.step()
            self.now += 30
            with self.assertLogs(level="WARNING") as logs:
                self.controller.process_domainID(self.domain, 1)
            self.assertTrue(any("timed out" in message for message in logs.output))
            self.assertNotIn("guest-a", self.controller.pending_targets)
            self.assertEqual(len(self.domain.calls), 1)
            self.step()
        self.assertEqual(len(self.domain.calls), 2)

    def test_completed_growth_at_timeout_does_not_trigger_false_grace(self):
        self.domain.usable = 100000
        with patch.object(self.domain, "setMemory", side_effect=self.domain.calls.append):
            self.step()
            requested = self.domain.calls[0]
            self.domain.memory = requested
            self.domain.usable = 524288
            with patch.object(autoreclaim.logging, "warning") as warning:
                self.step(seconds=30)
        warning.assert_not_called()
        self.assertEqual(self.controller.manual_increase_until, {})

    def test_host_pressure_uses_psi_or_available_buffer(self):
        for avg10, available, expected in ((0, 500, 0), (0, 200, 0), (0, 100, 0.5),
                                           (0, 0, 1), (2.5, 500, 0.5), (5, 500, 1),
                                           (100, 500, 1)):
            with self.subTest(avg10=avg10, available=available):
                files = {
                    "/proc/meminfo": "MemTotal: 1000 kB\nMemAvailable: {} kB\n".format(available),
                    "/proc/pressure/memory": (
                        "some avg10={} avg60=0.0 avg300=0.0 total=1\n"
                        "full avg10=99 avg60=0 avg300=0 total=0\n".format(avg10)),
                }
                with patch("builtins.open", side_effect=lambda path: mock_open(read_data=files[path])()):
                    self.assertEqual(self.controller.host_memory_pressure(), expected)

    def test_host_pressure_uses_configured_thresholds(self):
        self.controller.config["general"] = {
            "psi_threshold": 10, "host_min_free_ratio": 0.05}
        self.controller._LibVirtAutoReclaim__validate_config_parameters(self.controller.config)
        for psi, available, expected in ((0, 100, 0), (0, 25, 0.5),
                                         (5, 500, 0.5), (10, 500, 1)):
            with self.subTest(psi=psi, available=available):
                files = {
                    "/proc/meminfo": "MemTotal: 1000 kB\nMemAvailable: {} kB\n".format(available),
                    "/proc/pressure/memory": "some avg10={}\n".format(psi),
                }
                with patch("builtins.open", side_effect=lambda path: mock_open(read_data=files[path])()):
                    self.assertEqual(self.controller.host_memory_pressure(), expected)

    def test_bad_host_stats_disable_reclaim_but_allow_growth(self):
        self.controller.config["default"]["idle_reclaim_threshold"] = 0
        for psi in ("full avg10=1\n", "some avg10=nan\n", "some avg10=-1\n",
                    "some avg10=101\n", "some avg10=broken\n", OSError("PSI unavailable")):
            with self.subTest(psi=psi):
                def open_stats(path):
                    if path == "/proc/meminfo":
                        return mock_open(read_data="MemTotal: 1000 kB\nMemAvailable: 0 kB\n")()
                    if isinstance(psi, OSError):
                        raise psi
                    return mock_open(read_data=psi)()

                with patch("builtins.open", side_effect=open_stats):
                    with self.assertLogs(level="ERROR") as logs:
                        pressure = self.controller.host_memory_pressure()
                    self.assertEqual(pressure, autoreclaim.HOST_PRESSURE_UNAVAILABLE)
                    self.assertIn("reclaim disabled", logs.output[0])
                    self.domain = Domain()
                    self.domain.memory, self.domain.usable = 4194304, 3145728
                    self.controller.control_samples.clear()
                    self.controller.memory_targets.clear()
                    self.controller.pending_targets.clear()
                    self.step(pressure=pressure)
                    self.assertEqual(self.domain.memory, 4194304)
                    self.domain = Domain()
                    self.domain.usable = 100000
                    self.controller.control_samples.clear()
                    self.controller.memory_targets.clear()
                    self.controller.pending_targets.clear()
                    self.step(pressure=pressure)
                    self.assertGreater(self.domain.memory, 1048576)
        for content in ("MemTotal: 1000 kB\n", "MemTotal: broken kB\nMemAvailable: 0 kB\n",
                        "MemTotal:\n", "MemTotal: 0 kB\nMemAvailable: 0 kB\n"):
            with self.subTest(content=content):
                with patch("builtins.open", mock_open(read_data=content)), self.assertLogs(level="ERROR"):
                    self.assertEqual(
                        self.controller.host_memory_pressure(), autoreclaim.HOST_PRESSURE_UNAVAILABLE)

    def test_poll_reads_host_once_and_prioritizes_idle_guests(self):
        idle = Domain("guest-idle")
        idle.usable = 900000
        busy = Domain("guest-busy")
        busy.usable = 100000
        excluded = Domain("guest-excluded")
        self.controller.monitored_vms.update(("guest-idle", "guest-busy"))
        self.controller.conn = Mock()
        self.controller.conn.listDomainsID.return_value = [1, 2, 3, 4]
        self.controller.conn.lookupByID.side_effect = [busy, self.domain, idle, excluded]
        with patch.object(self.controller, "host_memory_pressure", return_value=0.5) as pressure:
            with patch.object(self.controller, "process_domainID") as process:
                self.controller.process_domains()
        pressure.assert_called_once_with()
        self.assertEqual([call.args[0].name() for call in process.call_args_list],
                         ["guest-idle", "guest-a", "guest-busy"])
        self.assertTrue(all(call.args[1] == 0.5 for call in process.call_args_list))

    def test_read_only_guest_is_sampled_by_status_but_not_by_polling(self):
        self.controller.read_only_vms.add("guest-a")
        self.controller.conn = Mock()
        self.controller.conn.listDomainsID.return_value = [1]
        self.controller.conn.lookupByID.return_value = self.domain
        stats = {"actual": self.domain.memory, "usable": self.domain.usable,
                 "source": "guest-agent"}
        output = io.StringIO()
        with patch.object(self.controller, "_get_guest_agent_meminfo", return_value=stats) as sample, \
                patch.object(self.controller, "host_memory_pressure") as pressure, \
                patch.object(self.controller, "process_domainID") as process, \
                patch.object(self.controller, "dom_print_names"), \
                patch.object(self.controller, "_get_guest_agent_memory_pressure", return_value=(None, None)), \
                redirect_stdout(output):
            self.controller.process_domains()
            sample.assert_not_called()
            pressure.assert_not_called()
            process.assert_not_called()
            self.controller.status()

        sample.assert_called_once_with(self.domain)
        self.assertEqual(output.getvalue().splitlines()[1].split()[:2],
                         ["guest-a", "agent/read-only"])

    def test_poll_prioritizes_yield_score_and_leaves_zero_idle_last(self):
        secondary = Domain("secondary")
        secondary.memory, secondary.usable = 1000000, 400000
        critical = Domain("critical")
        critical.memory, critical.usable = 1000000, 600000
        busy = Domain("busy")
        busy.usable = 0
        self.controller.config["default"]["memory_weight"] = 1000
        self.controller.config["vms"].extend([
            {"name": "secondary", "memory_weight": 50},
            {"name": "critical", "memory_weight": 200},
            {"name": "busy", "memory_weight": 1},
        ])
        self.controller.monitored_vms.update(("secondary", "critical", "busy"))
        self.controller.conn = Mock()
        self.controller.conn.listDomainsID.return_value = [1, 2, 3, 4]
        self.controller.conn.lookupByID.side_effect = [critical, busy, self.domain, secondary]
        with patch.object(self.controller, "host_memory_pressure", return_value=1):
            with patch.object(self.controller, "process_domainID") as process:
                self.controller.process_domains()
        self.assertEqual([call.args[0].name() for call in process.call_args_list],
                         ["secondary", "critical", "guest-a", "busy"])

    def test_memory_weight_requires_integer_from_one_to_one_thousand(self):
        for scope in ("default", "vm"):
            for value in (0, -1, 1001, 100.0, True, False, None, "100"):
                with self.subTest(scope=scope, value=value):
                    config = {"default": {}, "vms": []}
                    if scope == "default":
                        config["default"]["memory_weight"] = value
                    else:
                        config["vms"] = [{"name": "guest-a", "memory_weight": value}]
                    with self.assertRaisesRegex(autoreclaim.ExitFailure, "memory_weight"):
                        self.controller._LibVirtAutoReclaim__validate_config_parameters(config)
        for value in (1, 100, 1000):
            config = {"default": {"memory_weight": value},
                      "vms": [{"name": "guest-a", "memory_weight": value}]}
            self.controller._LibVirtAutoReclaim__validate_config_parameters(config)

    def test_invalid_controller_parameters_are_rejected(self):
        for key, values in {
                "ewma_alpha": (0, -0.1, 1.1, True, None, float("nan")),
                "keep_free_ratio": (-0.1, 1.1, "0.1"),
                "idle_reclaim_threshold": (-0.1, 1.1, True, None, "0.5", float("nan"), float("inf")),
                "reclaim_ratio": (0, 0.051),
                "pd_kp": (0, float("inf")),
                "pd_kd": (-1, 11),
                "psi_threshold": (0, 101, float("nan"), float("inf"), True, 10 ** 1000),
                "host_min_free_ratio": (0, -0.1, 1.1, float("-inf"), None),
        }.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    scope = "general" if key in ("psi_threshold", "host_min_free_ratio") else "default"
                    config = {scope: {key: value}, "vms": []}
                    with self.assertRaises(autoreclaim.ExitFailure):
                        self.controller._LibVirtAutoReclaim__validate_config_parameters(config)

    def test_idle_reclaim_threshold_rejects_invalid_vm_values(self):
        for value in (-0.1, 1.1, True, None, "0.5", float("nan")):
            with self.subTest(value=value):
                config = {"vms": [{"name": "guest-a", "idle_reclaim_threshold": value}]}
                with self.assertRaisesRegex(autoreclaim.ExitFailure, "idle_reclaim_threshold"):
                    self.controller._LibVirtAutoReclaim__validate_config_parameters(config)

    def test_host_pressure_settings_are_only_accepted_in_general(self):
        for key in ("psi_threshold", "host_min_free_ratio"):
            for scope in ("default", "vm"):
                with self.subTest(key=key, scope=scope):
                    config = {"default": {}, "vms": []}
                    if scope == "default":
                        config["default"][key] = 0.5
                    else:
                        config["vms"] = [{"name": "guest-a", key: 0.5}]
                    with self.assertRaisesRegex(autoreclaim.ExitFailure, "only supported in general"):
                        self.controller._LibVirtAutoReclaim__validate_config_parameters(config)

    def test_general_rejects_vm_and_unknown_settings(self):
        for parameters in ({"keep_free_kb": 524288}, {"auto_reclaim": True},
                           {"memory_weight": 100}, {"psi_threshol": 5}):
            with self.subTest(parameters=parameters):
                config = {"general": parameters, "vms": []}
                with self.assertRaisesRegex(autoreclaim.ExitFailure, "Unsupported general parameters"):
                    self.controller._LibVirtAutoReclaim__validate_config_parameters(config)

    def test_invalid_grace_is_rejected(self):
        for value in (-1, 1.5, True, "60"):
            self.controller.config["vms"][0]["manual_increase_grace_seconds"] = value
            with self.subTest(value=value), self.assertRaises(autoreclaim.ExitFailure):
                self.controller._LibVirtAutoReclaim__validate_config_parameters(self.controller.config)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.conn = Mock()
        self.conn.listAllDomains.return_value = []
        self.libvirt = types.SimpleNamespace(
            libvirtError=type("LibvirtError", (Exception,), {}),
            VIR_DOMAIN_EVENT_DEFINED=0,
            VIR_DOMAIN_EVENT_UNDEFINED=1,
            VIR_DOMAIN_EVENT_STARTED=2,
            VIR_DOMAIN_EVENT_RESUMED=4,
            VIR_DOMAIN_EVENT_STOPPED=5,
            VIR_DOMAIN_EVENT_CRASHED=8,
            VIR_DOMAIN_EVENT_ID_LIFECYCLE=0,
            VIR_ERR_AGENT_UNRESPONSIVE=86,
            VIR_ERR_AGENT_COMMAND_TIMEOUT=112,
            VIR_ERR_WARNING=1,
            open=Mock(return_value=self.conn),
            registerErrorHandler=Mock(),
            virEventRegisterDefaultImpl=Mock(),
            virEventAddTimeout=Mock(return_value=7),
            virEventUpdateTimeout=Mock(),
            virEventRemoveTimeout=Mock(),
            virEventRunDefaultImpl=Mock(),
        )
        libvirt_patch = patch.object(autoreclaim, "libvirt", self.libvirt)
        libvirt_patch.start()
        self.addCleanup(libvirt_patch.stop)
        self.config_open = mock_open(read_data='{"default": {"auto_reclaim": true}, "vms": []}')
        config_patch = patch("builtins.open", self.config_open)
        config_patch.start()
        self.addCleanup(config_patch.stop)
        self.controller = autoreclaim.LibVirtAutoReclaim()
        self.domain = Mock()
        self.domain.name.return_value = "guest-new"
        self.domain.isActive.return_value = 1
        self.domain.XMLDesc.return_value = (
            "<domain><devices><memballoon autodeflate='on'>"
            "<stats period='2'/></memballoon></devices></domain>")
        self.conn.listAllDomains.return_value = [self.domain]

    def lifecycle(self, event):
        self.controller._LibVirtAutoReclaim__domain_lifecycle(
            self.conn, self.domain, event, 0, None)

    def test_status_cli_only_prints_table_even_with_excluded_domains(self):
        self.domain.info.return_value = (1, 8388608, 1048576, 1, 0)
        self.domain.memoryStats.return_value = {"actual": 1048576, "usable": 524288}
        excluded = Mock()
        excluded.name.return_value = "excluded"
        self.config_open.return_value.read.return_value = (
            '{"default": {"auto_reclaim": true}, '
            '"vms": [{"name": "excluded", "auto_reclaim": false}]}')
        self.conn.listAllDomains.return_value = [self.domain, excluded]
        self.conn.listDomainsID.return_value = [1]
        self.conn.lookupByID.return_value = self.domain
        for debug in (False, True):
            with self.subTest(debug=debug):
                output, errors = io.StringIO(), io.StringIO()
                root = autoreclaim.logging.getLogger()
                previous_level = root.level
                try:
                    with patch.object(root, "handlers", []):
                        with redirect_stdout(output), redirect_stderr(errors):
                            autoreclaim.libvirt_autoreclaim(
                                ["libvirt-autoreclaim"] + (["--debug"] if debug else []) + ["status"])
                finally:
                    root.setLevel(previous_level)
                self.assertEqual(len(output.getvalue().splitlines()), 2)
                self.assertEqual(output.getvalue().splitlines()[0].split()[0], "Domain")
                if debug:
                    self.assertIn("Connecting to libvirt at qemu:///system",
                                  errors.getvalue().splitlines())
                    self.assertIn("Monitoring guest-new", errors.getvalue().splitlines())
                else:
                    self.assertEqual(errors.getvalue(), "")

    def test_cli_hides_agent_unavailability_but_keeps_other_libvirt_errors(self):
        self.domain.XMLDesc.return_value = (
            "<domain><devices>"
            "<channel type='spicevmc'/>"
            "<channel type='unix'><target name='org.qemu.guest_agent.0'/></channel>"
            "</devices></domain>")
        self.domain.info.return_value = (1, 8388608, 1048576, 1, 0)
        self.conn.listDomainsID.return_value = [1]
        self.conn.lookupByID.return_value = self.domain

        def qemu_agent_command(*_args):
            report = self.libvirt.registerErrorHandler.call_args
            for code, message in ((42, "Unrelated libvirt failure"),
                                  (agent_code, "Guest agent unavailable")):
                if report is None:
                    print(message, file=sys.stderr)
                else:
                    handler, context = report.args
                    handler(context, (code, 10, message, 2, None, None, None, 0, 0))
            raise self.libvirt.libvirtError("Guest agent unavailable")

        qemu_module = types.SimpleNamespace(qemuAgentCommand=qemu_agent_command)
        for agent_code in (86, 112):
            for action in ("start", "status"):
                for debug in (False, True):
                    with self.subTest(agent_code=agent_code, action=action, debug=debug):
                        self.libvirt.registerErrorHandler.reset_mock()
                        output, errors = io.StringIO(), io.StringIO()
                        root = autoreclaim.logging.getLogger()
                        previous_level = root.level
                        try:
                            with patch.dict(sys.modules, {"libvirt_qemu": qemu_module}), \
                                    patch.object(root, "handlers", []), \
                                    patch.object(autoreclaim.LibVirtAutoReclaim, "daemon",
                                                 autospec=True,
                                                 side_effect=lambda controller: controller.status()), \
                                    redirect_stdout(output), redirect_stderr(errors):
                                autoreclaim.libvirt_autoreclaim(
                                    ["libvirt-autoreclaim"] + (["--debug"] if debug else []) + [action])
                        finally:
                            root.setLevel(previous_level)
                        self.assertIn("unavailable/read-only", output.getvalue().splitlines()[1].split())
                        self.assertIn("Unrelated libvirt failure", errors.getvalue())
                        self.assertEqual(errors.getvalue().count("Guest agent unavailable"), int(debug))
                        self.libvirt.registerErrorHandler.assert_called_with(None, None)

    def test_new_domains_follow_selection_and_xml_requirements(self):
        for event in (self.libvirt.VIR_DOMAIN_EVENT_DEFINED,
                      self.libvirt.VIR_DOMAIN_EVENT_STARTED,
                      self.libvirt.VIR_DOMAIN_EVENT_RESUMED):
            with self.subTest(event=event):
                self.controller.monitored_vms.clear()
                self.lifecycle(event)
                self.assertIn("guest-new", self.controller.monitored_vms)
        self.controller.config["default"]["auto_reclaim"] = False
        self.lifecycle(self.libvirt.VIR_DOMAIN_EVENT_DEFINED)
        self.assertNotIn("guest-new", self.controller.monitored_vms)
        self.controller.config["default"]["auto_reclaim"] = True
        for xml in ("<domain/>", "<domain>",
                    "<domain><devices><memballoon><stats period='6'/>"
                    "</memballoon></devices></domain>"):
            with self.subTest(xml=xml):
                self.domain.XMLDesc.return_value = xml
                self.lifecycle(self.libvirt.VIR_DOMAIN_EVENT_STARTED)
                self.assertNotIn("guest-new", self.controller.monitored_vms)

    def test_guest_agent_allows_monitoring_without_valid_balloon_stats_period(self):
        self.domain.XMLDesc.return_value = (
            "<domain><devices>"
            "<channel type='unix'><target name='org.qemu.guest_agent.0'/></channel>"
            "<memballoon autodeflate='on'/>"
            "</devices></domain>")

        with self.assertLogs(level="WARNING") as logs:
            self.lifecycle(self.libvirt.VIR_DOMAIN_EVENT_DEFINED)

        self.assertIn("balloon stats period is missing", logs.output[0])
        self.assertIn("guest-new", self.controller.monitored_vms)
        self.assertIn("guest-new", self.controller.guest_agents)
        self.assertEqual(self.controller.stats_periods["guest-new"], 5)

    def test_spice_channel_without_balloon_requires_guest_agent_and_is_read_only(self):
        spice_channel = (
            "<channel type='spicevmc'><target type='virtio' name='com.redhat.spice.0'/>"
            "</channel>")
        guest_agent_channel = (
            "<channel type='unix'><target name='org.qemu.guest_agent.0'/></channel>")
        self.domain.XMLDesc.return_value = "<domain><devices>{}</devices></domain>".format(
            spice_channel)
        self.lifecycle(self.libvirt.VIR_DOMAIN_EVENT_DEFINED)
        self.assertNotIn("guest-new", self.controller.monitored_vms)

        self.domain.XMLDesc.return_value = "<domain><devices>{}{}</devices></domain>".format(
            spice_channel, guest_agent_channel)
        self.lifecycle(self.libvirt.VIR_DOMAIN_EVENT_DEFINED)

        self.assertIn("guest-new", self.controller.monitored_vms)
        self.assertIn("guest-new", self.controller.guest_agents)
        self.assertIn("guest-new", self.controller.read_only_vms)
        self.assertNotIn("guest-new", self.controller.stats_periods)

    def test_virtio_mem_without_balloon_is_monitored_with_guest_agent(self):
        self.domain.XMLDesc.return_value = (
            "<domain><memory unit='KiB'>3145728</memory>"
            "<currentMemory unit='KiB'>3145728</currentMemory><devices>"
            "<memory model='virtio-mem'><target><size unit='KiB'>2097152</size>"
            "<block unit='KiB'>2048</block><requested unit='KiB'>524288</requested>"
            "<current unit='KiB'>524288</current></target></memory>"
            "<channel type='unix'><target name='org.qemu.guest_agent.0'/></channel>"
            "</devices></domain>")

        self.lifecycle(self.libvirt.VIR_DOMAIN_EVENT_DEFINED)

        self.assertIn("guest-new", self.controller.monitored_vms)
        self.assertIn("guest-new", self.controller.guest_agents)
        self.assertNotIn("guest-new", self.controller.read_only_vms)
        self.assertEqual(self.controller.vm_backends["guest-new"]["type"], "virtio-mem")
        self.assertEqual(self.controller.vm_backends["guest-new"]["device_count"], 1)
        self.assertEqual(self.controller.stats_periods["guest-new"], 5)
        vmem_info = self.controller._inspect_virtio_mem_devices(self.domain)
        self.assertEqual(vmem_info["base_ram_kb"], 1048576)
        self.assertEqual(vmem_info["total_current_kb"], 524288)

        self.domain.XMLDesc.return_value = self.domain.XMLDesc.return_value.replace(
            "<channel type='unix'><target name='org.qemu.guest_agent.0'/></channel>", "")
        self.lifecycle(self.libvirt.VIR_DOMAIN_EVENT_DEFINED)
        self.assertNotIn("guest-new", self.controller.monitored_vms)

    def test_virtio_mem_ignores_stats_period_when_balloon_model_is_none(self):
        self.domain.XMLDesc.return_value = (
            "<domain><memory unit='KiB'>3145728</memory>"
            "<currentMemory unit='KiB'>3145728</currentMemory><devices>"
            "<memballoon model='none'><stats period='1'/></memballoon>"
            "<memory model='virtio-mem'><target><size unit='KiB'>2097152</size>"
            "<block unit='KiB'>2048</block><requested unit='KiB'>524288</requested>"
            "<current unit='KiB'>524288</current></target></memory>"
            "<channel type='unix'><target name='org.qemu.guest_agent.0'/></channel>"
            "</devices></domain>")

        self.lifecycle(self.libvirt.VIR_DOMAIN_EVENT_DEFINED)

        self.assertIn("guest-new", self.controller.monitored_vms)
        self.assertEqual(self.controller.stats_periods["guest-new"], 5)

    def test_poll_interval_ignores_inactive_domains(self):
        inactive = Mock()
        inactive.name.return_value = "guest-inactive"
        inactive.isActive.return_value = 0
        inactive.XMLDesc.return_value = (
            "<domain><devices><memballoon><stats period='1'/>"
            "</memballoon></devices></domain>")
        self.domain.XMLDesc.return_value = (
            "<domain><devices><memballoon><stats period='4'/>"
            "</memballoon></devices></domain>")
        self.conn.listAllDomains.return_value = [inactive, self.domain]

        with patch.object(autoreclaim.logging, "info") as info:
            self.controller.dom_print_names()

        self.assertEqual(self.controller.monitored_vms, {"guest-new"})
        self.assertEqual(self.controller.stats_periods, {"guest-new": 4})
        self.assertEqual(self.controller.poll_interval, 4)
        info.assert_any_call("Monitoring %s", "guest-new")
        self.assertNotIn(("Monitoring %s", "guest-inactive"),
                         [call.args for call in info.call_args_list])

    def test_stopped_crashed_and_removed_domains_clear_tracking(self):
        self.domain.ID.return_value = -1
        for event in (self.libvirt.VIR_DOMAIN_EVENT_STOPPED,
                      self.libvirt.VIR_DOMAIN_EVENT_CRASHED,
                      self.libvirt.VIR_DOMAIN_EVENT_UNDEFINED):
            with self.subTest(event=event):
                self.controller.monitored_vms.add("guest-new")
                self.controller.memory_targets["guest-new"] = 1048576
                self.controller.pending_targets["guest-new"] = (1048576, 100)
                self.controller.manual_increase_until["guest-new"] = 3600
                self.controller.control_samples["guest-new"] = autoreclaim.MemorySample(1, 1, 1, 100)
                self.lifecycle(event)
                self.assertNotIn("guest-new", self.controller.monitored_vms)
                self.assertNotIn("guest-new", self.controller.memory_targets)
                self.assertNotIn("guest-new", self.controller.pending_targets)
                self.assertNotIn("guest-new", self.controller.manual_increase_until)
                self.assertNotIn("guest-new", self.controller.control_samples)
                self.lifecycle(self.libvirt.VIR_DOMAIN_EVENT_STARTED)
                self.assertIn("guest-new", self.controller.monitored_vms)

    def test_undefining_running_guest_preserves_monitoring_and_grace(self):
        self.controller.monitored_vms.add("guest-new")
        self.controller.manual_increase_until["guest-new"] = 3600
        self.domain.ID.return_value = 1
        self.lifecycle(self.libvirt.VIR_DOMAIN_EVENT_UNDEFINED)
        self.assertIn("guest-new", self.controller.monitored_vms)
        self.assertEqual(self.controller.manual_increase_until["guest-new"], 3600)

    def test_start_reloads_configuration_for_new_and_existing_guests(self):
        existing = Mock()
        existing.name.return_value = "guest-existing"
        existing.XMLDesc.return_value = self.domain.XMLDesc.return_value
        self.conn.listAllDomains.return_value = [existing, self.domain]
        self.controller.monitored_vms.add("guest-existing")
        self.controller.memory_targets["guest-existing"] = 1048576
        self.controller.manual_increase_until["guest-existing"] = 3600
        self.controller.control_samples["guest-existing"] = autoreclaim.MemorySample(1, 1, 1, 100)
        self.config_open.return_value.read.return_value = (
            '{"default": {"auto_reclaim": false}, "vms": ['
            '{"name": "guest-new", "auto_reclaim": true, "keep_free_kb": 786432}]}')
        self.lifecycle(self.libvirt.VIR_DOMAIN_EVENT_STARTED)
        self.assertEqual(self.controller.monitored_vms, {"guest-new"})
        self.assertEqual(self.controller.dom_keep_usable(self.domain), 786432)
        self.assertNotIn("guest-existing", self.controller.memory_targets)
        self.assertNotIn("guest-existing", self.controller.manual_increase_until)
        self.assertEqual(self.controller.control_samples, {})

        self.config_open.return_value.read.return_value = (
            '{"default": {"auto_reclaim": false}, "vms": []}')
        self.lifecycle(self.libvirt.VIR_DOMAIN_EVENT_STARTED)
        self.assertEqual(self.controller.monitored_vms, set())

    def test_failed_reload_keeps_previous_valid_configuration(self):
        previous_config = self.controller.config
        sample = autoreclaim.MemorySample(1, 1, 1, 100)
        self.controller.control_samples["guest-existing"] = sample
        for content in ('{', '{"default": {"ewma_alpha": -1}, "vms": []}',
                        '{"general": [], "vms": []}', '{"general": null, "vms": []}',
                        '{"default": [], "vms": []}', '{"vms": [null]}',
                        '{"vms": {}}', 'null', '{}'):
            with self.subTest(content=content):
                self.config_open.return_value.read.return_value = content
                with self.assertLogs(level="ERROR") as logs:
                    self.lifecycle(self.libvirt.VIR_DOMAIN_EVENT_STARTED)
                self.assertIn("keeping previous configuration", logs.output[0])
                self.assertIs(self.controller.config, previous_config)
                self.assertIn("guest-new", self.controller.monitored_vms)
                self.assertIs(self.controller.control_samples["guest-existing"], sample)
        self.config_open.side_effect = OSError("cannot read configuration")
        with self.assertLogs(level="ERROR") as logs:
            self.lifecycle(self.libvirt.VIR_DOMAIN_EVENT_STARTED)
        self.assertIn("cannot read configuration", logs.output[0])
        self.assertIs(self.controller.config, previous_config)
        self.assertIn("guest-new", self.controller.monitored_vms)

    def test_definition_and_resume_do_not_reload_configuration(self):
        self.config_open.reset_mock()
        self.lifecycle(self.libvirt.VIR_DOMAIN_EVENT_DEFINED)
        self.lifecycle(self.libvirt.VIR_DOMAIN_EVENT_RESUMED)
        self.config_open.assert_not_called()

    def test_configuration_logs_loading_then_only_changed_reloads(self):
        with self.assertLogs(level="INFO") as logs:
            self.controller = autoreclaim.LibVirtAutoReclaim()
            self.lifecycle(self.libvirt.VIR_DOMAIN_EVENT_STARTED)
            self.config_open.return_value.read.return_value = (
                '{"default": {"auto_reclaim": true, "keep_free_kb": 786432}, "vms": []}')
            self.lifecycle(self.libvirt.VIR_DOMAIN_EVENT_STARTED)
            self.config_open.return_value.read.return_value = json.dumps(
                self.controller.config, indent=2, sort_keys=True)
            self.lifecycle(self.libvirt.VIR_DOMAIN_EVENT_STARTED)
        self.assertEqual(
            [record.getMessage() for record in logs.records
             if "config file:" in record.getMessage()],
            ["Loading config file: /etc/libvirt/autoreclaim.json",
             "Reloading config file: /etc/libvirt/autoreclaim.json"])

    def test_monitoring_logs_only_changes_across_reload_and_lifecycle_events(self):
        with patch.object(autoreclaim.logging, "info") as info:
            self.controller.dom_print_names()
            info.assert_any_call("Monitoring %s", "guest-new")
            info.reset_mock()
            self.lifecycle(self.libvirt.VIR_DOMAIN_EVENT_DEFINED)
            self.lifecycle(self.libvirt.VIR_DOMAIN_EVENT_STARTED)
            self.lifecycle(self.libvirt.VIR_DOMAIN_EVENT_RESUMED)
            self.assertFalse(any("Monitoring" in call.args[0] for call in info.call_args_list))

            info.reset_mock()
            self.config_open.return_value.read.return_value = (
                '{"default": {"auto_reclaim": false}, "vms": []}')
            self.lifecycle(self.libvirt.VIR_DOMAIN_EVENT_STARTED)
            info.assert_any_call("Stopped monitoring %s", "guest-new")
            info.reset_mock()
            self.lifecycle(self.libvirt.VIR_DOMAIN_EVENT_STARTED)
            self.assertFalse(any("monitoring" in call.args[0].lower()
                                 for call in info.call_args_list))

            info.reset_mock()
            self.config_open.return_value.read.return_value = (
                '{"default": {"auto_reclaim": true}, "vms": []}')
            self.lifecycle(self.libvirt.VIR_DOMAIN_EVENT_STARTED)
            info.assert_any_call("Monitoring %s", "guest-new")
            info.reset_mock()
            self.lifecycle(self.libvirt.VIR_DOMAIN_EVENT_STOPPED)
            self.lifecycle(self.libvirt.VIR_DOMAIN_EVENT_STOPPED)
            self.assertEqual(
                [call.args for call in info.call_args_list if "monitoring" in call.args[0]],
                [("Stopped monitoring %s", "guest-new")])

    def test_inventory_removal_logs_once_and_clears_tracking(self):
        self.controller.dom_print_names()
        self.controller.memory_targets["guest-new"] = 1048576
        self.controller.manual_increase_until["guest-new"] = 3600
        self.conn.listAllDomains.return_value = []
        with patch.object(autoreclaim.logging, "info") as info:
            self.controller.dom_print_names()
            self.controller.dom_print_names()
        self.assertEqual(
            [call.args for call in info.call_args_list if "monitoring" in call.args[0]],
            [("Stopped monitoring %s", "guest-new")])
        self.assertNotIn("guest-new", self.controller.monitored_vms)
        self.assertNotIn("guest-new", self.controller.stats_periods)
        self.assertNotIn("guest-new", self.controller.memory_targets)
        self.assertNotIn("guest-new", self.controller.manual_increase_until)

    def test_poll_interval_tracks_discovery_period_changes_and_removal(self):
        slow = Mock()
        slow.name.return_value = "guest-slow"
        slow.XMLDesc.return_value = (
            "<domain><devices><memballoon><stats period='5'/>"
            "</memballoon></devices></domain>")
        self.conn.listAllDomains.return_value = [slow]
        self.controller.dom_print_names()
        self.assertEqual(self.controller.poll_interval, 5)
        self.libvirt.virEventUpdateTimeout.assert_not_called()
        self.controller.timer_id = 7

        self.domain.XMLDesc.return_value = (
            "<domain><devices><memballoon><stats period='1'/>"
            "</memballoon></devices></domain>")
        self.lifecycle(self.libvirt.VIR_DOMAIN_EVENT_DEFINED)
        self.assertEqual(self.controller.poll_interval, 1)
        self.libvirt.virEventUpdateTimeout.assert_called_once_with(7, 1000)
        self.libvirt.virEventUpdateTimeout.reset_mock()
        self.lifecycle(self.libvirt.VIR_DOMAIN_EVENT_RESUMED)
        self.libvirt.virEventUpdateTimeout.assert_not_called()

        self.domain.XMLDesc.return_value = (
            "<domain><devices><memballoon><stats period='3'/>"
            "</memballoon></devices></domain>")
        self.lifecycle(self.libvirt.VIR_DOMAIN_EVENT_DEFINED)
        self.assertEqual(self.controller.poll_interval, 3)
        self.libvirt.virEventUpdateTimeout.assert_called_once_with(7, 3000)
        self.lifecycle(self.libvirt.VIR_DOMAIN_EVENT_STOPPED)
        self.assertNotIn("guest-new", self.controller.stats_periods)
        self.assertEqual(self.controller.poll_interval, 5)
        self.libvirt.virEventUpdateTimeout.assert_called_with(7, 5000)

        self.conn.listAllDomains.return_value = []
        self.controller.dom_print_names()
        self.assertEqual(self.controller.stats_periods, {})
        self.assertEqual(self.controller.poll_interval, 5)
        self.libvirt.virEventUpdateTimeout.assert_called_with(7, 5000)

    def test_excluded_or_invalid_periods_do_not_affect_poll_interval(self):
        excluded = Mock()
        excluded.name.return_value = "guest-excluded"
        excluded.XMLDesc.return_value = (
            "<domain><devices><memballoon><stats period='1'/>"
            "</memballoon></devices></domain>")
        self.controller.config["vms"] = [{"name": "guest-excluded", "auto_reclaim": False}]
        self.domain.XMLDesc.return_value = (
            "<domain><devices><memballoon><stats period='5'/>"
            "</memballoon></devices></domain>")
        self.conn.listAllDomains.return_value = [excluded, self.domain]
        self.controller.dom_print_names()
        self.assertEqual(self.controller.poll_interval, 5)
        self.assertEqual(self.controller.stats_periods, {"guest-new": 5})

        self.domain.XMLDesc.return_value = "<domain/>"
        self.lifecycle(self.libvirt.VIR_DOMAIN_EVENT_DEFINED)
        self.assertEqual(self.controller.poll_interval, 5)
        self.assertEqual(self.controller.stats_periods, {})

    def test_config_reload_recalculates_minimum_period(self):
        self.domain.XMLDesc.return_value = (
            "<domain><devices><memballoon><stats period='1'/>"
            "</memballoon></devices></domain>")
        self.controller.dom_print_names()
        self.assertEqual(self.controller.poll_interval, 1)
        self.controller.timer_id = 7
        self.config_open.return_value.read.return_value = (
            '{"default": {"auto_reclaim": false}, "vms": []}')
        self.lifecycle(self.libvirt.VIR_DOMAIN_EVENT_STARTED)
        self.assertEqual(self.controller.monitored_vms, set())
        self.assertEqual(self.controller.stats_periods, {})
        self.assertEqual(self.controller.poll_interval, 5)
        self.libvirt.virEventUpdateTimeout.assert_called_once_with(7, 5000)

    def test_unavailable_domain_does_not_break_event_dispatch(self):
        self.domain.name.side_effect = self.libvirt.libvirtError("domain disappeared")
        with self.assertLogs(level="WARNING") as logs:
            self.lifecycle(self.libvirt.VIR_DOMAIN_EVENT_DEFINED)
        self.assertIn("domain disappeared", logs.output[0])

    def test_daemon_dispatches_discovery_and_polls_on_timer(self):
        self.conn.domainEventRegisterAny.return_value = 3
        self.conn.listAllDomains.return_value = []
        self.conn.listDomainsID.side_effect = [[], [1]]
        self.conn.lookupByID.return_value = self.domain
        self.controller.process_domainID = Mock()
        self.controller.host_memory_pressure = Mock(return_value=0.5)
        memstat = {"actual": 1048576, "usable": 524288, "last_update": 100}
        self.domain.memoryStats.return_value = memstat

        def dispatch():
            if self.libvirt.virEventRunDefaultImpl.call_count == 1:
                args = self.conn.domainEventRegisterAny.call_args.args
                args[2](self.conn, self.domain, self.libvirt.VIR_DOMAIN_EVENT_DEFINED, 0, args[3])
                self.controller.process_domainID.assert_not_called()
                timer_args = self.libvirt.virEventAddTimeout.call_args.args
                timer_args[1](7, timer_args[2])
                return 0
            raise KeyboardInterrupt

        self.libvirt.virEventRunDefaultImpl.side_effect = dispatch
        with self.assertRaises(KeyboardInterrupt):
            self.controller.daemon()
        self.libvirt.open.assert_called_once_with("qemu:///system")
        self.libvirt.virEventRegisterDefaultImpl.assert_called_once_with()
        self.assertEqual(self.conn.mock_calls[0][0], "domainEventRegisterAny")
        self.assertEqual(self.libvirt.virEventAddTimeout.call_args.args[0], 5000)
        self.libvirt.virEventUpdateTimeout.assert_called_once_with(7, 2000)
        self.controller.process_domainID.assert_called_once_with(self.domain, 0.5, memstat)
        self.libvirt.virEventRemoveTimeout.assert_called_once_with(7)
        self.conn.domainEventDeregisterAny.assert_called_once_with(3)

    def test_daemon_registers_timer_with_discovered_minimum_period(self):
        self.domain.XMLDesc.return_value = (
            "<domain><devices><memballoon><stats period='5'/>"
            "</memballoon></devices></domain>")
        self.controller.process_domains = Mock()
        self.libvirt.virEventRunDefaultImpl.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.controller.daemon()
        self.assertEqual(self.libvirt.virEventAddTimeout.call_args.args[0], 5000)
        self.libvirt.virEventUpdateTimeout.assert_not_called()
        self.libvirt.virEventRemoveTimeout.assert_called_once_with(7)
        self.assertIsNone(self.controller.timer_id)

    def test_daemon_registers_and_removes_virtio_mem_callback_when_supported(self):
        self.libvirt.VIR_DOMAIN_EVENT_ID_MEMORY_DEVICE_SIZE_CHANGE = 9
        self.conn.domainEventRegisterAny.side_effect = [3, 4]
        self.libvirt.virEventRunDefaultImpl.side_effect = KeyboardInterrupt
        self.controller.process_domains = Mock()

        with self.assertRaises(KeyboardInterrupt):
            self.controller.daemon()

        registrations = self.conn.domainEventRegisterAny.call_args_list
        self.assertEqual(registrations[0].args[1], self.libvirt.VIR_DOMAIN_EVENT_ID_LIFECYCLE)
        self.assertEqual(
            registrations[1].args[1],
            self.libvirt.VIR_DOMAIN_EVENT_ID_MEMORY_DEVICE_SIZE_CHANGE)
        self.assertEqual(
            self.conn.domainEventDeregisterAny.call_args_list[0].args, (3,))
        self.assertEqual(
            self.conn.domainEventDeregisterAny.call_args_list[1].args, (4,))

    def test_event_loop_failure_is_reported_and_registrations_are_removed(self):
        self.conn.domainEventRegisterAny.return_value = 3
        self.conn.listDomainsID.return_value = []
        self.libvirt.virEventRunDefaultImpl.return_value = -1
        with self.assertRaisesRegex(autoreclaim.ExitFailure, "event loop failed"):
            self.controller.daemon()
        self.libvirt.virEventRemoveTimeout.assert_called_once_with(7)
        self.conn.domainEventDeregisterAny.assert_called_once_with(3)

    def test_closed_socket_attempts_all_cleanup_before_reconnecting(self):
        self.libvirt.VIR_DOMAIN_EVENT_ID_MEMORY_DEVICE_SIZE_CHANGE = 9
        self.conn.domainEventRegisterAny.side_effect = [3, 4]
        self.conn.listDomainsID.side_effect = self.libvirt.libvirtError("client socket is closed")
        self.libvirt.virEventRemoveTimeout.side_effect = self.libvirt.libvirtError("timer removal failed")
        self.conn.domainEventDeregisterAny.side_effect = self.libvirt.libvirtError("deregistration failed")
        self.conn.close.side_effect = self.libvirt.libvirtError("close failed")

        with patch.object(autoreclaim, "LibVirtAutoReclaim", return_value=self.controller):
            with patch.object(autoreclaim, "sleep", side_effect=KeyboardInterrupt) as sleep:
                with self.assertRaises(SystemExit) as stopped:
                    autoreclaim.libvirt_autoreclaim(["libvirt-autoreclaim", "start"])

        self.assertEqual(stopped.exception.code, 130)
        self.assertTrue(stopped.exception.__suppress_context__)
        sleep.assert_called_once_with(1)
        self.libvirt.virEventRemoveTimeout.assert_called_once_with(7)
        self.assertIsNone(self.controller.timer_id)
        self.assertEqual([call.args for call in self.conn.domainEventDeregisterAny.call_args_list],
                         [(3,), (4,)])
        self.conn.close.assert_called_once_with()
        self.assertIsNone(self.controller.conn)

    def test_daemon_reconnects_with_backoff_and_rediscovers_domains(self):
        self.conn.domainEventRegisterAny.return_value = 3
        self.conn.listDomainsID.side_effect = self.libvirt.libvirtError("client socket is closed")
        new_conn = Mock()
        new_conn.domainEventRegisterAny.return_value = 5
        new_conn.listAllDomains.return_value = []
        new_conn.listDomainsID.return_value = []
        self.libvirt.open.side_effect = [None, self.libvirt.libvirtError("refused"), new_conn]
        self.libvirt.virEventRunDefaultImpl.side_effect = KeyboardInterrupt

        with patch.object(autoreclaim, "sleep") as sleep:
            with self.assertRaises(KeyboardInterrupt):
                self.controller.daemon()

        self.assertEqual([call.args for call in sleep.call_args_list], [(1,), (2,), (4,)])
        self.assertEqual(self.libvirt.open.call_count, 4)
        self.libvirt.open.assert_called_with("qemu:///system")
        self.conn.close.assert_called_once_with()
        self.assertIs(self.controller.conn, new_conn)
        new_conn.listAllDomains.assert_called_once()
        new_conn.listDomainsID.assert_called_once_with()
        new_conn.domainEventDeregisterAny.assert_called_once_with(5)
        self.assertEqual(self.controller.reconnect_delay, 1)
        self.libvirt.virEventAddTimeout.assert_called_once()

    def test_cli_closes_connection_on_failure(self):
        with patch.object(autoreclaim, "LibVirtAutoReclaim", return_value=self.controller):
            with patch.object(self.controller, "daemon", side_effect=autoreclaim.ExitFailure):
                with self.assertRaises(autoreclaim.ExitFailure):
                    autoreclaim.libvirt_autoreclaim(["libvirt-autoreclaim", "start"])
        self.conn.close.assert_called_once_with()

    def test_sigterm_removes_timer_deregisters_events_and_closes_connection(self):
        self.conn.domainEventRegisterAny.return_value = 3
        self.conn.listDomainsID.return_value = []
        previous_handler = Mock()
        with patch.object(autoreclaim, "LibVirtAutoReclaim", return_value=self.controller):
            with patch.object(autoreclaim.signal, "signal", return_value=previous_handler) as signals:
                self.libvirt.virEventRunDefaultImpl.side_effect = lambda: (
                    signals.call_args_list[0].args[1](autoreclaim.signal.SIGTERM, None))
                with self.assertRaises(SystemExit) as stopped:
                    autoreclaim.libvirt_autoreclaim(["libvirt-autoreclaim", "start"])
                self.assertEqual(stopped.exception.code, 143)
                self.assertEqual(signals.call_count, 2)
                signals.assert_called_with(autoreclaim.signal.SIGTERM, previous_handler)
        self.libvirt.virEventRemoveTimeout.assert_called_once_with(7)
        self.conn.domainEventDeregisterAny.assert_called_once_with(3)
        self.conn.close.assert_called_once_with()

    def test_termination_during_configuration_load_closes_connection(self):
        with patch.object(autoreclaim.LibVirtAutoReclaim, "_LibVirtAutoReclaim__load_config",
                          side_effect=SystemExit(143)):
            with self.assertRaises(SystemExit):
                autoreclaim.LibVirtAutoReclaim()
        self.conn.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()