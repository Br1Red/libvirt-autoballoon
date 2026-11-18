import importlib.util
import sys
import types
import unittest
from unittest.mock import Mock, mock_open, patch


sys.modules.setdefault("libvirt", types.SimpleNamespace(
    libvirtError=type("LibvirtError", (Exception,), {})))
spec = importlib.util.spec_from_file_location("autoballoon", "libvirt-autoballoon.py")
autoballoon = importlib.util.module_from_spec(spec)
spec.loader.exec_module(autoballoon)


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


class BalloonAdjustmentTests(unittest.TestCase):
    def test_sub_mib_changes_are_skipped_even_across_mib_boundaries(self):
        actual = 1048576 + 1000
        for delta in (-1020, -900, -1, 0, 1, 1023):
            with self.subTest(delta=delta):
                domain = Domain()
                domain.memory = actual
                requested = autoballoon.dom_balloon(domain, actual + delta, actual, domain.maximum)
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
                requested = autoballoon.dom_balloon(domain, target, actual, domain.maximum)
                self.assertEqual(requested, actual + aligned_delta)
                self.assertEqual(domain.calls, [actual + aligned_delta])

    def test_minimum_change_is_checked_after_capping_to_maximum(self):
        actual = 1048576 + 1000
        domain = Domain()
        domain.memory = actual
        domain.maximum = actual + 1023
        requested = autoballoon.dom_balloon(domain, actual + 2048, actual, domain.maximum)
        self.assertIsNone(requested)
        self.assertEqual(domain.calls, [])

    def test_target_is_page_aligned_after_capping_to_unaligned_maximum(self):
        domain = Domain()
        domain.maximum = domain.memory + 2047
        expected = domain.memory + 2044
        requested = autoballoon.dom_balloon(
            domain, domain.memory + 4096, domain.memory, domain.maximum)
        self.assertEqual(requested, expected)
        self.assertEqual(domain.calls, [expected])


class AdaptiveControllerTests(unittest.TestCase):
    def setUp(self):
        self.controller = autoballoon.LibVirtAutoBalloon.__new__(autoballoon.LibVirtAutoBalloon)
        self.controller.config = {"default": {}, "vms": [
            {"name": "guest-a", "balloon": True, "manual_increase_grace_seconds": 3600}
        ]}
        self.controller.monitored_vms = {"guest-a"}
        self.controller.memory_targets = {}
        self.controller.pending_targets = {}
        self.controller.manual_increase_until = {}
        self.controller.control_samples = {}
        self.domain = Domain()
        self.now = 100
        for clock in ("monotonic", "time"):
            clock_patch = patch.object(autoballoon, clock, side_effect=lambda: self.now)
            clock_patch.start()
            self.addCleanup(clock_patch.stop)

    def step(self, seconds=2, pressure=1):
        self.now += seconds
        self.domain.updated = self.now
        self.controller.process_domainID(self.domain, pressure)

    def test_no_change_at_reserve(self):
        self.domain.memory = 1310720
        self.step()
        self.assertEqual(self.domain.calls, [])

    def test_reclaim_scales_with_pressure_and_idle_ratio_and_is_capped(self):
        reductions = []
        for pressure, expected in ((0, 0), (0.5, 47188), (1, 94372)):
            with self.subTest(pressure=pressure):
                self.domain = Domain()
                self.controller.control_samples.clear()
                self.controller.memory_targets.clear()
                self.controller.pending_targets.clear()
                self.domain.memory = 4194304
                self.domain.usable = 3145728
                self.step(pressure=pressure)
                reduction = 4194304 - self.domain.memory
                self.assertLessEqual(reduction, 4194304 * 0.03)
                self.assertEqual(reduction, expected)
                reductions.append(reduction)
        self.assertEqual(reductions[0], 0)
        self.assertLess(reductions[1], reductions[2])

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
        self.controller.control_samples["guest-a"] = autoballoon.MemorySample(
            1925120, 1925120, -131072, 99)
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
                self.controller.control_samples["guest-a"] = autoballoon.MemorySample(
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
            with patch.object(autoballoon.logging, "warning") as warning:
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
        self.controller._LibVirtAutoBalloon__validate_config_parameters(self.controller.config)
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
                    self.assertEqual(pressure, 0)
                    self.assertIn("reclaim disabled", logs.output[0])
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
                    self.assertEqual(self.controller.host_memory_pressure(), 0)

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

    def test_invalid_controller_parameters_are_rejected(self):
        for key, values in {
                "ewma_alpha": (0, -0.1, 1.1, True, None, float("nan")),
                "keep_free_ratio": (-0.1, 1.1, "0.1"),
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
                    with self.assertRaises(autoballoon.ExitFailure):
                        self.controller._LibVirtAutoBalloon__validate_config_parameters(config)

    def test_host_pressure_settings_are_only_accepted_in_general(self):
        for key in ("psi_threshold", "host_min_free_ratio"):
            for scope in ("default", "vm"):
                with self.subTest(key=key, scope=scope):
                    config = {"default": {}, "vms": []}
                    if scope == "default":
                        config["default"][key] = 0.5
                    else:
                        config["vms"] = [{"name": "guest-a", key: 0.5}]
                    with self.assertRaisesRegex(autoballoon.ExitFailure, "only supported in general"):
                        self.controller._LibVirtAutoBalloon__validate_config_parameters(config)

    def test_general_rejects_vm_and_unknown_settings(self):
        for parameters in ({"keep_free_kb": 524288}, {"balloon": True}, {"psi_threshol": 5}):
            with self.subTest(parameters=parameters):
                config = {"general": parameters, "vms": []}
                with self.assertRaisesRegex(autoballoon.ExitFailure, "Unsupported general parameters"):
                    self.controller._LibVirtAutoBalloon__validate_config_parameters(config)

    def test_invalid_grace_is_rejected(self):
        for value in (-1, 1.5, True, "60"):
            self.controller.config["vms"][0]["manual_increase_grace_seconds"] = value
            with self.subTest(value=value), self.assertRaises(autoballoon.ExitFailure):
                self.controller._LibVirtAutoBalloon__validate_config_parameters(self.controller.config)


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
            open=Mock(return_value=self.conn),
            virEventRegisterDefaultImpl=Mock(),
            virEventAddTimeout=Mock(return_value=7),
            virEventRemoveTimeout=Mock(),
            virEventRunDefaultImpl=Mock(),
        )
        libvirt_patch = patch.object(autoballoon, "libvirt", self.libvirt)
        libvirt_patch.start()
        self.addCleanup(libvirt_patch.stop)
        self.config_open = mock_open(read_data='{"default": {"balloon": true}, "vms": []}')
        config_patch = patch("builtins.open", self.config_open)
        config_patch.start()
        self.addCleanup(config_patch.stop)
        self.controller = autoballoon.LibVirtAutoBalloon()
        self.domain = Mock()
        self.domain.name.return_value = "guest-new"
        self.domain.XMLDesc.return_value = (
            "<domain><devices><memballoon autodeflate='on'>"
            "<stats period='2'/></memballoon></devices></domain>")
        self.conn.listAllDomains.return_value = [self.domain]

    def lifecycle(self, event):
        self.controller._LibVirtAutoBalloon__domain_lifecycle(
            self.conn, self.domain, event, 0, None)

    def test_new_domains_follow_selection_and_xml_requirements(self):
        for event in (self.libvirt.VIR_DOMAIN_EVENT_DEFINED,
                      self.libvirt.VIR_DOMAIN_EVENT_STARTED,
                      self.libvirt.VIR_DOMAIN_EVENT_RESUMED):
            with self.subTest(event=event):
                self.controller.monitored_vms.clear()
                self.lifecycle(event)
                self.assertIn("guest-new", self.controller.monitored_vms)
        self.controller.config["default"]["balloon"] = False
        self.lifecycle(self.libvirt.VIR_DOMAIN_EVENT_DEFINED)
        self.assertNotIn("guest-new", self.controller.monitored_vms)
        self.controller.config["default"]["balloon"] = True
        for xml in ("<domain/>", "<domain>",
                    "<domain><devices><memballoon><stats period='6'/>"
                    "</memballoon></devices></domain>"):
            with self.subTest(xml=xml):
                self.domain.XMLDesc.return_value = xml
                self.lifecycle(self.libvirt.VIR_DOMAIN_EVENT_STARTED)
                self.assertNotIn("guest-new", self.controller.monitored_vms)

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
                self.controller.control_samples["guest-new"] = autoballoon.MemorySample(1, 1, 1, 100)
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
        self.controller.control_samples["guest-existing"] = autoballoon.MemorySample(1, 1, 1, 100)
        self.config_open.return_value.read.return_value = (
            '{"default": {"balloon": false}, "vms": ['
            '{"name": "guest-new", "balloon": true, "keep_free_kb": 786432}]}')
        self.lifecycle(self.libvirt.VIR_DOMAIN_EVENT_STARTED)
        self.assertEqual(self.controller.monitored_vms, {"guest-new"})
        self.assertEqual(self.controller.dom_keep_usable(self.domain), 786432)
        self.assertNotIn("guest-existing", self.controller.memory_targets)
        self.assertNotIn("guest-existing", self.controller.manual_increase_until)
        self.assertEqual(self.controller.control_samples, {})

        self.config_open.return_value.read.return_value = (
            '{"default": {"balloon": false}, "vms": []}')
        self.lifecycle(self.libvirt.VIR_DOMAIN_EVENT_STARTED)
        self.assertEqual(self.controller.monitored_vms, set())

    def test_failed_reload_keeps_previous_valid_configuration(self):
        previous_config = self.controller.config
        sample = autoballoon.MemorySample(1, 1, 1, 100)
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
        self.assertEqual(self.libvirt.virEventAddTimeout.call_args.args[0], 2000)
        self.controller.process_domainID.assert_called_once_with(self.domain, 0.5, memstat)
        self.libvirt.virEventRemoveTimeout.assert_called_once_with(7)
        self.conn.domainEventDeregisterAny.assert_called_once_with(3)

    def test_event_loop_failure_is_reported_and_registrations_are_removed(self):
        self.conn.domainEventRegisterAny.return_value = 3
        self.conn.listDomainsID.return_value = []
        self.libvirt.virEventRunDefaultImpl.return_value = -1
        with self.assertRaisesRegex(autoballoon.ExitFailure, "event loop failed"):
            self.controller.daemon()
        self.libvirt.virEventRemoveTimeout.assert_called_once_with(7)
        self.conn.domainEventDeregisterAny.assert_called_once_with(3)

    def test_cli_closes_connection_on_failure(self):
        with patch.object(autoballoon, "LibVirtAutoBalloon", return_value=self.controller):
            with patch.object(self.controller, "daemon", side_effect=autoballoon.ExitFailure):
                with self.assertRaises(autoballoon.ExitFailure):
                    autoballoon.libvirt_autoballoon(["libvirt-autoballoon", "start"])
        self.conn.close.assert_called_once_with()

    def test_sigterm_removes_timer_deregisters_events_and_closes_connection(self):
        self.conn.domainEventRegisterAny.return_value = 3
        self.conn.listDomainsID.return_value = []
        previous_handler = Mock()
        with patch.object(autoballoon, "LibVirtAutoBalloon", return_value=self.controller):
            with patch.object(autoballoon.signal, "signal", return_value=previous_handler) as signals:
                self.libvirt.virEventRunDefaultImpl.side_effect = lambda: (
                    signals.call_args_list[0].args[1](autoballoon.signal.SIGTERM, None))
                with self.assertRaises(SystemExit) as stopped:
                    autoballoon.libvirt_autoballoon(["libvirt-autoballoon", "start"])
                self.assertEqual(stopped.exception.code, 143)
                self.assertEqual(signals.call_count, 2)
                signals.assert_called_with(autoballoon.signal.SIGTERM, previous_handler)
        self.libvirt.virEventRemoveTimeout.assert_called_once_with(7)
        self.conn.domainEventDeregisterAny.assert_called_once_with(3)
        self.conn.close.assert_called_once_with()

    def test_termination_during_configuration_load_closes_connection(self):
        with patch.object(autoballoon.LibVirtAutoBalloon, "_LibVirtAutoBalloon__load_config",
                          side_effect=SystemExit(143)):
            with self.assertRaises(SystemExit):
                autoballoon.LibVirtAutoBalloon()
        self.conn.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()