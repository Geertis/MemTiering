import copy
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('control_timeout',
    Path(__file__).resolve().parents[1] / 'runtime/host/control_timeout.py')
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


class ControlTimeoutTest(unittest.TestCase):
    def setUp(self):
        self.p = dict(label='P-r1', strategy='P', result='completed', medium='ssd',
                      dram_gib=7, n=8, repetition=1, seed=42,
                      slots=[dict(lane=0, case='trace', offset_s=0, deadline_s=None)],
                      release_epoch=100, all_done_epoch=130)
        self.f = dict(self.p, label='F-r1', strategy='F')
        self.audit = dict(valid=True, l1_oom=False)

    def test_only_unfinished_control_is_stopped_at_twice_batch_time(self):
        rule = policy.paired_policy(self.f, self.p, self.audit)
        self.assertEqual(rule['limit_s'], 60)
        self.assertFalse(policy.expired(rule, 59.99))
        self.assertTrue(policy.expired(rule, 60))
        self.assertFalse(policy.expired(rule, 61, all_done=True))
        self.assertIsNone(policy.paired_policy(self.p))
        self.assertFalse(policy.expired(None, 100000))

    def test_failed_or_unmatched_reference_is_never_used(self):
        for key, bad in [('medium', 'cxl'), ('dram_gib', 6), ('n', 4),
                         ('repetition', 2), ('seed', 43), ('slots', [])]:
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'unmatched'):
                policy.paired_policy(dict(self.f, **{key: bad}), self.p, self.audit)
        for audit in [dict(valid=False), dict(valid=True, l1_oom=True)]:
            with self.assertRaises(ValueError):
                policy.paired_policy(self.f, self.p, audit)
        with self.assertRaises(ValueError):
            policy.paired_policy(self.f)
        for duration in [0, -1, float('inf'), float('nan')]:
            bad = copy.deepcopy(self.p)
            bad['all_done_epoch'] = 100 + duration
            with self.assertRaises(ValueError):
                policy.paired_policy(self.f, bad, self.audit)


if __name__ == '__main__':
    unittest.main()
