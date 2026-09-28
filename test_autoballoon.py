import importlib.util
import sys
import types
import unittest
from unittest.mock import patch


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