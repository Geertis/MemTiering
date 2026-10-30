import ast
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from global_pool import GlobalPool, MIB, policy

H = Path(__file__).resolve().parent


class GlobalPoolTest(unittest.TestCase):
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
        for path in (H/'artifacts').glob('*/adaptive_controller.py'):
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
