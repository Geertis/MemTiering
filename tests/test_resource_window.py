import importlib.util
from pathlib import Path
import unittest

path=Path(__file__).resolve().parents[1]/'experiments/concurrent/diagnostics/resource_window.py'
spec=importlib.util.spec_from_file_location('resource_window',path)
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ResourceWindowTest(unittest.TestCase):
    def test_partial_completion_cannot_truncate_late_failure(self):
        state=dict(result='vmm-loss',release_monotonic_ns=100,
                   final_resources=dict(monotonic_ns=10000))
        lo,hi=module.resource_window(state)
        samples=[(150,8),(1000,8),(9999,7),(10001,0)]
        self.assertEqual(min(live for t,live in samples if lo<=t<=hi),7)

    def test_completed_batch_excludes_cleanup_and_prefers_monotonic(self):
        state=dict(result='completed',release_monotonic_ns=100,
            release_epoch=1,all_done_epoch=3,all_done_monotonic_ns=2100,
            final_resources=dict(monotonic_ns=3000))
        self.assertEqual(module.resource_window(state),(100,2100))
        state.update(result='control-relative-timeout',control_timeout=dict(stop_monotonic_ns=2000))
        self.assertEqual(module.resource_window(state),(100,2000))


if __name__=='__main__':
    unittest.main()
