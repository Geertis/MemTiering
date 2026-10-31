import importlib.util
from pathlib import Path
import unittest

path=Path(__file__).resolve().parents[1]/'experiments/concurrent/diagnostics/kernel_sigsys_probe.py'
spec=importlib.util.spec_from_file_location('sigsys_probe',path)
probe=importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class SigsysProbeTest(unittest.TestCase):
    def test_syscall_and_thread_identity_are_preserved(self):
        row=probe.parse('  vmm-9999999 [003] d... 123.456: trap: (force_sig_seccomp+0x0) syscall=16 reason=0 fatal=0')
        self.assertEqual(row['syscall'],16)
        self.assertEqual(row['tid'],9999999)
        self.assertEqual(row['comm'],'vmm')
        self.assertEqual(row['fatal'],0)

    def test_unrelated_trace_text_cannot_invent_a_syscall(self):
        self.assertNotIn('syscall',probe.parse('# tracer: nop'))


if __name__=='__main__':
    unittest.main()
