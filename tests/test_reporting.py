import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from cxl_controller import Controller, ReportingGate, State


class ReportingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.order = self.root / 'order'
        self.order.write_text('5')
        self.gate = ReportingGate(self.order, 5, 0, 500)

    def test_brief_wait_does_not_lower_floor(self):
        self.gate.update(True, 1_000_000_000)
        self.gate.update(True, 1_499_999_999)
        self.assertEqual(self.order.read_text(), '5')
        self.gate.update(False, 1_500_000_000)
        self.gate.update(True, 1_600_000_000)
        self.assertEqual(self.order.read_text(), '5')

    def test_wait_floor_is_restored_on_any_active_sandbox(self):
        self.gate.update(True, 1_000_000_000)
        self.assertTrue(self.gate.update(True, 1_500_000_000))
        self.assertEqual(self.order.read_text(), '0')
        self.assertTrue(self.gate.update(False, 1_500_000_001))
        self.assertEqual(self.order.read_text(), '5')

    def test_start_restores_before_measure_and_ack(self):
        self.gate.change(0)
        s = State('a', self.root, self.root)
        c = Controller([s], 10000, 0, 1, 100, io.StringIO(), reporting=self.gate)
        def measure():
            self.assertEqual(self.order.read_text(), '5')
        with patch.object(c, 'measure', side_effect=measure), patch.object(c, 'rebalance'):
            reply = c.event({'sandbox':'a', 'event':'start'})
        self.assertEqual(reply['reporting_order'], 5)
        self.assertTrue(s.pending_active)

    def test_one_waiting_sandbox_cannot_trigger_global_low_floor(self):
        a, b = State('a', self.root, self.root), State('b', self.root, self.root, phase=1)
        for filename in ['quota_pages', 'phase']:
            (self.root / filename).write_text('0')
        c = Controller([a, b], 10000, 0, 1, 100, io.StringIO(), reporting=self.gate)
        with patch.object(c, 'measure'):
            c.rebalance()
            c.rebalance()
        self.assertEqual(self.order.read_text(), '5')

    def test_invalid_orders_are_rejected(self):
        for active, wait in [(0,5), (5,-1), (10,0)]:
            with self.assertRaises(ValueError):
                ReportingGate(self.order, active, wait, 500)

    def quiet_gate(self):
        path = self.root/'page_reporting_paused'
        path.write_text('N')
        return ReportingGate(self.order, 5, 0, 500, pause_path=path), path

    def test_quiet_wait_enables_reporting_and_any_active_pauses(self):
        gate, path = self.quiet_gate()
        self.assertEqual(path.read_text(), '1')
        gate.update(True, 1_000_000_000)
        self.assertEqual(path.read_text(), '1')
        gate.update(True, 1_500_000_000)
        self.assertEqual((self.order.read_text(),path.read_text()), ('0','0'))
        gate.update(False, 1_500_000_001)
        self.assertEqual((self.order.read_text(),path.read_text()), ('5','1'))

    def test_quiet_start_waits_for_pause_before_measure_and_ack(self):
        gate, path = self.quiet_gate()
        gate.update(True, 1_000_000_000)
        gate.update(True, 1_500_000_000)
        c = Controller([State('a',self.root,self.root)],10000,0,1,100,
                       io.StringIO(),reporting=gate)
        def measure():
            self.assertEqual((self.order.read_text(),path.read_text()), ('5','1'))
        with patch.object(c,'measure',side_effect=measure), patch.object(c,'rebalance'):
            reply = c.event({'sandbox':'a','event':'start'})
        self.assertTrue(reply['reporting_paused'])

    def test_rejected_pause_cannot_ack_or_activate_tool(self):
        gate, _ = self.quiet_gate()
        s = State('a',self.root,self.root)
        c = Controller([s],10000,0,1,100,io.StringIO(),reporting=gate)
        with patch.object(gate,'pause',side_effect=RuntimeError('write rejected')):
            with self.assertRaises(RuntimeError):
                c.event({'sandbox':'a','event':'start'})
        self.assertFalse(s.pending_active)

    def test_close_restores_default_reporting(self):
        gate, path = self.quiet_gate()
        gate.close()
        self.assertEqual((self.order.read_text(),path.read_text()), ('5','0'))

    def drain_gate(self):
        path = self.root/'page_reporting_paused'
        path.write_text('N')
        return ReportingGate(self.order,5,0,500,pause_path=path,drain_only=True),path

    def test_drain_start_keeps_coarse_reporting_enabled_before_ack(self):
        gate,path=self.drain_gate()
        gate.update(True,1_000_000_000)
        gate.update(True,1_500_000_000)
        c=Controller([State('a',self.root,self.root)],10000,0,1,100,
                     io.StringIO(),reporting=gate)
        def measure():
            self.assertEqual((self.order.read_text(),path.read_text()), ('5','0'))
        with patch.object(c,'measure',side_effect=measure),patch.object(c,'rebalance'):
            reply=c.event({'sandbox':'a','event':'start'})
        self.assertFalse(reply['reporting_paused'])

    def test_active_ticks_do_not_repeatedly_cancel_coarse_work(self):
        gate,_=self.drain_gate()
        with patch.object(gate,'pause',wraps=gate.pause) as pause:
            self.assertFalse(gate.update(False,1_000_000_000))
            self.assertFalse(gate.update(False,1_050_000_000))
        pause.assert_not_called()

    def test_each_wait_resume_drains_once_and_returns_to_coarse(self):
        gate,path=self.drain_gate()
        for start in [1_000_000_000,3_000_000_000]:
            gate.update(True,start)
            gate.update(True,start+500_000_000)
            self.assertEqual((self.order.read_text(),path.read_text()), ('0','0'))
            self.assertTrue(gate.restore())
            self.assertEqual((self.order.read_text(),path.read_text()), ('5','0'))
            self.assertFalse(gate.restore())

    def barrier_gate(self):
        path=self.root/'barrier'
        path.write_text('0')
        return ReportingGate(self.order,5,0,500,barrier_path=path),path

    def test_barrier_raises_floor_before_waiting_and_ack(self):
        gate,path=self.barrier_gate()
        gate.update(True,1_000_000_000);gate.update(True,1_500_000_000)
        original=Path.write_text
        def write(p,value,*args,**kw):
            if p==path:self.assertEqual(self.order.read_text(),'5')
            return original(p,value,*args,**kw)
        c=Controller([State('a',self.root,self.root)],10000,0,1,100,io.StringIO(),reporting=gate)
        with patch.object(Path,'write_text',new=write),patch.object(c,'measure'),patch.object(c,'rebalance'):
            reply=c.event({'sandbox':'a','event':'start'})
        self.assertEqual(reply['reporting_order'],5)
        self.assertFalse(reply['reporting_paused'])

    def test_barrier_active_ticks_preserve_queue(self):
        gate,path=self.barrier_gate();path.write_text('0')
        self.assertFalse(gate.update(False,1_000_000_000))
        self.assertFalse(gate.update(False,1_050_000_000))
        self.assertEqual(path.read_text(),'0')

    def test_rejected_barrier_blocks_tool_ack(self):
        gate,path=self.barrier_gate()
        gate.update(True,1_000_000_000);gate.update(True,1_500_000_000)
        original=Path.write_text
        def write(p,value,*args,**kw):
            if p==path:raise PermissionError('barrier rejected')
            return original(p,value,*args,**kw)
        s=State('a',self.root,self.root)
        c=Controller([s],10000,0,1,100,io.StringIO(),reporting=gate)
        with patch.object(Path,'write_text',new=write):
            with self.assertRaises(PermissionError):c.event({'sandbox':'a','event':'start'})
        self.assertFalse(s.pending_active)


if __name__ == '__main__':
    unittest.main()
