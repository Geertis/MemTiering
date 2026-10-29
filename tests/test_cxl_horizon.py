import importlib.util, io, sys, tempfile, unittest
from pathlib import Path
spec=importlib.util.spec_from_file_location('lookahead_controller',Path(__file__).resolve().parents[1]/'runtime/cxl/cxl_controller.py')
c=importlib.util.module_from_spec(spec);sys.modules[spec.name]=c;spec.loader.exec_module(c)
class HorizonTest(unittest.TestCase):
    def test_conditional_remaining_duration_and_unknown_tail(self):
        s=c.State('x',Path('/unused'),Path('/unused'));s.phase=1;s.kind=3;s.start_ns=1_000_000_000
        s.duration[3].extend([1000,2000,4000])
        self.assertEqual(s.horizon_ms(2_500_000_000),500)
        self.assertEqual(s.horizon_ms(6_000_000_000),1000)
        self.assertEqual(s.horizon_ms(1_000_000_000),1000)
    def test_refresh_preserves_phase_and_wait_cannot_refresh(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t);(p/'horizon_ms').touch();(p/'phase').write_text('marker')
            s=c.State('x',Path('/unused'),p);s.phase=1;s.start_ns=1
            ctl=c.Controller([s],1000,0,1,10,io.StringIO(),refresh_horizons=True);ctl.measure=lambda:None
            ctl.rebalance();self.assertEqual((p/'phase').read_text(),'marker')
            self.assertEqual((p/'horizon_ms').read_text(),'1000')
            (p/'horizon_ms').write_text('marker');s.phase=0;s.next_horizon_ns=0
            ctl.rebalance();self.assertEqual((p/'horizon_ms').read_text(),'marker')
if __name__=='__main__': unittest.main()
