import importlib.util
import sys
import types
import unittest
from unittest.mock import mock_open, patch


sys.modules.setdefault("libvirt", types.SimpleNamespace(libvirtError=Exception))
spec = importlib.util.spec_from_file_location("autoballoon", "libvirt-autoballoon.py")
autoballoon = importlib.util.module_from_spec(spec)
spec.loader.exec_module(autoballoon)


class Domain:
    def __init__(self):
        self.memory = 1048576
        self.usable = 524288
        self.calls = []

    def name(self):
        return "guest-a"

    def info(self):
        return (1, 4194304, self.memory, 1, 0)

    def memoryStats(self):
        return {"actual": self.memory, "usable": self.usable}

    def setMemory(self, target):
        self.calls.append(target)
        self.memory = target


class ManualIncreaseTests(unittest.TestCase):
    def setUp(self):
        self.controller = autoballoon.LibVirtAutoBalloon.__new__(autoballoon.LibVirtAutoBalloon)
        self.controller.config = {"default": {}, "vms": [
            {"name": "guest-a", "balloon": True, "manual_increase_grace_seconds": 3600}
        ]}
        self.controller.monitored_vms = {"guest-a"}
        self.controller.memory_targets = {}
        self.controller.manual_increase_until = {}
        self.domain = Domain()
        self.meminfo = mock_open(read_data="MemTotal: 1000 kB\nMemAvailable: 500 kB\n")
        self.open_patch = patch("builtins.open", self.meminfo)
        self.open_patch.start()
        self.addCleanup(self.open_patch.stop)

    def test_host_memory_threshold_controls_reductions(self):
        for available in (499, 500, 501):
            with self.subTest(available=available):
                self.meminfo.return_value.read.return_value = (
                    "MemTotal: 1000 kB\nMemAvailable: {} kB\n".format(available))
                self.domain.memory = 2097152
                self.domain.usable = 1572864
                self.domain.calls.clear()
                self.controller.process_domainID(self.domain)
                self.assertEqual(self.domain.calls, [] if available > 500 else [1310720])

    def test_growth_is_allowed_regardless_of_host_memory_threshold(self):
        for available in (499, 500, 501):
            with self.subTest(available=available):
                self.meminfo.return_value.read.return_value = (
                    "MemTotal: 1000 kB\nMemAvailable: {} kB\n".format(available))
                self.domain.memory = 1048576
                self.domain.usable = 100000
                self.domain.calls.clear()
                self.controller.process_domainID(self.domain)
                self.assertEqual(self.domain.calls, [1735008])

    def test_no_change_within_guest_thresholds(self):
        self.controller.process_domainID(self.domain)
        self.assertEqual(self.domain.calls, [])

    def test_invalid_host_memory_stats_prevent_reduction(self):
        for content in ("MemTotal: 1000 kB\n", "MemTotal: 0 kB\nMemAvailable: 0 kB\n"):
            with self.subTest(content=content):
                self.meminfo.return_value.read.return_value = content
                self.domain.memory = 2097152
                self.domain.usable = 1572864
                with self.assertRaises(autoballoon.ExitFailure):
                    self.controller.process_domainID(self.domain)
                self.assertEqual(self.domain.calls, [])

    def test_manual_increase_defers_reduction_until_grace_expires(self):
        with patch.object(autoballoon, "monotonic", return_value=100):
            self.controller.process_domainID(self.domain)
        self.domain.memory = 2097152
        self.domain.usable = 1572864
        with patch.object(autoballoon, "monotonic", return_value=101):
            self.controller.process_domainID(self.domain)
        self.assertEqual(self.domain.calls, [])
        with patch.object(autoballoon, "monotonic", return_value=3701):
            self.controller.process_domainID(self.domain)
        self.assertEqual(len(self.domain.calls), 1)
        self.assertLess(self.domain.calls[0], 2097152)

    def test_own_increase_does_not_start_grace_period(self):
        self.domain.usable = 100000
        with patch.object(autoballoon, "monotonic", return_value=100):
            self.controller.process_domainID(self.domain)
        self.assertGreater(self.domain.calls[0], 1048576)
        self.domain.usable = 1572864
        with patch.object(autoballoon, "monotonic", return_value=101):
            self.controller.process_domainID(self.domain)
        self.assertEqual(len(self.domain.calls), 2)
        self.assertNotIn("guest-a", self.controller.manual_increase_until)

    def test_invalid_grace_is_rejected(self):
        for value in (-1, 1.5, True, "60"):
            self.controller.config["vms"][0]["manual_increase_grace_seconds"] = value
            with self.subTest(value=value), self.assertRaises(autoballoon.ExitFailure):
                self.controller._LibVirtAutoBalloon__validate_config_parameters()


if __name__ == "__main__":
    unittest.main()