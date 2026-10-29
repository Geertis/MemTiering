"""Latency-budget feedback using only current events and completed references.

References are prior completed DRAM tool timings, never memory traces, future
waits, or the test task's final duration. Missing references stop latency-based
shrinkage; a cold-start counterfactual slowdown is not observable online.
"""
from dataclasses import dataclass
import math


@dataclass
class BudgetFeedback:
    limit: float
    ceiling_mib: float
    initial_mib: float
    floor_mib: float = 0
    deadband: float = .1
    horizon_s: float = 0.

    def __post_init__(self):
        if not (0 < self.limit < 1 and 0 <= self.floor_mib <= self.initial_mib <= self.ceiling_mib):
            raise ValueError('invalid latency/physical bounds')
        if not math.isfinite(self.horizon_s) or self.horizon_s<0:
            raise ValueError('invalid historical task horizon')
        self.target_mib = self.initial_mib
        self.origin = None
        self.actual_service = self.reference_service = 0.
        self.seen = set()
        self.current = None
        self.inflight = False
        self.reference = None
        self.nonservice = 0.
        self.unknown = 0
        self.trusted = True
        self.last_change = -math.inf
        self.pressure = 0.
        self.reason = 'initial'

    def completed(self, item, references):
        if not item or item['id'] in self.seen:
            return
        service = float(item['service_s'])
        if not math.isfinite(service) or service < 0:
            raise ValueError('invalid completed service')
        self.seen.add(item['id'])
        self.actual_service += service
        ref = references.get(item['signature'])
        if ref is None:
            self.unknown += 1
            self.trusted = False
        else:
            self.reference_service += float(ref)

    def start(self, message, references, now):
        self.completed(message.get('completed'), references)
        origin = float(message['e2e_started_s'])
        started = float(message['service_started_s'])
        if not math.isfinite(origin + started) or not origin <= started <= now + .1:
            raise ValueError('invalid Runtime clock')
        if self.origin is None:
            self.origin = origin
        if abs(self.origin - origin) > .001:
            raise ValueError('E2E origin changed')
        self.nonservice = max(0., started - origin - self.actual_service)
        self.current = dict(message)
        self.inflight = True
        self.reference = references.get(message['signature'])
        if self.reference is None or not self.trusted:
            self.target_mib = self.ceiling_mib
            self.reason = 'unknown-reference-conservative'
        else:
            error = self.actual_service - self.reference_service
            allowed = self.limit * self.budget_base(self.nonservice + self.reference_service)
            if self.seen and now - self.last_change >= .25:
                if error > allowed * (1 - self.deadband):
                    self.grow(now, 'latency-debt')
                elif error < allowed * (1 - 2 * self.deadband):
                    self.target_mib = max(self.floor_mib, self.target_mib * .75 - 16)
                    self.last_change, self.reason = now, 'latency-slack-explore'
        return self.target_mib

    def grow(self, now, reason):
        if now - self.last_change < .25:
            return
        self.target_mib = min(self.ceiling_mib, max(self.target_mib * 1.5, self.target_mib + 128))
        self.last_change, self.reason = now, reason

    def tick(self, now, *, active, psi_fraction=0., pressure=0.):
        self.pressure = max(0., min(1., pressure))
        self.inflight = active
        if not active and self.origin is not None:
            # Only elapsed WAIT is credited. No future gap/end time is read.
            self.nonservice = max(self.nonservice, now - self.origin - self.actual_service)
        if active:
            # Reclaim stalls consume the same latency budget as tool work.
            # Growing on *any* PSI signal made 5/10/15/20% behave alike.
            if psi_fraction > .05 and (not self.trusted or self.reference is None or
                                       self.slack_s(now) <= .1):
                self.grow(now, 'direct-reclaim-stall')
            elif self.current and self.reference is not None and self.trusted:
                elapsed = max(0., now - self.current['service_started_s'])
                debt = self.actual_service - self.reference_service + max(0., elapsed - self.reference)
                allowed = self.limit * self.budget_base(self.nonservice + self.reference_service + self.reference)
                if debt > allowed * (1 - self.deadband):
                    self.grow(now, 'inflight-latency-debt')
        elif pressure > .5 and now - self.last_change >= 1:
            self.target_mib = max(self.floor_mib, self.target_mib * .85 - 16)
            self.last_change, self.reason = now, 'host-pressure-wait-reclaim'
        elif (self.origin is not None and self.trusted and self.reference is not None and
              now - self.last_change >= 1):
            debt = self.actual_service - self.reference_service
            allowed = self.limit * self.budget_base(self.nonservice + self.reference_service)
            if allowed > 0 and debt < allowed * (1 - 2 * self.deadband):
                self.target_mib = max(self.floor_mib, self.target_mib * .85 - 16)
                self.last_change, self.reason = now, 'elapsed-wait-slack-explore'
        return self.target_mib

    def budget_base(self, observed_prefix):
        # Historical forecast only; current/future trace timing is never read.
        return max(.001, observed_prefix, self.horizon_s if self.trusted else 0.)

    def slack_s(self, now):
        if not self.trusted or self.reference is None or not self.current:
            return 0.
        elapsed = max(0., now - self.current['service_started_s']) if self.inflight else 0.
        debt = self.actual_service - self.reference_service + max(0., elapsed - self.reference)
        allowed = self.limit * self.budget_base(self.nonservice + self.reference_service + (self.reference if self.inflight else 0.))
        return max(0., allowed * (1 - self.deadband) - debt)

    def status(self, now):
        ref_prefix = self.nonservice + self.reference_service
        return dict(target_mib=self.target_mib, physical_ceiling_mib=self.ceiling_mib,
                    estimated_slack_s=self.slack_s(now),
                    reason=self.reason, completed_tools=len(self.seen),
                    reference_known=self.trusted and self.reference is not None,
                    unknown_completed=self.unknown, observed_nonservice_s=self.nonservice,
                    actual_completed_service_s=self.actual_service,
                    reference_completed_service_s=self.reference_service,
                    estimated_completed_excess_s=self.actual_service - self.reference_service if self.trusted else None,
                    estimated_prefix_slowdown=(self.actual_service - self.reference_service) / ref_prefix
                    if self.trusted and ref_prefix > 0 else None,
                    host_pressure=self.pressure, limit=self.limit,
                    historical_horizon_s=self.horizon_s,
                    budget_base_s=self.budget_base(ref_prefix+(self.reference or 0) if self.inflight else ref_prefix))


class PhysicalLedger:
    """Shared reservation arithmetic; a lower target alone never frees credit.

    A caller must supply fresh whole-VMM physical measurements, overhead and
    reservations. This class alone is not a Host-enforced hard limit.
    """
    def __init__(self, budget):
        if budget < 0:
            raise ValueError('negative budget')
        self.budget = budget

    def grant(self, targets, resident, requested):
        owned = {k: max(targets.get(k, 0), resident.get(k, 0)) for k in resident}
        committed = sum(owned.values())
        granted = dict(targets)
        for key, desired in requested.items():
            if key not in owned or desired < 0:
                raise ValueError('unknown VMM or negative target')
            value = min(desired, owned[key] + max(0, self.budget - committed))
            committed += max(value, resident[key]) - owned[key]
            owned[key] = max(value, resident[key])
            granted[key] = value
        assert sum(max(granted.get(k, 0), v) for k, v in resident.items()) <= max(self.budget, sum(max(targets.get(k, 0), v) for k, v in resident.items()))
        return granted
