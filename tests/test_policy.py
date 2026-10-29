import sys
from pathlib import Path
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parent))
from adaptive_controller import PhaseQuota
from placement import actions


class WaitCachePolicy(unittest.TestCase):
    def test_fast_cache_remains_wait_only_and_preserves_anon_cooldown(self):
        for fast in (False,True):
            self.assertEqual(actions('cold-cache',True,30,0,0,30,fast),('stat',)*3)
            self.assertEqual(actions('cold',False,30,0,0,30,fast),('stat',)*3)
            self.assertEqual(actions('cold-cache',False,.19,0,0,30,fast),('stat',)*3)
            self.assertEqual(actions('cold-cache',True,0,32<<20,300,0,fast)[:2],('pageout','stat'))

    def test_only_explicit_fast_cache_acts_in_short_wait(self):
        self.assertEqual(actions('cold-cache',False,.3,0,0,30),('stat',)*3)
        self.assertEqual(actions('cold-cache',False,.3,0,0,30,True),('stat','stat','pageout'))

    def test_quota_stays_medium_independent(self):
        p=PhaseQuota(active_mib=128,wait_mib=16)
        self.assertEqual([p.target(a) for a in [True,False,True]],[128,16,128])
        with self.assertRaises(ValueError):PhaseQuota(active_mib=16,wait_mib=128)


if __name__=='__main__':unittest.main()
