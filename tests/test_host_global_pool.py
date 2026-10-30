import ast
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'runtime/host'))
from global_pool import GlobalPool, MIB, physical_guard, policy

H = Path(__file__).resolve().parents[1] / 'experiments/phase'


class GlobalPoolTest(unittest.TestCase):
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
