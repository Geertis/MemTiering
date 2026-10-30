"""Cross-VMM adapter for the existing capacity and physical-credit algorithms."""
import importlib.util
import json
import math
from pathlib import Path
import time

H = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('existing_global_policy',
    H.parent / 'cxl/cxl_controller.py')
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


def physical_guard(point, medium, resident_mib, reserve_mib=1024):
    """Credit clean file cache, but keep dirty pages, buffers and all slab held.

    Host-managed capacity excludes reserved pages. The physical observation also
    accounts for Guest pages not yet returned through free-page reporting.
    """
    m = point['memory'] if medium == 'ssd' else point['nodes']['node0']
    file_pages = m.get('Cached', 0) if medium == 'ssd' else m.get('FilePages', 0)
    clean_file = max(0, file_pages - m.get('Shmem', 0)
                     - m.get('Dirty', 0) - m.get('Writeback', 0))
    managed = m['MemTotal'] / MIB
    used = (m['MemTotal'] - m['MemFree'] - clean_file) / MIB
    overhead = max(0, used - resident_mib)
    available = max(0, int((managed - overhead - reserve_mib) // 16) * 16)
    return dict(fast_managed_mib=managed, fast_safe_used_mib=used,
        nonpayload_overhead_mib=overhead, physical_headroom_mib=managed-used,
        safe_payload_budget_mib=available, clean_file_credit_mib=clean_file/MIB,
        reserve_shortfall_mib=max(0, reserve_mib-(managed-used)))


def physical_reserve(point, spec):
    """Size the optional small-host reserve; fixed 1GiB remains the default.

    Scaled cohorts reserve 1/16 of managed DRAM, rounded down to 16MiB,
    bounded by 256MiB and 1GiB. A paired control can pin the same reserve.
    """
    if spec.get('reserve_policy', 'fixed') == 'fixed':
        return 1024
    if spec['reserve_policy'] != 'scaled':
        raise ValueError('unknown physical reserve policy')
    m = point['memory'] if spec['medium'] == 'ssd' else point['nodes']['node0']
    proposed = min(1024, max(256, int((m['MemTotal']/MIB/16)//16)*16))
    reserve = spec.get('physical_reserve_mib', proposed)
    if type(reserve) is not int or not 256 <= reserve <= 1024:
        raise ValueError('paired reserve outside declared bounds')
    return reserve


class GlobalPool:
    def __init__(self, folder, spec):
        self.folder, self.spec = folder, spec
        self.budget = None
        self.ceiling = None
        self.previous = {f'slot-{i}': 0 for i in range(spec['n'])}
        self.seq = 0
        self.next_tick = 0
        self.log = (folder / 'global-pool.jsonl').open('x', buffering=1)

    def tick(self, point, samples, *, allow_initial_commit=True):
        now = time.monotonic()
        if now < self.next_tick or any(sample is None for sample in samples):
            return
        self.next_tick = now + .1
        timestamp = point.get('monotonic_ns')
        if timestamp is not None and any(timestamp - sample.get('host_monotonic_ns', timestamp) > 1e9
                                          for sample in samples):
            return  # A stale Guest observation cannot create release credit.
        states = [sample['guest'].get('capacity_state') for sample in samples]
        if any(state is None for state in states):
            return
        for sample,state in zip(samples,states):
            # Transport freshness does not imply the controller is alive:
            # guest_state may keep streaming the same old capacity record.
            if state.get('controller_alive') is False:
                return
            if self.spec.get('require_controller_health') and state.get('controller_alive') is not True:
                return
            guest_now=sample['guest'].get('guest_monotonic_ns')
            capacity_at=state.get('monotonic_ns')
            if guest_now is not None and capacity_at is not None and not 0 <= guest_now-capacity_at <= 5_000_000_000:
                return
        resident = {f'slot-{i}': math.ceil(state['measurement']['fast_bytes'] / MIB)
                    for i, state in enumerate(states)}
        reserve = physical_reserve(point, self.spec)
        guard = physical_guard(point, self.spec['medium'], sum(resident.values()), reserve)
        safe = guard['safe_payload_budget_mib']
        self.overhead = guard['nonpayload_overhead_mib']
        self.peak_overhead = max(getattr(self, 'peak_overhead', 0), self.overhead)
        if self.budget is None:
            ceiling = self.spec.get('payload_budget_mib', safe)
            # Boot residency is provisional. Do not crash or freeze a zero
            # ceiling before the READY barrier and physical headroom exist.
            if allow_initial_commit and min(ceiling, safe) >= self.spec['n'] * 16:
                self.ceiling = ceiling
                self.budget = min(ceiling, safe)
        else:
            # Shrink immediately when unregulated residency grows. Recover at
            # most 64MiB/tick, and only from verified physical availability.
            self.budget = min(self.ceiling, safe, self.budget + 64)
        effective_budget = self.budget if self.budget is not None else 0
        demands = {f'slot-{i}': 6144 if state['active'] else 16 for i, state in enumerate(states)}
        order = sorted(demands, key=lambda k: (-demands[k], k))
        if self.spec['strategy'] == 'F':
            q, rem = divmod(effective_budget, self.spec['n'])
            desired = {key: min(6144, q + int(i < rem)) for i, key in enumerate(sorted(demands))}
        else:
            desired = policy.waterfill(demands, dict.fromkeys(demands, 1.0), effective_budget)
        granted = policy.settle_grants(desired, self.previous, resident, effective_budget, order)
        self.previous = granted
        self.seq += 1
        row = dict(seq=self.seq, epoch=time.time(), budget_mib=effective_budget,
            budget_ceiling_mib=self.ceiling, peak_nonpayload_overhead_mib=self.peak_overhead,
            initial_budget_ready=self.budget is not None,
            admission_state='admitted' if self.budget is not None else 'bootstrap-wait',
            minimum_initial_budget_mib=self.spec['n'] * 16,
            reserve_mib=reserve, reserve_policy=self.spec.get('reserve_policy','fixed'),
            resident_mib=resident, **guard,
            demand_mib=demands, grants_mib=granted, strategy=self.spec['strategy'],
            fast_physical_used_mib=fast_used(point, self.spec['medium']) / MIB)
        self.log.write(json.dumps(row) + '\n')
        path = self.folder / 'global-quotas.json'
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(row))
        tmp.replace(path)

    def close(self):
        self.log.close()
