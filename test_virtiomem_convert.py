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
