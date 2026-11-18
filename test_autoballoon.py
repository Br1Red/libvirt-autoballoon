import importlib.util
import sys
import types
import unittest
from unittest.mock import Mock, mock_open, patch


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
                self.controller.manual_increase_until["guest-new"] = 3600
                self.lifecycle(event)
                self.assertNotIn("guest-new", self.controller.monitored_vms)
                self.assertNotIn("guest-new", self.controller.memory_targets)
                self.assertNotIn("guest-new", self.controller.manual_increase_until)
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
        self.config_open.return_value.read.return_value = (
            '{"default": {"balloon": false}, "vms": ['
            '{"name": "guest-new", "balloon": true, "keep_free_kb": 786432}]}')
        self.lifecycle(self.libvirt.VIR_DOMAIN_EVENT_STARTED)
        self.assertEqual(self.controller.monitored_vms, {"guest-new"})
        self.assertEqual(self.controller.dom_keep_usable(self.domain), 786432)
        self.assertNotIn("guest-existing", self.controller.memory_targets)
        self.assertNotIn("guest-existing", self.controller.manual_increase_until)

        self.config_open.return_value.read.return_value = (
            '{"default": {"balloon": false}, "vms": []}')
        self.lifecycle(self.libvirt.VIR_DOMAIN_EVENT_STARTED)
        self.assertEqual(self.controller.monitored_vms, set())

    def test_failed_reload_keeps_previous_valid_configuration(self):
        previous_config = self.controller.config
        for content in ('{', '{"default": {"threshold": -1}, "vms": []}',
                        '{"default": [], "vms": []}', '{"vms": [null]}',
                        '{"vms": {}}', 'null', '{}'):
            with self.subTest(content=content):
                self.config_open.return_value.read.return_value = content
                with self.assertLogs(level="ERROR") as logs:
                    self.lifecycle(self.libvirt.VIR_DOMAIN_EVENT_STARTED)
                self.assertIn("keeping previous configuration", logs.output[0])
                self.assertIs(self.controller.config, previous_config)
                self.assertIn("guest-new", self.controller.monitored_vms)
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
        self.controller.process_domainID.assert_called_once_with(self.domain)
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


if __name__ == "__main__":
    unittest.main()