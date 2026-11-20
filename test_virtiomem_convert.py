import os
import stat
import tempfile
import unittest
import xml.etree.ElementTree as ElementTree
from unittest.mock import call, patch

import virtiomem_convert


class VirtioMemConvertTests(unittest.TestCase):
    def test_missing_memory_text_raises_value_error(self):
        for value in (None, '', '   '):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, 'Memory value'):
                    virtiomem_convert.to_kib(value, 'KiB')

    def test_empty_memory_element_reports_value_error(self):
        xml = '<domain><memory/><currentMemory>1024</currentMemory></domain>'
        with self.assertRaisesRegex(ValueError, 'Memory value is missing'):
            virtiomem_convert.transform_xml_string(xml, 'to-virtiomem', 2048)

    def test_to_virtiomem_disables_balloon_by_default(self):
        xml = """<domain>
  <memory unit='KiB'>4194304</memory>
  <currentMemory unit='KiB'>3145728</currentMemory>
  <vcpu>2</vcpu>
  <devices>
    <memballoon model='virtio' autodeflate='on'>
      <stats period='2'/>
    </memballoon>
  </devices>
</domain>"""

        result = virtiomem_convert.transform_xml_string(xml, 'to-virtiomem', 1048576)
        balloon = ElementTree.fromstring(result).find('devices/memballoon')

        self.assertEqual(balloon.get('model'), 'none')
        self.assertNotIn('autodeflate', balloon.attrib)
        self.assertIsNone(balloon.find('stats'))

    def test_keep_balloon_flag_preserves_existing_model(self):
        xml = """<domain>
  <memory unit='KiB'>4194304</memory>
  <currentMemory unit='KiB'>3145728</currentMemory>
  <vcpu>2</vcpu>
  <devices><memballoon model='virtio'/></devices>
</domain>"""

        result = virtiomem_convert.transform_xml_string(
            xml, 'to-virtiomem', 1048576, keep_balloon=True
        )
        balloon = ElementTree.fromstring(result).find('devices/memballoon')

        self.assertEqual(balloon.get('model'), 'virtio')
        self.assertEqual(balloon.find('stats').get('period'), '5')

    def test_idempotent_to_virtiomem_disables_existing_balloon(self):
        xml = """<domain>
  <memory unit='KiB'>4194304</memory>
  <currentMemory unit='KiB'>1048576</currentMemory>
  <devices>
    <memory model='virtio-mem'>
      <target><size unit='KiB'>2048</size><requested unit='KiB'>0</requested></target>
    </memory>
    <memballoon model='virtio'/>
  </devices>
</domain>"""

        result = virtiomem_convert.transform_xml_string(xml, 'to-virtiomem', 1048576)
        root = ElementTree.fromstring(result)

        self.assertEqual(len(root.findall("devices/memory[@model='virtio-mem']")), 1)
        self.assertEqual(root.find('devices/memballoon').get('model'), 'none')

    def test_to_virtiomem_creates_requested_balloon_model_when_missing(self):
        xml = (
            "<domain><memory unit='KiB'>4194304</memory>"
            "<currentMemory unit='KiB'>3145728</currentMemory>"
            "<vcpu>2</vcpu><devices/></domain>"
        )
        for keep_balloon, expected_model in ((False, 'none'), (True, 'virtio')):
            with self.subTest(keep_balloon=keep_balloon):
                result = virtiomem_convert.transform_xml_string(
                    xml, 'to-virtiomem', 1048576, keep_balloon=keep_balloon
                )
                balloon = ElementTree.fromstring(result).find('devices/memballoon')
                self.assertEqual(balloon.get('model'), expected_model)

    def test_to_balloon_uses_numa_base_without_double_counting_virtio_mem(self):
        xml = """<domain>
  <maxMemory slots='16' unit='KiB'>8388608</maxMemory>
  <memory unit='KiB'>8388608</memory>
  <currentMemory unit='KiB'>1048576</currentMemory>
  <cpu><numa><cell id='0' cpus='0-3' memory='1048576' unit='KiB'/></numa></cpu>
  <devices>
    <memory model='virtio-mem'>
      <target>
        <size unit='KiB'>7340032</size>
        <node>0</node>
        <requested unit='KiB'>1572864</requested>
      </target>
    </memory>
  </devices>
</domain>"""
        result = virtiomem_convert.transform_xml_string(xml, 'to-balloon', 1048576)
        root = ElementTree.fromstring(result)

        self.assertEqual(root.findtext('memory'), '8388608')
        self.assertEqual(root.findtext('currentMemory'), '2621440')

        result = virtiomem_convert.transform_xml_string(
            xml, 'to-balloon', 1048576, initial_kib=3145729
        )
        root = ElementTree.fromstring(result)
        self.assertEqual(root.findtext('memory'), '8388608')
        self.assertEqual(root.findtext('currentMemory'), '3145729')
        self.assertEqual(root.find('currentMemory').get('unit'), 'KiB')

        multi_node_xml = xml.replace(
            "<cell id='0' cpus='0-3' memory='1048576' unit='KiB'/>",
            "<cell id='0' cpus='0-1' memory='512' unit='MiB'/>"
            "<cell id='1' cpus='2-3' memory='524288' unit='KiB'/>",
        )
        result = virtiomem_convert.transform_xml_string(multi_node_xml, 'to-balloon', 1048576)
        root = ElementTree.fromstring(result)
        self.assertEqual(root.findtext('memory'), '8388608')
        self.assertEqual(root.findtext('currentMemory'), '2621440')
        self.assertEqual(
            [cell.get('memory') for cell in root.findall('cpu/numa/cell')],
            ['7864320', '524288'],
        )

    def test_to_balloon_initial_memory_creates_current_memory_and_checks_limits(self):
        xml = "<domain><memory unit='GiB'>8</memory></domain>"
        result = virtiomem_convert.transform_xml_string(
            xml, 'to-balloon', 1048576, initial_kib=2621440
        )
        root = ElementTree.fromstring(result)
        self.assertEqual(root.findtext('memory'), '8')
        self.assertEqual(root.findtext('currentMemory'), '2621440')
        for value in (0, 8388609):
            with self.subTest(initial_kib=value):
                with self.assertRaisesRegex(ValueError, 'Initial memory must be positive'):
                    virtiomem_convert.transform_xml_string(
                        xml, 'to-balloon', 1048576, initial_kib=value
                    )

    def test_to_balloon_reenables_disabled_balloon(self):
        for with_virtiomem in (False, True):
            with self.subTest(with_virtiomem=with_virtiomem):
                memory = (
                    "<memory unit='KiB'>4194304</memory>"
                    "<currentMemory unit='KiB'>3145728</currentMemory>"
                )
                virtio_mem = (
                    "<memory model='virtio-mem'><target>"
                    "<size unit='KiB'>2048</size><requested unit='KiB'>1024</requested>"
                    "</target></memory>"
                    if with_virtiomem else ''
                )
                xml = (
                    f"<domain>{memory}<devices>{virtio_mem}"
                    "<memballoon model='none'/></devices></domain>"
                )

                result = virtiomem_convert.transform_xml_string(xml, 'to-balloon', 1048576)
                balloon = ElementTree.fromstring(result).find('devices/memballoon')

                self.assertEqual(balloon.get('model'), 'virtio')
                self.assertEqual(balloon.get('autodeflate'), 'on')
                self.assertEqual(balloon.find('stats').get('period'), '5')

    def test_keep_balloon_cli_flag_is_applied(self):
        xml = (
            "<domain><memory unit='KiB'>4194304</memory>"
            "<currentMemory unit='KiB'>3145728</currentMemory>"
            "<vcpu>2</vcpu><devices><memballoon model='virtio'/></devices></domain>"
        )
        with tempfile.TemporaryDirectory() as directory:
            input_path = os.path.join(directory, 'input.xml')
            output_path = os.path.join(directory, 'output.xml')
            with open(input_path, 'w', encoding='utf-8') as xml_file:
                xml_file.write(xml)

            with patch(
                'sys.argv',
                ['virtiomem_convert.py', 'to-virtiomem', input_path, output_path, '-k'],
            ):
                virtiomem_convert.main()

            with open(output_path, 'r', encoding='utf-8') as xml_file:
                root = ElementTree.parse(xml_file).getroot()
            self.assertEqual(root.find('devices/memballoon').get('model'), 'virtio')

    def test_conversion_options_are_passed_to_vm_mode(self):
        with patch(
            'sys.argv',
            [
                'virtiomem_convert.py', 'to-virtiomem', '-V', 'guest-a',
                '--keep-balloon', '-i', '2GiB', '-m', '6GiB', '-n', '-L', 'always',
            ],
        ), patch.object(virtiomem_convert, 'process_vm') as process_vm:
            virtiomem_convert.main()

        process_vm.assert_called_once_with(
            'guest-a',
            'to-virtiomem',
            1048576,
            connect_uri=None,
            dry_run=True,
            color_mode='always',
            keep_balloon=True,
            initial_kib=2097152,
            max_override_kib=6291456,
        )

    def test_max_memory_overrides_capacity_in_both_directions(self):
        xml = (
            "<domain><memory unit='GiB'>4</memory>"
            "<currentMemory unit='GiB'>3</currentMemory></domain>"
        )
        base_kib = 1048576
        virtio_xml = virtiomem_convert.transform_xml_string(
            xml, 'to-virtiomem', base_kib, max_override_kib=6291457
        )
        root = ElementTree.fromstring(virtio_xml)
        self.assertEqual(root.findtext('maxMemory'), '6291456')
        self.assertEqual(root.findtext('devices/memory/target/size'), '5242880')
        self.assertEqual(root.findtext('devices/memory/target/requested'), '2097152')

        with self.assertRaisesRegex(ValueError, 'exceeds --max-memory'):
            virtiomem_convert.transform_xml_string(
                virtio_xml, 'to-balloon', base_kib, max_override_kib=2097153
            )
        balloon_xml = virtiomem_convert.transform_xml_string(
            virtio_xml, 'to-balloon', base_kib, initial_kib=1572864, max_override_kib=2097153
        )
        root = ElementTree.fromstring(balloon_xml)
        self.assertEqual(root.findtext('memory'), '2097153')
        self.assertEqual(root.findtext('currentMemory'), '1572864')

    def test_max_memory_updates_already_ballooned_domain(self):
        xml = "<domain><memory unit='GiB'>4</memory><currentMemory unit='GiB'>2</currentMemory></domain>"
        result = virtiomem_convert.transform_xml_string(
            xml, 'to-balloon', 1048576, max_override_kib=3145728
        )
        root = ElementTree.fromstring(result)
        self.assertEqual(root.findtext('memory'), '3145728')
        self.assertEqual(root.findtext('currentMemory'), '2')
        self.assertEqual(root.find('currentMemory').get('unit'), 'GiB')

    def test_initial_memory_overrides_current_memory_and_is_aligned(self):
        xml = (
            "<domain><memory unit='GiB'>4</memory>"
            "<currentMemory unit='GiB'>3</currentMemory></domain>"
        )
        for initial_kib, expected_requested in ((None, 2097152), (2097153, 1048576)):
            with self.subTest(initial_kib=initial_kib):
                result = virtiomem_convert.transform_xml_string(
                    xml, 'to-virtiomem', 1048576, initial_kib=initial_kib
                )
                root = ElementTree.fromstring(result)
                self.assertEqual(root.findtext('currentMemory'), '1048576')
                requested = root.find('devices/memory/target/requested')
                self.assertEqual(requested.get('unit'), 'KiB')
                self.assertEqual(int(requested.text), expected_requested)

    def test_initial_memory_outside_domain_limits_is_rejected(self):
        xml = "<domain><memory unit='GiB'>4</memory></domain>"
        for initial_kib in (1048575, 4194305):
            with self.subTest(initial_kib=initial_kib):
                with self.assertRaisesRegex(ValueError, 'Initial memory must be between'):
                    virtiomem_convert.transform_xml_string(
                        xml, 'to-virtiomem', 1048576, initial_kib=initial_kib
                    )

    def test_removing_first_child_keeps_sibling_indentation(self):
        root = ElementTree.Element('domain')
        root.text = '\n  '
        max_memory = ElementTree.SubElement(root, 'maxMemory', {'unit': 'KiB'})
        max_memory.text = '4096'
        max_memory.tail = '\n  '
        comment = ElementTree.Comment('adjacent')
        comment.tail = '\n  '
        root.append(comment)
        memory = ElementTree.SubElement(root, 'memory', {'unit': 'KiB'})
        memory.text = '2048'
        memory.tail = '\n'

        virtiomem_convert.remove_child_formatted(root, max_memory)

        self.assertEqual(
            ElementTree.tostring(root, encoding='unicode'),
            '<domain>\n  <!--adjacent-->\n  <memory unit="KiB">2048</memory>\n</domain>',
        )

    def test_large_diff_blocks_skip_pairwise_alignment(self):
        minus_lines = ['-old line\n'] * 51
        plus_lines = ['+new line\n'] * 50

        with patch('virtiomem_convert.difflib.SequenceMatcher', side_effect=AssertionError):
            colored_minus, colored_plus = virtiomem_convert.align_hunk_lines(minus_lines, plus_lines)

        self.assertEqual(len(colored_minus), len(minus_lines))
        self.assertEqual(len(colored_plus), len(plus_lines))
        self.assertTrue(colored_minus[0].startswith(virtiomem_convert.ANSI_RED))
        self.assertTrue(colored_plus[0].startswith(virtiomem_convert.ANSI_GREEN))

    def test_stdlib_fallback_preserves_qemu_namespace_prefix(self):
        xml = """<domain xmlns:qemu='http://libvirt.org'>
  <memory unit='KiB'>4096</memory>
  <currentMemory unit='KiB'>3072</currentMemory>
  <maxMemory slots='16' unit='KiB'>4096</maxMemory>
  <devices>
    <memory model='virtio-mem'>
      <target>
        <size unit='KiB'>2048</size>
        <requested unit='KiB'>1024</requested>
      </target>
    </memory>
  </devices>
  <qemu:commandline><qemu:arg value='-foo'/></qemu:commandline>
</domain>"""

        with patch.object(virtiomem_convert, 'HAS_LXML', False), patch.object(
            virtiomem_convert, 'ET', ElementTree
        ):
            result = virtiomem_convert.transform_xml_string(xml, 'to-balloon', 1024)

        self.assertIn('<qemu:commandline>', result)
        self.assertIn('<qemu:arg ', result)
        self.assertNotIn('<ns0:commandline>', result)

    def test_numa_cpu_set_excludes_disabled_vcpus(self):
        xml = """<domain>
  <memory unit='KiB'>4194304</memory>
  <currentMemory unit='KiB'>3145728</currentMemory>
  <vcpu current='2'>
    4
  </vcpu>
  <vcpus>
    <vcpu id='0' enabled='yes'/>
    <vcpu id='1' enabled='no'/>
    <vcpu id='2' enabled='yes'/>
    <vcpu id='3' enabled='no'/>
  </vcpus>
  <cpu><topology sockets='1' dies='1' cores='2' threads='2'/></cpu>
  <devices/>
</domain>"""

        with patch.object(virtiomem_convert, 'HAS_LXML', False), patch.object(
            virtiomem_convert, 'ET', ElementTree
        ):
            result = virtiomem_convert.transform_xml_string(xml, 'to-virtiomem', 1048576)

        root = ElementTree.fromstring(result)
        self.assertEqual(root.find('cpu/numa/cell').get('cpus'), '0,2')

    def test_input_and_output_can_be_the_same_file(self):
        xml = (
            "<domain><memory unit='KiB'>4194304</memory>"
            "<currentMemory unit='KiB'>3145728</currentMemory>"
            "<vcpu>2</vcpu><devices/></domain>"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'domain.xml')
            with open(path, 'w', encoding='utf-8') as xml_file:
                xml_file.write(xml)
            os.chmod(path, 0o640)

            with patch(
                'sys.argv',
                ['virtiomem_convert.py', 'to-virtiomem', path, path],
            ):
                virtiomem_convert.main()

            with open(path, 'r', encoding='utf-8') as xml_file:
                result = ElementTree.parse(xml_file).getroot()
            self.assertIsNotNone(result.find('devices/memory[@model="virtio-mem"]'))
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o640)

    def test_failed_atomic_replace_keeps_original_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'domain.xml')
            with open(path, 'w', encoding='utf-8') as xml_file:
                xml_file.write('original')

            with patch.object(virtiomem_convert.os, 'replace', side_effect=OSError('replace failed')):
                with self.assertRaisesRegex(OSError, 'replace failed'):
                    virtiomem_convert.write_xml_atomically(path, 'replacement')

            with open(path, 'r', encoding='utf-8') as xml_file:
                self.assertEqual(xml_file.read(), 'original')
            self.assertEqual(os.listdir(directory), ['domain.xml'])

    def test_process_vm_rejects_nonpersistent_domains(self):
        with patch.object(
            virtiomem_convert,
            'run_virsh',
            side_effect=['shut off', 'Name: guest-a\nPersistent: no\n'],
        ) as run_virsh:
            with self.assertRaisesRegex(RuntimeError, 'not persistent'):
                virtiomem_convert.process_vm('guest-a', 'to-virtiomem', 1048576)

        self.assertEqual(
            run_virsh.call_args_list,
            [call(['domstate', 'guest-a'], None), call(['dominfo', 'guest-a'], None)],
        )

    def test_process_vm_rejects_domains_with_snapshots(self):
        with patch.object(
            virtiomem_convert,
            'run_virsh',
            side_effect=['shut off', 'Persistent: yes\n', 'snapshot-a\n'],
        ) as run_virsh:
            with self.assertRaisesRegex(RuntimeError, 'has snapshots'):
                virtiomem_convert.process_vm('guest-a', 'to-virtiomem', 1048576)

        self.assertEqual(
            run_virsh.call_args_list,
            [
                call(['domstate', 'guest-a'], None),
                call(['dominfo', 'guest-a'], None),
                call(['snapshot-list', '--name', 'guest-a'], None),
            ],
        )

    def test_process_vm_proceeds_for_persistent_domain_without_snapshots(self):
        with patch.object(
            virtiomem_convert,
            'run_virsh',
            side_effect=['shut off', 'Persistent: yes\n', '', '<domain/>', ''],
        ) as run_virsh, patch.object(virtiomem_convert, 'transform_xml_string', return_value='<domain/>'):
            virtiomem_convert.process_vm('guest-a', 'to-virtiomem', 1048576)

        self.assertEqual(run_virsh.call_args_list[:4], [
            call(['domstate', 'guest-a'], None),
            call(['dominfo', 'guest-a'], None),
            call(['snapshot-list', '--name', 'guest-a'], None),
            call(['dumpxml', '--inactive', 'guest-a'], None),
        ])
        self.assertEqual(run_virsh.call_args_list[4][0][0][:1], ['define'])


if __name__ == '__main__':
    unittest.main()
