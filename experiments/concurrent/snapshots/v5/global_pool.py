"""Cross-VMM adapter for the existing capacity and physical-credit algorithms."""
import importlib.util
import json
import math
from pathlib import Path
import time

H = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('existing_global_policy',
    H / 'artifacts/vmm-v67-compact-cxl/cxl_controller.py')
policy = importlib.util.module_from_spec(spec)
import sys
sys.modules[spec.name] = policy
spec.loader.exec_module(policy)
MIB = 1 << 20


def fast_used(point, medium):
    if medium == 'ssd':
        m = point['memory']
        return m['MemTotal'] - m['MemFree'] - max(0, m['Cached'] - m['Shmem']) - m['Buffers'] - m['SReclaimable']
    m = point['nodes']['node0']
    return m['MemTotal'] - m['MemFree'] - max(0, m['FilePages'] - m['Shmem']) - m['SReclaimable']


class GlobalPool:
    def __init__(self, folder, spec):
        self.folder, self.spec = folder, spec
        self.budget = None
        self.previous = {f'slot-{i}': 0 for i in range(spec['n'])}
        self.seq = 0
        self.next_tick = 0
        self.log = (folder / 'global-pool.jsonl').open('x', buffering=1)

    def tick(self, point, samples):
        now = time.monotonic()
        if now < self.next_tick or any(sample is None for sample in samples):
            return
        self.next_tick = now + .25
        states = [sample['guest'].get('capacity_state') for sample in samples]
        if any(state is None for state in states):
            return
        resident = {f'slot-{i}': math.ceil(state['measurement']['fast_bytes'] / MIB)
                    for i, state in enumerate(states)}
        if self.budget is None:
            overhead = max(0, fast_used(point, self.spec['medium']) / MIB - sum(resident.values()))
            # Keep a full GiB beyond measured non-payload residency for bursts.
            measured_budget = int((self.spec['dram_gib'] * 1024 - overhead - 1024) // 16) * 16
            self.budget = self.spec.get('payload_budget_mib', measured_budget)
            if self.budget < self.spec['n'] * 16:
                raise RuntimeError(('no payload DRAM budget after host overhead', overhead, self.budget))
            self.overhead = overhead
        demands = {f'slot-{i}': 6144 if state['active'] else 16 for i, state in enumerate(states)}
        order = sorted(demands, key=lambda k: (-demands[k], k))
        if self.spec['strategy'] == 'F':
            q, rem = divmod(self.budget, self.spec['n'])
            granted = {key: min(6144, q + int(i < rem)) for i, key in enumerate(sorted(demands))}
        else:
            desired = policy.waterfill(demands, dict.fromkeys(demands, 1.0), self.budget)
            granted = policy.settle_grants(desired, self.previous, resident, self.budget, order)
        self.previous = granted
        self.seq += 1
        row = dict(seq=self.seq, epoch=time.time(), budget_mib=self.budget,
            nonpayload_overhead_mib=self.overhead, reserve_mib=1024, resident_mib=resident,
            demand_mib=demands, grants_mib=granted, strategy=self.spec['strategy'],
            fast_physical_used_mib=fast_used(point, self.spec['medium']) / MIB)
        self.log.write(json.dumps(row) + '\n')
        path = self.folder / 'global-quotas.json'
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(row))
        tmp.replace(path)

    def close(self):
        self.log.close()
