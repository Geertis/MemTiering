#!/usr/bin/env python3
"""Fixed-budget Sandbox DRAM coordinator; telemetry is actual memcg residency.

Quotas are placement targets. Lowered quotas release credit only after physical
residency falls. The fixed Guest fast NUMA node is the physical capacity cap.
No future trace timestamps or original resource measurements enter this policy.
"""
from __future__ import annotations
import argparse
from collections import deque
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import selectors
import socket
import time

PAGE = 4096
MIB = 1 << 20
NUMA_SIZE_FIELDS = frozenset("""anon file kernel_stack pagetables sec_pagetables
    shmem file_mapped file_dirty file_writeback swapcached anon_thp file_thp
    shmem_thp inactive_anon active_anon inactive_file active_file unevictable
    slab_reclaimable slab_unreclaimable""".split())


def numa_categories(group: Path) -> dict[int, dict[str, int]]:
    """Byte occupancy only; workingset event counters are not page counts.

    shmem/unevictable are overlapping subsets, never add them to anon+file.
    """
    result: dict[int, dict[str, int]] = {}
    for line in (group / "memory.numa_stat").read_text().splitlines():
        parts = line.split()
        if not parts or parts[0] not in NUMA_SIZE_FIELDS:
            continue
        for item in parts[1:]:
            name, value = item.split("=", 1)
            result.setdefault(int(name[1:]), {})[parts[0]] = int(value) // PAGE
    return result


def numa_bytes(group: Path, node: int) -> int:
    values = numa_categories(group).get(node, {})
    return PAGE * (values.get("anon", 0) + values.get("file", 0))


def read_stats(path: Path) -> dict[str, int]:
    return {key: int(value) for key, value in (line.split() for line in path.read_text().splitlines())}


def waterfill(demands: dict[str, int], weights: dict[str, float], budget: int) -> dict[str, int]:
    """Weighted max-min allocation, capped at demand, with integer conservation."""
    if budget < 0 or any(v < 0 for v in demands.values()):
        raise ValueError("negative budget/demand")
    result = dict.fromkeys(demands, 0)
    pending = {key for key, demand in demands.items() if demand}
    remaining = budget
    while pending and remaining:
        total_weight = sum(weights[key] for key in pending)
        if total_weight <= 0 or not math.isfinite(total_weight):
            raise ValueError("weights must be positive and finite")
        capped = [key for key in sorted(pending)
                  if demands[key] <= remaining * weights[key] / total_weight]
        if capped:
            for key in capped:
                result[key] = demands[key]
                remaining -= demands[key]
                pending.remove(key)
            continue
        for key in sorted(pending):
            result[key] = int(remaining * weights[key] / total_weight)
        slack = remaining - sum(result[key] for key in pending)
        for key in sorted(pending)[:slack]:
            result[key] += 1
        break
    assert sum(result.values()) <= budget
    assert all(0 <= result[key] <= demands[key] for key in demands)
    return result


def settle_grants(desired: dict[str, int], previous: dict[str, int],
                  resident: dict[str, int], budget: int, order: list[str]) -> dict[str, int]:
    """Revoke first; never spend credit from pages that have not left DRAM."""
    granted = {key: min(previous[key], desired[key]) for key in desired}
    committed = sum(max(granted[key], resident[key]) for key in desired)
    # Allocation can grow past a soft target between ticks. Revoke unbacked
    # reservations first; occupied pages still consume credit until migrated.
    excess = max(0, committed - max(budget, sum(resident.values())))
    for key in reversed(order):
        revoke = min(max(0, granted[key] - resident[key]), excess)
        granted[key] -= revoke
        committed -= revoke
        excess -= revoke
    for key in order:
        owned = max(granted[key], resident[key])
        target = min(desired[key], owned + max(0, budget - committed))
        # Raising a quota within already occupied pages costs no residency
        # credit, but all kernel quota grants must still sum to the budget.
        increase = min(max(0, target - granted[key]),
                       max(0, budget - sum(granted.values())))
        granted[key] += increase
        committed += max(granted[key], resident[key]) - owned
    assert sum(granted.values()) <= budget
    # Observed overshoot may predate the coordinator. It does not create credit.
    assert sum(max(granted[k], resident[k]) for k in granted) <= max(budget, sum(resident.values()))
    return granted


@dataclass
class State:
    key: str
    group: Path
    interface: Path
    weight: float = 1.0
    sensitivity: float = 1.0
    pressure: float = 0.0
    phase: int = 0
    kind: int = 0
    quota: int = 0
    fast: int = 0
    slow: int = 0
    occupancy: dict = field(default_factory=dict)
    start_ns: int = 0
    observed_peak: int = 0
    start_resident: int = 0
    pending_active: bool = False
    next_horizon_ns: int = 0
    start_anon: int = 0
    start_file: int = 0
    peak_anon: int = 0
    peak_file: int = 0
    duration: dict[int, deque] = field(default_factory=lambda: {k: deque(maxlen=32) for k in range(4)})
    growth: dict[int, deque] = field(default_factory=lambda: {k: deque(maxlen=32) for k in range(4)})
    anon_growth: dict[int, deque] = field(default_factory=lambda: {k: deque(maxlen=32) for k in range(4)})
    file_growth: dict[int, deque] = field(default_factory=lambda: {k: deque(maxlen=32) for k in range(4)})
    startup_headroom_pages: int = 0
    last_cpu: int = 0
    last_total: int = 0
    inferred: bool = False
    last_activity_ns: int = 0

    def horizon_ms(self, now_ns: int | None = None) -> int:
        values = sorted(self.duration[self.kind])
        if now_ns is not None and self.phase and self.start_ns:
            elapsed_ms = max(0, (now_ns - self.start_ns) / 1e6)
            values = [v - elapsed_ms for v in values if v > elapsed_ms]
        # Lower quartile of completed observations is deliberately conservative.
        return min(5000, max(100, int(values[(len(values) - 1) // 4]))) if values else 1000

    def headroom(self) -> int:
        # Completed phase experience only. File growth is often disposable
        # cache, so it may reserve at most 16 MiB beyond anonymous growth.
        # This caps headroom, not the Sandbox's actual memory allocation.
        anon, cache = sorted(self.anon_growth[self.kind]), sorted(self.file_growth[self.kind])
        # Search retains the conservative historical path. Other tools get
        # an explicit startup reservation before fork/exec, capped by the ledger.
        # This uses the current command class, never future trace measurements.
        floor = self.startup_headroom_pages if self.kind != 1 else 0
        if not anon:
            return max(floor, 128 * MIB // PAGE)
        return max(floor, 16 * MIB // PAGE, anon[(3 * (len(anon) - 1)) // 4] +
                   min(16 * MIB // PAGE, cache[(3 * (len(cache) - 1)) // 4]))


class ReportingGate:
    """Guest-wide free-page reporting; enter only when every Sandbox waits.

    Lowering the floor requests a bounded asynchronous kernel pass. Raising
    it stops new small-block isolation; an already submitted batch can finish.
    Neither operation creates allocation credit or a Host reservation.
    """
    def __init__(self, path: Path, active_order: int, wait_order: int, delay_ms: int,
                 pause_path: Path | None = None, drain_only: bool = False,
                 barrier_path: Path | None = None):
        if not 0 <= wait_order <= active_order <= 9 or delay_ms < 0:
            raise ValueError("invalid reporting orders or WAIT delay")
        self.path = path
        self.active_order, self.wait_order = active_order, wait_order
        self.delay_ns = delay_ms * 1_000_000
        self.wait_since = None
        self.current = int(path.read_text())
        self.pause_path = pause_path
        self.paused = self.read_pause() if pause_path else None
        if drain_only and not pause_path:
            raise ValueError('fine drain needs the synchronous pause interface')
        self.drain_only = drain_only
        self.barrier_path = barrier_path
        self.wait_mode = True
        self.restore()

    def read_pause(self) -> bool:
        value = self.pause_path.read_text().strip().lower()
        if value not in ('y', 'n', '1', '0'):
            raise RuntimeError('invalid reporting pause value')
        return value in ('y', '1')

    def pause(self, value: bool) -> bool:
        if not self.pause_path or self.paused == value:
            return False
        # The kernel write drains queued and in-flight reporting before return.
        self.pause_path.write_text('1' if value else '0')
        if self.read_pause() != value:
            raise RuntimeError('reporting pause write did not apply')
        self.paused = value
        return True

    def status(self) -> dict:
        parent = self.path.parent
        result = {'reporting_paused': self.paused}
        for name in ['page_reporting_runs', 'page_reporting_batches', 'page_reporting_fine_batches',
                     'page_reporting_pause_wait_ns', 'page_reporting_barrier_wait_ns']:
            path = parent/name
            if path.exists():
                result[name] = int(path.read_text())
        return result

    def change(self, order: int) -> bool:
        if order == self.current:
            return False
        self.path.write_text(str(order))
        if int(self.path.read_text()) != order:
            raise RuntimeError("reporting order write did not apply")
        self.current = order
        return True

    def update(self, all_wait: bool, now: int) -> bool:
        if not all_wait:
            return self.restore()
        if self.wait_since is None:
            self.wait_since = now
        if now - self.wait_since >= self.delay_ns:
            self.wait_mode = True
            changed = self.change(self.wait_order)
            return self.pause(False) or changed
        return False

    def restore(self) -> bool:
        self.wait_since = None
        if self.barrier_path:
            if not self.wait_mode:
                return False
            self.change(self.active_order)
            self.barrier_path.write_text('1')
            self.wait_mode = False
            return True
        if self.drain_only:
            if not self.wait_mode:
                return False
            self.pause(True)
            self.change(self.active_order)
            self.pause(False)
            self.wait_mode = False
            return True
        changed = self.pause(True)
        return self.change(self.active_order) or changed

    def close(self) -> None:
        self.change(self.active_order)
        self.pause(False)


class Controller:
    def __init__(self, states: list[State], budget: int, fast_node: int, slow_node: int,
                 minimum: int, log, use_events: bool = True, refresh_horizons: bool = False,
                 reporting: ReportingGate | None = None):
        if budget < len(states) * minimum:
            raise ValueError("budget is smaller than Sandbox minimums")
        self.states = {s.key: s for s in states}
        self.budget = budget
        self.fast_node, self.slow_node = fast_node, slow_node
        self.minimum = minimum
        self.log = log
        self.use_events = use_events
        self.refresh_horizons = refresh_horizons
        self.reporting = reporting
        self.tick_index = 0
        self.log_seq = 0

    def emit(self, event: dict) -> None:
        self.log_seq += 1
        self.log.write(json.dumps({"seq": self.log_seq, "monotonic_ns": time.monotonic_ns(), **event}) + "\n")
        self.log.flush()

    def measure(self) -> None:
        now = time.monotonic_ns()
        for s in self.states.values():
            # Snapshot the commit watermark before reading asynchronous memcg
            # counters. The kernel charges commits racing with this read.
            committed_before = int((s.interface / "allocation_snapshot").read_text())
            nodes = numa_categories(s.group)
            fast, slow = nodes.get(self.fast_node, {}), nodes.get(self.slow_node, {})
            s.fast = fast.get("anon", 0) + fast.get("file", 0)
            s.slow = slow.get("anon", 0) + slow.get("file", 0)
            s.occupancy = {"fast_categories_pages": fast, "slow_categories_pages": slow,
                           "other_total_pages_node_unknown": max(0, int((s.group / "memory.current").read_text()) // PAGE - s.fast - s.slow)}
            cpu = read_stats(s.group / "cpu.stat").get("usage_usec", 0)
            total = s.fast + s.slow
            for line in (s.group / "memory.pressure").read_text().splitlines():
                if line.startswith("some "):
                    s.pressure = float(dict(p.split("=", 1) for p in line.split()[1:])["avg10"]) / 100
            active = cpu - s.last_cpu > 2000 or total - s.last_total > 256
            s.last_cpu, s.last_total = cpu, total
            if active:
                s.last_activity_ns = now
            # Runtime events are optional. CPU progress and memory growth infer
            # ACTIVE; a 500 ms quiet interval infers WAIT, without future timing.
            if not self.use_events or s.inferred:
                phase = int(now - s.last_activity_ns < 500_000_000)
                if phase != s.phase:
                    self.set_phase(s, phase, 0, now, inferred=True)
            if s.phase:
                s.observed_peak = max(s.observed_peak, total)
                s.peak_anon = max(s.peak_anon, fast.get("anon", 0) + slow.get("anon", 0))
                s.peak_file = max(s.peak_file, fast.get("file", 0) + slow.get("file", 0))
            (s.interface / "residency").write_text(f"{s.fast} {s.slow} {committed_before}\n")

    def set_phase(self, s: State, phase: int, kind: int, now: int, inferred: bool = False) -> None:
        if s.phase and not phase and s.start_ns:
            s.duration[s.kind].append((now - s.start_ns) / 1e6)
            s.growth[s.kind].append(max(0, s.observed_peak - s.start_resident))
            s.anon_growth[s.kind].append(max(0, s.peak_anon - s.start_anon))
            s.file_growth[s.kind].append(max(0, s.peak_file - s.start_file))
        s.phase, s.kind, s.inferred = phase, kind, inferred
        s.start_ns = now
        s.observed_peak = s.start_resident = s.fast + s.slow
        f = s.occupancy.get("fast_categories_pages", {})
        c = s.occupancy.get("slow_categories_pages", {})
        s.peak_anon = s.start_anon = f.get("anon", 0) + c.get("anon", 0)
        s.peak_file = s.start_file = f.get("file", 0) + c.get("file", 0)
        # WAIT must stop promotion before revocation. ACTIVE must see committed
        # residency and quota before waking the kernel placement worker.
        s.pending_active = bool(phase)
        if not phase:
            (s.interface / "phase").write_text(f"0 {kind} {s.horizon_ms()}\n")
        self.emit({"event": "phase", "sandbox": s.key, "phase": phase, "kind": kind,
                   "inferred": inferred, "horizon_ms": s.horizon_ms()})

    def rebalance(self) -> None:
        self.measure()
        if self.reporting:
            now = time.monotonic_ns()
            all_wait = all(not s.phase and now - s.last_activity_ns >= self.reporting.delay_ns
                           for s in self.states.values())
            if self.reporting.update(all_wait, now):
                self.emit({"event": "reporting_order", "order": self.reporting.current,
                           "all_wait": all_wait, **self.reporting.status()})
        demands = {k: self.minimum + (s.fast + s.slow + s.headroom() if s.phase else 0)
                   for k, s in self.states.items()}
        weights = {k: s.weight * s.sensitivity * (4 if s.phase else 1) *
                   (1 + s.slow / max(1, s.fast + s.slow) + min(s.pressure, 1))
                   for k, s in self.states.items()}
        # Reserve the minimum first, then share only the remaining capacity.
        extra = waterfill({k: max(0, d - self.minimum) for k, d in demands.items()},
                          weights, self.budget - len(demands) * self.minimum)
        desired = {k: self.minimum + extra[k] for k in demands}
        keys = sorted(self.states)
        rotation = self.tick_index % len(keys)
        order = keys[rotation:] + keys[:rotation]
        grants = settle_grants(desired, {k: s.quota for k, s in self.states.items()},
                               {k: s.fast for k, s in self.states.items()}, self.budget, order)
        # Write reductions before increases, mirroring the reservation ledger.
        for key in sorted(keys, key=lambda k: grants[k] - self.states[k].quota):
            s = self.states[key]
            if s.quota != grants[key]:
                (s.interface / "quota_pages").write_text(str(grants[key]))
                s.quota = grants[key]
        for s in self.states.values():
            if s.pending_active:
                (s.interface / "phase").write_text(f"1 {s.kind} {s.horizon_ms()}\n")
                s.pending_active = False
                self.emit({"event": "phase_applied", "sandbox": s.key,
                           "quota_pages": s.quota, "fast_pages": s.fast})
        self.tick_index += 1
        now = time.monotonic_ns()
        if self.refresh_horizons:
            for s in self.states.values():
                if s.phase and now >= s.next_horizon_ns and (s.interface / "horizon_ms").exists():
                    horizon = s.horizon_ms(now)
                    (s.interface / "horizon_ms").write_text(str(horizon))
                    s.next_horizon_ns = now + 250_000_000
                    self.emit({"event": "horizon", "sandbox": s.key, "horizon_ms": horizon})
        self.emit({"event": "quota", "budget_pages": self.budget, "controller_cpu_ns": time.process_time_ns(),
                   "committed_pages": sum(max(s.quota, s.fast) for s in self.states.values()),
                   "resident_pages": sum(s.fast for s in self.states.values()),
                   "states": {k: {"phase": s.phase, "quota": s.quota, "desired": desired[k],
                                   "fast": s.fast, "slow": s.slow, "psi_some": s.pressure,
                                   "occupancy": s.occupancy, "headroom_pages": s.headroom(),
                                   "anonymous_growth_pages": list(s.anon_growth[s.kind]),
                                   "file_growth_pages": list(s.file_growth[s.kind])}
                              for k, s in self.states.items()}})

    def event(self, message: dict) -> dict:
        s = self.states[str(message["sandbox"])]
        action = message["event"]
        if action not in ("start", "end", "fallback"):
            raise ValueError("unknown phase event")
        # Restore the coarse floor before tool-start ACK/fork, including
        # reactive experiments where the phase will only be inferred later.
        if action == "start" and self.reporting and self.reporting.restore():
            self.emit({"event": "reporting_order", "order": self.reporting.current,
                       "all_wait": False, "trigger": "runtime_start",
                       **self.reporting.status()})
        if self.reporting:
            self.emit({"event": "reporting_boundary", "sandbox": s.key,
                       "action": action, **self.reporting.status()})
        self.measure()
        if self.use_events:
            self.set_phase(s, int(action == "start"), int(message.get("kind", 0)),
                           time.monotonic_ns(), inferred=action == "fallback")
        self.rebalance()
        # Prefer the fast node only when the actual reservation ledger has
        # room. MPOL_PREFERRED allows fallback to CXL if physical DRAM fills.
        committed = sum(max(x.quota, x.fast) for x in self.states.values())
        fast_allowed = s.phase and s.quota > s.fast + 16 and committed <= self.budget
        return {"preferred_node": self.fast_node if fast_allowed else self.slow_node,
                "quota_pages": s.quota, "fast_pages": s.fast, "horizon_ms": s.horizon_ms(),
                "reporting_order": self.reporting.current if self.reporting else None,
                **(self.reporting.status() if self.reporting else {})}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--budget-mib", type=int, required=True)
    parser.add_argument("--minimum-mib", type=int, default=16)
    parser.add_argument("--startup-headroom-mib", type=int, default=0)
    parser.add_argument("--fast-node", type=int, required=True)
    parser.add_argument("--slow-node", type=int, required=True)
    parser.add_argument("--sysfs", type=Path, default=Path("/sys/kernel/mm/demeter/sandboxes"))
    parser.add_argument("--without-runtime-events", action="store_true")
    parser.add_argument("--interval-ms", type=int, default=50)
    parser.add_argument("--without-horizon-refresh", action="store_true")
    parser.add_argument("--wait-reporting-order", type=int, default=None)
    parser.add_argument("--active-reporting-order", type=int, default=5)
    parser.add_argument("--wait-reporting-delay-ms", type=int, default=500)
    parser.add_argument("--pause-reporting-active", action="store_true")
    parser.add_argument("--drain-reporting-start", action="store_true")
    parser.add_argument("--barrier-reporting-start", action="store_true")
    args = parser.parse_args()
    if not 20 <= args.interval_ms <= 1000:
        parser.error("interval must be 20–1000 ms")
    if not 0 <= args.startup_headroom_mib <= args.budget_mib:
        parser.error("startup headroom must fit the budget")
    reporting = None
    if sum([args.pause_reporting_active,args.drain_reporting_start,args.barrier_reporting_start])>1:
        parser.error('choose ACTIVE pause or start drain')
    if (args.pause_reporting_active or args.drain_reporting_start or args.barrier_reporting_start) and args.wait_reporting_order is None:
        parser.error('ACTIVE pause needs a WAIT reporting order')
    if args.wait_reporting_order is not None:
        reporting = ReportingGate(Path("/sys/module/page_reporting/parameters/page_reporting_order"),
                                  args.active_reporting_order, args.wait_reporting_order,
                                  args.wait_reporting_delay_ms,
                                  Path('/sys/module/page_reporting/parameters/page_reporting_paused')
                                  if args.pause_reporting_active or args.drain_reporting_start else None,
                                  drain_only=args.drain_reporting_start,
                                  barrier_path=Path('/sys/module/page_reporting/parameters/page_reporting_barrier')
                                  if args.barrier_reporting_start else None)
    interval = args.interval_ms / 1000
    config = json.loads(args.config.read_text())
    if not config or len(config) > 8 or len({str(c["id"]) for c in config}) != len(config):
        parser.error("need 1–8 distinct Sandbox IDs")
    if args.socket.exists():
        parser.error("controller socket already exists")
    states = [State(str(c["id"]), Path(c["cgroup"]), args.sysfs / str(i),
                    weight=float(c.get("weight", 1)), sensitivity=float(c.get("sensitivity", 1)),
                    startup_headroom_pages=args.startup_headroom_mib * MIB // PAGE)
              for i, c in enumerate(config)]
    if any(not math.isfinite(s.weight * s.sensitivity) or s.weight <= 0 or s.sensitivity <= 0 for s in states):
        parser.error("Sandbox weights must be positive and finite")
    (args.sysfs / "nr_sandboxes").write_text(str(len(states)))
    try:
        (args.sysfs / "budget_pages").write_text(str(args.budget_mib * MIB // PAGE))
        for s in states:
            relative = s.group.relative_to("/sys/fs/cgroup")
            (s.interface / "cgroup").write_text("/" + str(relative))
        with args.log.open("w", encoding="utf-8") as log, socket.socket(socket.AF_UNIX) as server:
            controller = Controller(states, args.budget_mib * MIB // PAGE, args.fast_node,
                                    args.slow_node, args.minimum_mib * MIB // PAGE, log,
                                    use_events=not args.without_runtime_events,
                                    refresh_horizons=not args.without_horizon_refresh,
                                    reporting=reporting)
            controller.rebalance()
            server.bind(str(args.socket))
            server.listen(32)
            selector = selectors.DefaultSelector()
            selector.register(server, selectors.EVENT_READ)
            deadline = time.monotonic() + interval
            while True:
                for _key, _mask in selector.select(max(0, deadline - time.monotonic())):
                    with server.accept()[0] as connection:
                        connection.settimeout(2)
                        try:
                            data = connection.makefile("rb").readline(8192)
                            response = controller.event(json.loads(data))
                        except Exception as error:
                            controller.emit({"event": "error", "error": repr(error)})
                            response = {"error": repr(error)}
                        connection.sendall(json.dumps(response).encode() + b"\n")
                if time.monotonic() >= deadline:
                    controller.rebalance()
                    for s in states:
                        controller.emit({"event": "placement", "sandbox": s.key,
                                         "stats": read_stats(s.interface / "stats")})
                    deadline = time.monotonic() + interval
    finally:
        if reporting:
            reporting.close()
        args.socket.unlink(missing_ok=True)
        (args.sysfs / "nr_sandboxes").write_text("0")


if __name__ == "__main__":
    main()
