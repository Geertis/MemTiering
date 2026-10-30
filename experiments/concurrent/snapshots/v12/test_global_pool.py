import ast
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

import sys
from global_pool import GlobalPool, MIB, physical_guard, physical_reserve, policy

H = Path(__file__).resolve().parent


class GlobalPoolTest(unittest.TestCase):
    def test_fresh_transport_cannot_reuse_a_stale_controller_snapshot(self):
        point=dict(memory=dict(MemTotal=8192*MIB,MemFree=4096*MIB,Cached=0,Shmem=0,Buffers=0,SReclaimable=0))
        sample=dict(guest=dict(guest_monotonic_ns=10_000_000_000,capacity_state=dict(
            active=True,monotonic_ns=1,controller_alive=True,measurement=dict(fast_bytes=0))))
        with tempfile.TemporaryDirectory() as tmp:
            p=GlobalPool(Path(tmp),dict(n=1,medium='ssd',strategy='P',dram_gib=8,require_controller_health=True))
            p.tick(point,[sample]);self.assertIsNone(p.budget);self.assertEqual(p.seq,0)
            sample['guest']['capacity_state']['monotonic_ns']=sample['guest']['guest_monotonic_ns']
            p.next_tick=0;p.tick(point,[sample]);self.assertEqual(p.seq,1)
            sample['guest']['capacity_state']['controller_alive']=False
            p.next_tick=0;p.tick(point,[sample]);self.assertEqual(p.seq,1)
            p.close()

    def test_required_controller_health_must_be_positive_before_admission(self):
        point=dict(memory=dict(MemTotal=8192*MIB,MemFree=4096*MIB,Cached=0,Shmem=0,Buffers=0,SReclaimable=0))
        sample=dict(guest=dict(capacity_state=dict(active=False,measurement=dict(fast_bytes=0))))
        with tempfile.TemporaryDirectory() as tmp:
            p=GlobalPool(Path(tmp),dict(n=1,medium='ssd',strategy='F',dram_gib=8,require_controller_health=True))
            p.tick(point,[sample]);self.assertIsNone(p.budget)
            sample['guest']['capacity_state']['controller_alive']=True
            p.next_tick=0;p.tick(point,[sample]);self.assertIsNotNone(p.budget)
            p.close()
    def test_scaled_reserve_admits_observed_small_host_without_inventing_credit(self):
        point=dict(memory=dict(MemTotal=int(5901.4453125*MIB),MemFree=int(720.03125*MIB),
            Cached=0,Shmem=0,Buffers=0,SReclaimable=0))
        samples=[dict(guest=dict(capacity_state=dict(active=False,
            measurement=dict(fast_bytes=0)))) for _ in range(8)]
        for strategy in ['P','F']:
            spec=dict(n=8,medium='ssd',strategy=strategy,dram_gib=6,reserve_policy='scaled')
            self.assertEqual(physical_reserve(point,spec),368)
            with tempfile.TemporaryDirectory() as tmp:
                p=GlobalPool(Path(tmp),spec)
                p.tick(point,samples)
                self.assertEqual(p.budget,352)
                row=json.loads((Path(tmp)/'global-quotas.json').read_text())
                self.assertEqual(row['reserve_mib'],368)
                self.assertLessEqual(sum(row['grants_mib'].values()),row['safe_payload_budget_mib'])
                point['memory']['MemFree']=128*MIB
                p.next_tick=0;p.tick(point,samples)
                self.assertEqual(p.budget,0)
                p.close()
            point['memory']['MemFree']=int(720.03125*MIB)

    def test_scaled_reserve_bounds_and_explicit_pair_match(self):
        for capacity,reserve in [(2048,256),(6144,384),(65536,1024)]:
            point=dict(memory=dict(MemTotal=capacity*MIB))
            self.assertEqual(physical_reserve(point,dict(medium='ssd',reserve_policy='scaled')),reserve)
            self.assertEqual(physical_reserve(point,dict(medium='ssd')),1024)
        point=dict(memory=dict(MemTotal=5901*MIB))
        self.assertEqual(physical_reserve(point,dict(medium='ssd',reserve_policy='scaled',physical_reserve_mib=384)),384)
        with self.assertRaises(ValueError):
            physical_reserve(point,dict(medium='ssd',reserve_policy='scaled',physical_reserve_mib=0))

    def test_zero_startup_budget_waits_for_ready_without_freezing_zero_ceiling(self):
        point=dict(memory=dict(MemTotal=int(5901.4453125*MIB),MemFree=int(560.375*MIB),Cached=0,Shmem=0,Buffers=0,SReclaimable=0))
        samples=[dict(guest=dict(capacity_state=dict(active=False,
            measurement=dict(fast_bytes=0)))) for _ in range(8)]
        with tempfile.TemporaryDirectory() as tmp:
            p=GlobalPool(Path(tmp),dict(n=8,medium='ssd',strategy='P',dram_gib=6))
            p.tick(point,samples,allow_initial_commit=False)
            self.assertIsNone(p.budget)
            self.assertIsNone(p.ceiling)
            row=json.loads((Path(tmp)/'global-quotas.json').read_text())
            self.assertEqual(row['admission_state'],'bootstrap-wait')
            self.assertEqual(sum(row['grants_mib'].values()),0)
            point['memory']['MemFree']=2048*MIB
            p.next_tick=0
            p.tick(point,samples,allow_initial_commit=False)
            self.assertIsNone(p.budget)  # Physical headroom alone is not READY.
            p.next_tick=0
            p.tick(point,samples,allow_initial_commit=True)
            self.assertEqual(p.budget,1024)
            self.assertEqual(p.ceiling,1024)
            p.close()

    def test_paired_ceiling_is_preserved_across_zero_budget_boot_observation(self):
        point=dict(memory=dict(MemTotal=6144*MIB,MemFree=512*MIB,Cached=0,Shmem=0,Buffers=0,SReclaimable=0))
        samples=[dict(guest=dict(capacity_state=dict(active=False,
            measurement=dict(fast_bytes=0)))) for _ in range(8)]
        with tempfile.TemporaryDirectory() as tmp:
            p=GlobalPool(Path(tmp),dict(n=8,medium='ssd',strategy='F',dram_gib=6,payload_budget_mib=512))
            p.tick(point,samples)
            self.assertIsNone(p.budget)
            point['memory']['MemFree']=2048*MIB
            p.next_tick=0
            p.tick(point,samples)
            self.assertEqual(p.ceiling,512)
            self.assertEqual(p.budget,512)
            row=json.loads((Path(tmp)/'global-quotas.json').read_text())
            self.assertTrue(row['initial_budget_ready'])
            self.assertEqual(sum(row['grants_mib'].values()),512)
            p.close()

    def test_matched_ceiling_survives_higher_initial_overhead(self):
        point=dict(memory=dict(MemTotal=8192*MIB,MemFree=2048*MIB,
            Cached=0,Shmem=0,Buffers=0,SReclaimable=0))
        samples=[dict(guest=dict(capacity_state=dict(active=True,
            measurement=dict(fast_bytes=512*MIB)))) for _ in range(2)]
        with tempfile.TemporaryDirectory() as tmp:
            p=GlobalPool(Path(tmp),dict(n=2,medium='ssd',strategy='F',
                dram_gib=8,payload_budget_mib=3072))
            p.tick(point,samples)
            self.assertEqual(p.ceiling,3072)
            self.assertEqual(p.budget,2048)
            point['memory']['MemFree']+=1024*MIB
            p.next_tick=0
            p.tick(point,samples)
            self.assertEqual(p.ceiling,3072)
            self.assertEqual(p.budget,2112)
            p.close()

    def test_recorded_oom_state_uses_managed_capacity_and_keeps_slab_held(self):
        point = dict(nodes={'node0': dict(MemTotal=int(7989.7*MIB),
            MemFree=int(25.1*MIB), FilePages=int(7054.5*MIB),
            Shmem=int(7050.1*MIB), Dirty=int(.5*MIB),
            SReclaimable=int(302.1*MIB))})
        guard = physical_guard(point, 'cxl', 2729)
        self.assertEqual(guard['safe_payload_budget_mib'], 1728)
        # Old initialization left 2992MiB granted in this observed OOM state.
        self.assertLess(guard['safe_payload_budget_mib'], 2992)
        self.assertGreater(guard['nonpayload_overhead_mib'], 5200)

    def test_guest_release_is_not_host_release_and_host_release_recovers_gradually(self):
        point = dict(memory=dict(MemTotal=8192*MIB, MemFree=2048*MIB,
            Cached=0, Shmem=0, Buffers=0, SReclaimable=0))
        def samples(a,b):
            return [dict(guest=dict(capacity_state=dict(active=True,
                measurement=dict(fast_bytes=v*MIB)))) for v in [a,b]]
        with tempfile.TemporaryDirectory() as tmp:
            p=GlobalPool(Path(tmp),dict(n=2,medium='ssd',strategy='P',dram_gib=8))
            p.tick(point,samples(1024,1024))
            initial=p.budget
            p.next_tick=0
            p.tick(point,samples(0,1024))
            guest_only=p.budget
            self.assertLess(guest_only,initial)
            point['memory']['MemFree']+=1024*MIB
            p.next_tick=0
            p.tick(point,samples(0,1024))
            self.assertEqual(p.budget,guest_only+64)
            p.close()

    def test_dirty_cache_and_reserved_capacity_are_not_spendable(self):
        point=dict(memory=dict(MemTotal=7680*MIB,MemFree=1024*MIB,
            Cached=2048*MIB,Shmem=0,Dirty=2048*MIB,Writeback=0,SReclaimable=1024*MIB))
        guard=physical_guard(point,'ssd',2048)
        self.assertEqual(guard['clean_file_credit_mib'],0)
        self.assertEqual(guard['fast_managed_mib'],7680)
        self.assertEqual(guard['safe_payload_budget_mib'],2048)

    def test_pending_release_does_not_create_credit(self):
        desired = {'wait': 16, 'active': 1024}
        held = policy.settle_grants(desired, {'wait': 1024, 'active': 0},
            {'wait': 1024, 'active': 0}, 1024, ['active', 'wait'])
        self.assertEqual(held['active'], 0)
        released = policy.settle_grants(desired, held,
            {'wait': 16, 'active': 0}, 1024, ['active', 'wait'])
        self.assertEqual(released['active'], 1008)

    def test_global_budget_and_static_capacity_are_conserved(self):
        point = dict(memory={'MemTotal': 12*1024*MIB, 'MemFree': 8*1024*MIB,
            'Cached': 0, 'Shmem': 0, 'Buffers': 0, 'SReclaimable': 0})
        samples = [dict(guest=dict(capacity_state=dict(active=active,
            measurement=dict(fast_bytes=256*MIB)))) for active in [True, False]]
        for strategy in ['P', 'F']:
            with tempfile.TemporaryDirectory() as tmp:
                p = GlobalPool(Path(tmp), dict(n=2, medium='ssd', strategy=strategy,
                    dram_gib=12, payload_budget_mib=1024))
                p.tick(point, samples)
                row = json.loads((Path(tmp)/'global-quotas.json').read_text())
                self.assertLessEqual(sum(row['grants_mib'].values()),1024)
                if strategy == 'P':self.assertEqual(row['grants_mib']['slot-1'],16)
                else:self.assertEqual(row['grants_mib'],{'slot-0':512,'slot-1':512})
                p.close()

    def test_guest_target_applies_host_grant_in_both_phases(self):
        # Evaluate the actual target method with controlled input, without
        # constructing a controller that writes kernel interfaces.
        paths=[H/'artifacts'/name/'adaptive_controller.py' for name in ['vmm-v66-ssd-compact','vmm-v66-ssd-compact-readguard','vmm-v67-compact-cxl','vmm-v67-compact-cxl-readguard']]
        self.assertEqual(len(paths),4)
        for path in paths:
            tree=ast.parse(path.read_text())
            cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='Controller')
            method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='target_mib')
            mod=ast.Module(body=[method],type_ignores=[])
            with tempfile.TemporaryDirectory() as tmp:
                pressure=Path(tmp)/'host-pressure.json'
                pressure.write_text(json.dumps(dict(global_quota_mib=128,global_quota_seq=7)))
                env=dict(Path=lambda _:pressure,json=json)
                exec(compile(ast.fix_missing_locations(mod),str(path),'exec'),env)
                class DummyPolicy:
                    def target(self,active):return 6144 if active else 16
                class Dummy:
                    policy=DummyPolicy()
                    active=True
                dummy=Dummy()
                self.assertEqual(env['target_mib'].fget(dummy),128)
                dummy.active=False
                self.assertEqual(env['target_mib'].fget(dummy),16)


if __name__=='__main__':unittest.main()
