import unittest
from placement import actions, ram_ranges, MIB,HotPriority,select_protection,Placement,NAMES
from controller import PhaseQuota
from history import transition,coalesce_pfns,PRESENT,SWAPPED,PageHistory,reuse_evidence


class PlacementSafety(unittest.TestCase):
    def test_readback_stops_only_anonymous_pageout_and_preserves_quota(self):
        quota=PhaseQuota(512,16)
        self.assertEqual(actions('cold',True,0,128*MIB,4096,0),('pageout','stat','stat'))
        self.assertEqual((quota.target(True),quota.target(False)),(512,16))

    def test_anonymous_pageout_needs_a_full_readback_cooldown(self):
        self.assertEqual(actions('cold',True,0,128*MIB,0,.9)[1],'stat')
        self.assertEqual(actions('cold',True,0,128*MIB,0,1.1)[1],'pageout')

    def test_inactive_target_does_not_trigger_unnecessary_sandbox_eviction(self):
        self.assertEqual(actions('cold',False,10,0,0,10),('stat','stat','stat'))

    def test_vm_cache_scope_is_explicit_and_never_enabled_in_active(self):
        for mode in ('cold','cold-cache'):
            self.assertEqual(actions(mode,True,10,128*MIB,0,10)[2],'stat')
        self.assertEqual(actions('cold',False,10,0,0,10)[2],'stat')
        self.assertEqual(actions('cold-cache',False,10,0,0,10)[2],'pageout')

    def test_wait_has_a_grace_period(self):
        self.assertEqual(actions('cold',False,.2,128*MIB,0,10),('stat','stat','stat'))

    def test_physical_holes_and_unaligned_kernel_reserved_ranges_are_excluded(self):
        text='00000000-0009fbff : System RAM\n  00001000-00002000 : Kernel code\n00100000-7fffffff : System RAM\n80000000-8fffffff : PCI Bus\n'
        self.assertEqual(ram_ranges(text),[(0,0x9f000),(0x100000,0x80000000)])
        with self.assertRaises(RuntimeError):ram_ranges('00000000-00000000 : System RAM')

    def test_swapcache_refault_observation_is_not_invented_disk_io(self):
        self.assertEqual(transition(SWAPPED|12,PRESENT|24),'refault')
        self.assertEqual(transition(PRESENT|24,SWAPPED|12),'evicted')
        self.assertIsNone(transition(PRESENT|24,PRESENT|25))
        self.assertIsNone(transition(None,PRESENT|25))
        self.assertIsNone(transition(0,PRESENT|25))

    def test_protection_is_physical_page_aligned_bounded_and_skips_hidden_pfns(self):
        self.assertEqual(coalesce_pfns([0,17,18,33,100]),[(65536,196608),(393216,458752)])
        self.assertEqual(len(coalesce_pfns(range(16,16000,32),limit=8)),8)

    def test_process_identity_includes_an_exec_layout_and_start_time(self):
        import os
        value=PageHistory.identity(os.getpid())
        self.assertEqual(len(value),4)
        self.assertGreater(value[0],0)
        self.assertTrue(value[3].startswith('/'))

    def test_live_observer_uses_packed_bounded_state_and_purges_exited_members(self):
        import os,tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            group=Path(tmp);(group/'cgroup.procs').write_text(str(os.getpid()))
            h=PageHistory(group);result=h.sample(1.)
            self.assertGreater(result['sampled_pages'],0)
            self.assertLessEqual(result['sampled_pages'],8192)
            self.assertLessEqual(result['packed_history_bytes'],(8192+32)*8)
            self.assertNotIn('pages',h.processes[os.getpid()])
            (group/'cgroup.procs').write_text('');result=h.sample(2.)
            self.assertEqual(result['processes'],0)
            self.assertEqual(result['packed_history_bytes'],0)

    def test_hot_priority_obeys_the_real_damon_kernel_attribute_constraint(self):
        from unittest.mock import patch
        from pathlib import Path
        writes={}
        def capture(path,value):writes[str(path)]=value
        with patch('placement.Placement.write',side_effect=capture):
            HotPriority(Path('/damon/kdamonds/1'),Path('/sys/fs/cgroup/agent-workload'))
        self.assertGreaterEqual(writes['/damon/kdamonds/1/contexts/0/monitoring_attrs/nr_regions/min'],3)
        self.assertEqual(writes['/damon/kdamonds/1/contexts/0/schemes/0/action'],'lru_prio')

    def test_first_refault_does_not_invent_future_reuse(self):
        state,hint=reuse_evidence(None,1.)
        self.assertEqual(state,(1.,1));self.assertIsNone(hint)
        state,hint=reuse_evidence(state,3.)
        self.assertEqual(state,(3.,2));self.assertIsNotNone(hint)
        self.assertIsNone(reuse_evidence(state,40.)[1])
        self.assertIsNone(reuse_evidence((1.,1),20.)[1])

    def test_protection_never_crowds_active_tools_or_exceeds_wait_quota_fraction(self):
        history=dict(priority_blocks=[(100-i,i*65536,(i+1)*65536) for i in range(64)])
        self.assertEqual(select_protection(history,112,True),[])
        selected=select_protection(history,16,False)
        self.assertLessEqual(sum(hi-lo for lo,hi in selected),2*MIB)
        self.assertEqual(select_protection(history,0,False),[])

    def test_declared_execution_tolerance_does_not_trigger_extra_eviction(self):
        self.assertEqual(actions('cold',True,0,4*MIB,0,10),('stat','stat','stat'))
        self.assertEqual(actions('cold',False,10,8*MIB,0,10),('stat','stat','stat'))

    def test_runtime_start_stops_wait_monitor_before_ack_without_changing_quota(self):
        import threading
        from pathlib import Path
        from unittest.mock import Mock
        p=Placement.__new__(Placement);p.lock=threading.RLock();p.seen_start=False
        p.hot=Mock();p.main_running=True;p.k=Path('/damon/0');p.write=Mock()
        p.completed_stats={name:{} for name in NAMES}
        p.raw_stats=Mock(return_value={name:{'sz_applied':4096} for name in NAMES})
        p.phase(True)
        self.assertTrue(p.active);self.assertFalse(p.main_running)
        p.hot.update.assert_called_once_with([])
        p.write.assert_called_once_with(Path('/damon/0/state'),'off')
        self.assertEqual(p.completed_stats['sandbox_anon']['sz_applied'],4096)

    def test_transient_kernel_sysfs_mutex_busy_is_retried_without_hiding_invalid_arguments(self):
        from unittest.mock import Mock,patch
        import errno
        path=Mock();path.write_text.side_effect=[OSError(errno.EBUSY,'busy'),None]
        with patch('placement.time.sleep'):
            Placement.write(path,'on')
        self.assertEqual(path.write_text.call_count,2)
        path=Mock();path.write_text.side_effect=OSError(errno.EINVAL,'invalid')
        with self.assertRaises(OSError):Placement.write(path,'on')


if __name__=='__main__':unittest.main()
