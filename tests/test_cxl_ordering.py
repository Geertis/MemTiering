"""Exercise observable sysfs ordering and learned headroom through the controller."""
import importlib.util,io,sys,unittest
from pathlib import Path
spec=importlib.util.spec_from_file_location('benefit_controller',Path(__file__).resolve().parents[1]/'runtime/cxl/cxl_controller.py');c=importlib.util.module_from_spec(spec);sys.modules[spec.name]=c;spec.loader.exec_module(c)
class File:
    def __init__(self,path,writes):self.path,self.writes=path,writes
    def __truediv__(self,n):return File(self.path+'/'+n,self.writes)
    def write_text(self,s):self.writes.append((self.path,s))
class ControllerTest(unittest.TestCase):
    def test_active_quota_precedes_wakeup_wait_precedes_revoke(self):
        writes=[];s=c.State('x',Path('/unused'),File('slot',writes));ctl=c.Controller([s],1000,0,1,10,io.StringIO());ctl.measure=lambda:None
        ctl.set_phase(s,1,3,1_000_000_000);ctl.rebalance()
        self.assertLess(next(i for i,x in enumerate(writes) if x[0].endswith('quota_pages')),next(i for i,x in enumerate(writes) if x[0].endswith('phase')))
        writes.clear();ctl.set_phase(s,0,3,2_000_000_000);ctl.rebalance()
        self.assertTrue(writes[0][0].endswith('phase'));self.assertEqual(s.quota,10)
    def test_cache_growth_cannot_reserve_unbounded_headroom(self):
        s=c.State('x',Path('/unused'),Path('/unused'));s.kind=3;s.anon_growth[3].append(32*c.MIB//c.PAGE);s.file_growth[3].append(1024*c.MIB//c.PAGE)
        self.assertEqual(s.headroom(),48*c.MIB//c.PAGE)
    def test_pending_active_receives_no_unreleased_credit(self):
        writes=[];a=c.State('a',Path('/unused'),File('a',writes));b=c.State('b',Path('/unused'),File('b',writes));a.quota=a.fast=900;b.quota=100
        ctl=c.Controller([a,b],1000,0,1,100,io.StringIO());ctl.measure=lambda:None;ctl.set_phase(b,1,3,1);ctl.rebalance()
        self.assertEqual(b.quota,100);self.assertLessEqual(sum(max(s.fast,s.quota) for s in [a,b]),1000)
if __name__=='__main__':unittest.main()
