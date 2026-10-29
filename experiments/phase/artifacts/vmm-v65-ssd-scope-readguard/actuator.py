"""Resident-aware SSD admission and bounded, tool-scoped feedback."""
from collections import OrderedDict
import math


class ToolHistory:
    """Reuse only completed evidence from the same Runtime tool signature.

    A signature identifies the tool and its arguments, not code state or page
    identity. Global refault protection remains in the controller. LRU eviction
    forgets historical hints; it never changes the live memory ledger.
    """
    def __init__(self, ceiling_mib, stop_reuse=True, scope='signature', capacity=64):
        if scope not in ('signature', 'session') or capacity < 1:
            raise ValueError('invalid history scope/capacity')
        self.ceiling_mib, self.stop_reuse = ceiling_mib, stop_reuse
        self.scope, self.capacity = scope, capacity
        self.entries = OrderedDict()

    def select(self, signature):
        if not isinstance(signature, str) or not signature:
            raise ValueError('missing Runtime tool signature')
        key = signature if self.scope == 'signature' else 'session'
        hit = key in self.entries
        if hit:
            placement, io = self.entries.pop(key)
        else:
            placement = BudgetPlacement(self.ceiling_mib, stop_reuse=self.stop_reuse)
            io = ReuseHistory()
        self.entries[key] = (placement, io)
        while len(self.entries) > self.capacity:
            self.entries.popitem(last=False)
        return placement, io, hit


class ReclaimProgress:
    """Pace reclaim by observed work, with bounded backoff on no progress.

    memory.reclaim may partially succeed, including when it returns EAGAIN.
    Use memcg pgsteal deltas where available, bounded by the request. A net
    charge decrease is a fallback only: allocation/exit can change it during a
    reclaim. Neither observation is a Host physical-memory return receipt.
    """
    def __init__(self):
        self.cost = {False: .002, True: .002}
        self.begin()

    def begin(self):
        self.failures = {False: 0, True: 0}
        self.retry_after = {False: 0., True: 0.}

    def ready(self, anonymous, now):
        return now >= self.retry_after[anonymous]

    def observe(self, result, now):
        anonymous = bool(result['anonymous_allowed'])
        requested = max(0, result['requested_bytes'])
        net = max(0, result['before_bytes'] - result['after_bytes'])
        stolen = result.get('pgsteal_bytes')
        observed = min(requested, max(0, stolen if stolen is not None else net))
        elapsed = max(0., result['elapsed_s'])
        if not math.isfinite(elapsed):
            raise ValueError('non-finite reclaim duration')
        if observed > 0:
            sample = elapsed / (observed / (1 << 20))
            self.cost[anonymous] = max(.001, .75 * self.cost[anonymous] + .25 * sample)
            self.failures[anonymous] = 0
            self.retry_after[anonymous] = 0.
        else:
            # An unproductive request must not make the next one look cheap.
            self.cost[anonymous] = max(self.cost[anonymous], min(1., elapsed))
            self.failures[anonymous] += 1
            if self.failures[anonymous] >= 2:
                self.retry_after[anonymous] = now + min(4., .5 * 2 ** min(3, self.failures[anonymous] - 2))
        return dict(observed_reclaimed_bytes=observed,
                    observed_charge_drop_bytes=net,
                    progress_source='memcg-pgsteal' if stolen is not None else 'net-charge-fallback',
                    progress_ratio=observed / requested if requested else 0.,
                    cost_s_per_mib=self.cost[anonymous],
                    consecutive_no_progress=self.failures[anonymous],
                    retry_after_s=self.retry_after[anonymous])


class Admission:
    def __init__(self, ceiling, headroom):
        self.ceiling, self.headroom = ceiling, headroom
        self.high, self.target = ceiling, None

    def choose(self, *, current, target, active, new_phase=False, recover=False, budgeted=False):
        if active and budgeted:
            # A slowly increasing budget goal is not a new admission burst.
            # Keep the existing lower watermark while usage is above the goal;
            # background/direct reclaim converges without blocking Runtime ACK.
            high = min(self.ceiling, max(target,current+self.headroom) if new_phase or recover else target)
            self.target = target
            if high < current:return None
            self.high = high
            return high
        if not active:
            high = self.ceiling
        elif new_phase or recover or self.target is None or target > self.target:
            high = min(self.ceiling, max(target, current + self.headroom))
        else:
            # Hold the admission watermark across ticks, including allocation
            # bursts. Lower it toward target after bounded reclaim succeeds.
            high = min(self.high, max(target, current + self.headroom))
        self.target = target
        self.high = high
        return high if high >= current else None


class StallGuard:
    """Bound a reclaim episode before it exhausts the whole E2E prefix.

    PSI is measured stall time, not a prediction of the next footprint. The
    episode allowance is at most one tenth of observed prefix slack and one
    tool's reference-duration budget, with 20ms noise tolerance and a limit-dependent
    reaction cap (25ms at epsilon=.05, 100ms at epsilon=.20). These are controller settings, not a second SLO guarantee.
    """
    def __init__(self, ceiling_mib, headroom_mib, limit):
        self.ceiling_mib = ceiling_mib
        self.headroom_mib = headroom_mib
        self.limit = limit
        self.last_time = None
        self.stall_s = 0.
        self.last_recovery = -float('inf')
        self.last_deferred = False

    def begin(self, now):
        self.last_time = now
        self.stall_s = 0.
        self.last_recovery = -float('inf')
        self.last_deferred = False

    def observe(self, now, *, psi_fraction, current_mib, target_mib,
                reference_s, slack_s, last_change,
                recent_swapin_pages=0, recent_swapout_pages=0, prior_reuse_heavy=False):
        self.last_deferred = False
        elapsed = max(0., now - self.last_time) if self.last_time is not None else 0.
        self.last_time = now
        if reference_s is None:
            self.stall_s = 0.
            return None
        self.stall_s += elapsed * max(0., min(1., psi_fraction))
        allowance = max(.02, min(.5 * self.limit, max(0., slack_s) * .1,
                                 reference_s * self.limit))
        if psi_fraction <= .05 or self.stall_s < allowance or now - self.last_recovery < .1:
            return None
        # Page-out stalls do not imply that evicted state is being reused.
        # A one-second tool/reference window and >=4MiB recent writes avoid
        # classifying a few Runtime faults or a short tool as a cold stream.
        # Retain two reference-tool durations of already observed E2E slack;
        # refaults, a tight budget, and file-only pressure still recover fast.
        write_dominated = (recent_swapout_pages >= 1024 and
                           recent_swapin_pages < max(256, int(.25 * recent_swapout_pages)))
        if (not prior_reuse_heavy and reference_s >= 1 and
                slack_s > 2 * reference_s and write_dominated):
            self.last_deferred = True
            self.stall_s = 0.
            return None
        target = min(self.ceiling_mib, max(target_mib * 2, target_mib + 256,
                                          current_mib + self.headroom_mib))
        if target <= target_mib:
            return None
        decision = dict(target_mib=target, stall_s=self.stall_s,
                        episode_allowance_s=allowance, resident_mib=current_mib)
        self.stall_s = 0.
        self.last_recovery = now
        return decision


class ReuseHistory:
    """Completed ACTIVE I/O evidence; WAIT traffic cannot teach reuse history.

    A prior long episode with >=4MiB written and >=max(4MiB,25% of writes)
    read back vetoes write-only deferral for this Sandbox session. This is
    conservative cost feedback, not an assertion about the identity of pages.
    """
    def __init__(self):
        self.start = None
        self.reuse_heavy = False

    def begin(self, swapin_pages, swapout_pages, reference_s):
        self.start = (swapin_pages, swapout_pages, reference_s)

    def end(self, swapin_pages, swapout_pages):
        if self.start is None:
            return None
        read0, write0, reference = self.start
        self.start = None
        read, written = max(0, swapin_pages-read0), max(0, swapout_pages-write0)
        if (reference is not None and reference >= 1 and written >= 1024 and
                read >= max(1024, int(written * .25))):
            self.reuse_heavy = True
        return dict(swapin_pages=read, swapout_pages=written,
                    reference_s=reference, prior_reuse_heavy=self.reuse_heavy)


class CacheLease:
    """Spend observed surplus on releasing clean VM cache during quiet WAIT.

    The prototype owns exactly one Sandbox per VM. This is a Guest-wide cache
    actuator, not cgroup page ranking. Keep a ten-second or two-long-tool reserve
    for a cold resume, using only already completed latency references. Never
    trim in ACTIVE, on unknown references, or twice within one WAIT episode.
    """
    def __init__(self):
        self.longest_reference_s = 0.
        self.last_wait = None

    def completed(self, reference_s):
        if reference_s is not None:
            self.longest_reference_s = max(self.longest_reference_s, reference_s)

    @property
    def reserve_s(self):
        return max(10., 2 * self.longest_reference_s)

    def choose(self, now, *, active, quiet, trusted, wait_since, slack_s, cache_mib):
        available = slack_s - self.reserve_s
        if (active or not quiet or not trusted or now-wait_since < .5 or
                available <= 0 or cache_mib < 16 or self.last_wait == wait_since):
            return None
        self.last_wait = wait_since
        return dict(observed_slack_s=slack_s, resume_reserve_s=self.reserve_s,
                    spendable_s=available, cached_before_mib=cache_mib,
                    wait_elapsed_s=now-wait_since)


class BudgetPlacement:
    """Convert current latency slack to an aggregate SSD eviction allowance.

    Demand is unique resident+swap occupancy, deduplicating resident swapcache.
    The cost model uses PSI per peak swapped MiB, so repeated refaulting raises
    future eviction cost. It does not use a future footprint or page identities.
    Prefix exhaustion delegates to the original fast rescue.
    """
    def __init__(self, ceiling_mib, stop_reuse=True):
        self.ceiling_mib=ceiling_mib
        self.stop_reuse=stop_reuse
        self.cost_s_per_mib=.01
        self.learned_live_mib=0.
        self.reuse_entry_cost_s=None
        self.engaged=False
        self.last_time=None
        self.peak_live_mib=self.peak_swap_mib=self.stall_s=0.
        self.reuse=False

    def begin(self, now, reference_s, trusted):
        self.engaged=bool(trusted and reference_s is not None and reference_s>=1)
        self.last_time=now
        self.started=now; self.reference_s=reference_s
        self.peak_live_mib=self.peak_swap_mib=self.stall_s=0.
        self.reuse=False
        self.phase_budget_s=None
        self.stopped=False
        self.stop_time=None; self.stop_reason=None
        self.first_reuse_time=None

    def choose(self, now, *, current_mib, swap_mib, slack_s, psi_fraction,
               recent_in_pages=0, recent_out_pages=0, prior_reuse=False, swapcache_mib=0.):
        dt=max(0,now-self.last_time) if self.last_time is not None else 0.
        self.last_time=now
        if not self.engaged:return None
        self.stall_s+=dt*min(1,max(0,psi_fraction))
        unique_swap=max(0.,swap_mib-min(swap_mib,swapcache_mib))
        self.peak_live_mib=max(self.peak_live_mib,current_mib+unique_swap)
        self.peak_swap_mib=max(self.peak_swap_mib,unique_swap)
        self.reuse=self.reuse or prior_reuse or recent_in_pages>=max(256,int(recent_out_pages*.25))
        if self.reuse and self.first_reuse_time is None:self.first_reuse_time=now
        live=max(self.peak_live_mib,self.learned_live_mib)
        if self.phase_budget_s is None:self.phase_budget_s=.2*slack_s
        excess=max(0.,now-self.started-self.reference_s)
        prohibit=(self.reuse_entry_cost_s is not None and self.phase_budget_s<self.reuse_entry_cost_s)
        if self.stop_reuse and self.reuse and (self.stopped or prohibit or excess>=self.phase_budget_s):
            if not self.stopped:
                self.stop_time=now
                self.stop_reason="entry-cost-veto" if prohibit else "phase-excess-trigger"
            self.stopped=True
            return dict(target_mib=min(self.ceiling_mib,live+128),quota_mode='reuse-cost-stop',
                live_demand_mib=live,swap_current_mib=swap_mib,swapcache_mib=swapcache_mib,
                unique_swap_mib=unique_swap,phase_stall_s=self.stall_s,
                io_cost_s_per_mib=self.cost_s_per_mib,eviction_budget_s=self.phase_budget_s,
                actual_phase_excess_s=excess,reuse_entry_cost_s=self.reuse_entry_cost_s,
                stop_reason=self.stop_reason,recovery_elapsed_s=now-self.stop_time)
        if slack_s<=.25:return None
        cold=(not self.reuse and slack_s>2*self.reference_s and recent_out_pages>=1024 and
              recent_in_pages<max(256,int(recent_out_pages*.25)))
        if cold:
            target=128.
            mode='write-stream'
        else:
            evict=min(.75*live,.2*slack_s/self.cost_s_per_mib)
            target=max(128.,live-evict)
            mode='reuse-budget'
        return dict(target_mib=min(self.ceiling_mib,target),quota_mode=mode,
                    live_demand_mib=live,swap_current_mib=swap_mib,swapcache_mib=swapcache_mib,
                    unique_swap_mib=unique_swap,
                    phase_stall_s=self.stall_s,io_cost_s_per_mib=self.cost_s_per_mib,
                    eviction_budget_s=.2*slack_s)

    def end(self, now=None):
        if not self.engaged:return None
        excess=max(0.,(self.last_time if now is None else now)-self.started-self.reference_s)
        cost_s=max(self.stall_s,excess)
        self.learned_live_mib=max(self.learned_live_mib,self.peak_live_mib)
        if self.peak_swap_mib>=4 and cost_s>.02:
            observed=cost_s/self.peak_swap_mib
            self.cost_s_per_mib=max(.002,min(.05,.75*self.cost_s_per_mib+.25*observed))
        if self.reuse and self.peak_swap_mib>=64 and excess>=.25:
            self.reuse_entry_cost_s=excess if self.reuse_entry_cost_s is None else min(self.reuse_entry_cost_s,excess)
        self.engaged=False
        return dict(stop_reason=self.stop_reason,
                    first_reuse_elapsed_s=None if self.first_reuse_time is None else self.first_reuse_time-self.started,
                    stop_elapsed_s=None if self.stop_time is None else self.stop_time-self.started,
                    recovery_tail_s=None if self.stop_time is None else (self.last_time if now is None else now)-self.stop_time,
                    reuse_entry_cost_s=self.reuse_entry_cost_s,phase_budget_s=self.phase_budget_s,
                    peak_live_mib=self.peak_live_mib,peak_swap_mib=self.peak_swap_mib,
                    phase_stall_s=self.stall_s,phase_excess_s=excess,io_cost_s_per_mib=self.cost_s_per_mib,
                    learned_live_mib=self.learned_live_mib)
