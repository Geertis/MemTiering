"""SSD page selection using the existing Guest DAMON physical-address backend.

The immutable Sandbox capacity stays in controller.PhaseQuota. DAMOS chooses
cold regions and rechecks each folio's young bit before non-destructive reclaim.
Sandbox file/anonymous pages and the optional VM file-cache diagnostic have
separate filters and counters. No latency/total-duration model changes capacity.
"""
from pathlib import Path
import time
import threading
import errno

MIB = 1 << 20
NAMES = ('sandbox_file', 'sandbox_anon', 'vm_file')


def ram_ranges(text):
    ranges = []
    for line in text.splitlines():
        bounds, _, name = line.strip().partition(' : ')
        if name != 'System RAM':
            continue
        start, end = (int(x, 16) for x in bounds.split('-'))
        start = (start + 4095) & ~4095
        end = (end + 1) & ~4095
        if end > start:
            ranges.append((start, end))
    if not ranges:
        raise RuntimeError('no readable Guest physical System RAM ranges')
    return sorted(ranges)


def actions(mode, active, wait_elapsed, excess, recent_read_pages, last_read_age, fast_wait_cache=False):
    # A local readback cooldown changes only pageout scheduling, never the quota.
    # File-cache reclaim needs no swap write; anonymous reclaim requires both
    # cold-page evidence and an interval without substantial swap readback.
    ready = active or wait_elapsed >= .5
    file_on = ready and excess > 8*MIB
    anon_on = file_on and recent_read_pages < 256 and last_read_age >= 1
    vm_on = mode == 'cold-cache' and not active and wait_elapsed >= (.2 if fast_wait_cache else 1)
    return ('pageout' if file_on else 'stat',
            'pageout' if anon_on else 'stat',
            'pageout' if vm_on else 'stat')


def select_protection(history,target_mib,active):
    # During ACTIVE, native LRU controls the running tool's working set. Keeping
    # the idle Runtime artificially hot can crowd out this set under a tight cap.
    if active:return []
    budget=min(8*MIB,target_mib*MIB//8)
    result=[]
    for _,lo,hi in history.get('priority_blocks',[]):
        if hi-lo<=budget:
            result.append((lo,hi));budget-=hi-lo
    return sorted(result)


class Placement:
    busy_retries=0
    already_off_requests=0
    def __init__(self, group, mode, emit, root=Path('/sys/kernel/mm/damon/admin'), fast_wait_cache=False):
        if mode not in ('cold', 'cold-cache'):
            raise ValueError('unknown SSD placement mode')
        self.group, self.mode, self.emit, self.root = Path(group), mode, emit, root
        self.fast_wait_cache=fast_wait_cache
        self.lock=threading.RLock()
        self.active = False
        self.seen_start = False
        self.wait_since = time.monotonic()
        self.last_read = -float('inf')
        self.last_swapin = 0
        self.last_observe = 0
        self.current_actions = ('stat',) * 3
        self.protected_ranges=[]
        self.priority_applied_ranges=[]
        self.high_excess_since = None
        self.last_applied = 0
        self.previous_applied = 0
        self.last_fallback = 0
        self.k = root/'kdamonds/0'
        self.c = self.k/'contexts/0'
        self.completed_stats={name:{} for name in NAMES}
        self.main_running=False
        self.last_stats_refresh=-float('inf')
        self.cached_raw={name:{} for name in NAMES}
        self.write(root/'kdamonds/nr_kdamonds', 2)
        self.write(self.k/'contexts/nr_contexts', 1)
        self.write(self.c/'operations', 'paddr')
        for name, value in [('sample_us', 5000), ('aggr_us', 100000), ('update_us', 1000000)]:
            self.write(self.c/'monitoring_attrs/intervals'/name, value)
        self.write(self.c/'monitoring_attrs/nr_regions/min', 64)
        self.write(self.c/'monitoring_attrs/nr_regions/max', 1024)
        self.write(self.c/'targets/nr_targets', 1)
        regions = ram_ranges(Path('/proc/iomem').read_text())
        self.ram=regions
        self.write(self.c/'targets/0/regions/nr_regions', len(regions))
        for i, (lo, hi) in enumerate(regions):
            self.write(self.c/f'targets/0/regions/{i}/start', lo)
            self.write(self.c/f'targets/0/regions/{i}/end', hi)
        self.write(self.c/'schemes/nr_schemes', 3)
        relative = '/' + str(self.group.relative_to('/sys/fs/cgroup'))
        for i, name in enumerate(NAMES):
            s = self.c/f'schemes/{i}'
            self.write(s/'action', 'stat')
            self.write(s/'apply_interval_us', 100000)
            for field, lo, hi in [('sz', 4096, 1 << 63),
                                  ('nr_accesses', 0, 1 if i == 0 else 0),
                                  ('age', [10, 20, 2 if fast_wait_cache else 10][i], (1 << 32)-1)]:
                self.write(s/f'access_pattern/{field}/min', lo)
                self.write(s/f'access_pattern/{field}/max', hi)
            # Size counts scanned physical regions, NOT actual reclaimed bytes.
            self.write(s/'quotas/ms', 5)
            self.write(s/'quotas/bytes', 512*MIB)
            self.write(s/'quotas/reset_interval_ms', 100)
            self.write(s/'quotas/weights/sz_permil', 0)
            self.write(s/'quotas/weights/nr_accesses_permil', 500)
            self.write(s/'quotas/weights/age_permil', 500)
            self.write(s/'watermarks/metric', 'none')
            self.write(s/'filters/nr_filters', 3)
            filters = [('memcg', 'Y' if i == 2 else 'N'),
                       ('anon', 'N' if i == 1 else 'Y'), ('young', 'Y')]
            for j, (kind, matching) in enumerate(filters):
                f = s/f'filters/{j}'
                self.write(f/'type', kind)
                self.write(f/'matching', matching)
                if kind == 'memcg':
                    self.write(f/'memcg_path', relative)
        self.write(self.k/'state', 'on')
        self.main_running=True
        # An empty sysfs region specification preserves the live kernel regions
        # on subsequent commits. Replaying initial RAM ranges resets cold ages.
        self.write(self.c/'targets/0/regions/nr_regions', 0)
        self.hot=HotPriority(root/'kdamonds/1',self.group)
        self.emit('placement_init', mode=mode, engine='DAMON-paddr/DAMOS-pageout',
                  ranges=regions, sample_us=5000, aggregate_us=100000,
                  region_limits=[64,1024], schemes=list(NAMES),
                  guest_cache_scope=mode == 'cold-cache', per_folio_young_filter=True,
                  cold_age_ms=[1000,2000,200 if fast_wait_cache else 1000],
                  wait_cache_delay_ms=200 if fast_wait_cache else 1000, scan_budget_bytes=512*MIB,
                  operation_budget_ms_per_100ms=5,
                  process_lifetime_prediction=False, capacity_override=False,
                  native_lru_refault_protection=True,minimum_refaults=2,
                  protection_fraction_of_quota=.125,protection_active=False,
                  sampling_only_on_actual_debt_or_wait_cache=True)

    @staticmethod
    def write(path, value):
        # DAMON sysfs uses mutex_trylock(), so unrelated kdamond callbacks can
        # transiently reject even serialized user-space requests with EBUSY.
        deadline=time.monotonic()+1
        while True:
            try:
                path.write_text(str(value));return
            except OSError as exc:
                # damon_stop() returns EPERM when kdamond already finished.
                # Treat only a verified, already-off stop/stat request as done.
                if path.name=='state' and ((str(value)=='off' and exc.errno in (errno.EPERM,errno.EINVAL)) or
                        (str(value)=='update_schemes_stats' and exc.errno==errno.EINVAL)):
                    try:already_off=path.read_text().strip()=='off'
                    except OSError:already_off=False
                    if already_off:
                        Placement.already_off_requests+=1
                        return
                if exc.errno!=errno.EBUSY or time.monotonic()>=deadline:raise
                Placement.busy_retries+=1;time.sleep(.002)

    def phase(self, active):
        with self.lock:
            self.active = active
            self.seen_start = self.seen_start or active
            if active:
                # Stop WAIT schemes before returning Runtime's start ACK.
                # This is a measured barrier, not a free asynchronous promise.
                self.hot.update([])
                self.priority_applied_ranges=[]
                self.pause_main()
            else:self.wait_since = time.monotonic()

    def observe(self, now, measurement, target_mib, vmstat, history):
        with self.lock:
            return self.observe_locked(now,measurement,target_mib,vmstat,history)

    def observe_locked(self, now, measurement, target_mib, vmstat, history):
        reads = vmstat.get('pswpin', 0)
        recent_read = max(0, reads-self.last_swapin)
        self.last_swapin = reads
        if recent_read >= 256:
            self.last_read = now
        excess = max(0, measurement['fast_bytes']-target_mib*MIB)
        desired = actions(self.mode, self.active, now-self.wait_since, excess,
                          recent_read, now-self.last_read, self.fast_wait_cache)
        if not self.seen_start:desired=('stat',)*3
        protected=select_protection(history,target_mib,self.active)
        changed=protected!=self.protected_ranges
        if protected!=self.priority_applied_ranges:
            self.hot.update(protected)
            self.priority_applied_ranges=list(protected)
        if changed:
            for i in (0,1):
                s=self.c/f'schemes/{i}'
                self.write(s/'filters/nr_filters',3+len(protected))
                for j,(kind,matching) in enumerate([('memcg','N'),('anon','N' if i==1 else 'Y')]):
                    f=s/f'filters/{j}'
                    self.write(f/'type',kind);self.write(f/'matching',matching)
                    if kind=='memcg':self.write(f/'memcg_path','/'+str(self.group.relative_to('/sys/fs/cgroup')))
                for j,(lo,hi) in enumerate(protected,2):
                    f=s/f'filters/{j}'
                    self.write(f/'type','addr');self.write(f/'matching','Y')
                    self.write(f/'addr_start',lo);self.write(f/'addr_end',hi)
                f=s/f'filters/{2+len(protected)}'
                self.write(f/'type','young');self.write(f/'matching','Y')
            self.protected_ranges=list(protected)
        raw_budget=max(4096,min(512*MIB,int(excess)//8)) if any(a=='pageout' for a in desired[:2]) else 4096
        budget=1<<(int(raw_budget).bit_length()-1)
        budget_changed=getattr(self,'last_scan_budget',None)!=budget
        if budget_changed:
            for i in (0,1):self.write(self.c/f'schemes/{i}/quotas/bytes',budget)
            self.last_scan_budget=budget
        configuration_changed=changed or budget_changed or desired!=self.current_actions
        if configuration_changed:
            for i, action in enumerate(desired):
                self.write(self.c/f'schemes/{i}/action', action)
        wanted=any(a=='pageout' for a in desired)
        if not wanted and self.main_running:
            self.pause_main()
        elif wanted and not self.main_running:
            self.write(self.c/'targets/0/regions/nr_regions',len(self.ram))
            for i,(lo,hi) in enumerate(self.ram):
                self.write(self.c/f'targets/0/regions/{i}/start',lo)
                self.write(self.c/f'targets/0/regions/{i}/end',hi)
            self.write(self.k/'state','on');self.main_running=True
            self.write(self.c/'targets/0/regions/nr_regions',0)
            self.cached_raw={name:{} for name in NAMES};self.last_stats_refresh=-float('inf')
        elif wanted and configuration_changed:
            self.write(self.k/'state','commit')
        self.current_actions=desired
        raw=self.raw_stats() if self.main_running else {name:{} for name in NAMES}
        statistics={name:{key:self.completed_stats[name].get(key,0)+raw[name].get(key,0)
            for key in ['qt_exceeds','nr_applied','sz_tried','nr_tried','sz_applied']} for name in NAMES}
        total = sum(statistics[name]['sz_applied'] for name in ['sandbox_file','sandbox_anon'])
        applied_delta = max(0, total-self.previous_applied)
        self.previous_applied = total
        if applied_delta:
            self.last_applied = now
        if excess > 8*MIB:
            if self.high_excess_since is None:
                self.high_excess_since = now
        else:
            self.high_excess_since = None
        # Keep the declared soft capacity accountable if no cold candidate is
        # being found; expose this native fallback rather than silently expand.
        fallback = (self.high_excess_since is not None and
                    now-self.high_excess_since >= 2 and now-self.last_applied >= 1 and
                    now-self.last_fallback >= 1)
        if fallback:
            self.last_fallback = now
        self.last_observe = now
        return dict(event='placement_update', actions=list(desired), statistics=statistics,
                    applied_delta_bytes=applied_delta, excess_bytes=excess,
                    recent_swapin_pages=recent_read, anon_cooldown=now-self.last_read < 1,
                    history=history,protected_physical_ranges=len(protected),
                    selected_protection_bytes=sum(hi-lo for lo,hi in protected),
                    main_monitor_running=self.main_running,per_scope_scan_budget_bytes=budget,
                    transient_sysfs_busy_retries=Placement.busy_retries,
                    already_off_sysfs_requests=Placement.already_off_requests,
                    scheme_stats_refresh_s=1.,scheme_counters_sampled_lower_bound=True,
                    hot_lru_statistics=self.hot.stats(),
                    native_fallback=fallback, capacity_override=False,
                    active=self.active)

    def close(self):
        self.hot.close()
        if self.main_running:self.write(self.k/'state', 'off')

    def raw_stats(self,refresh=True):
        if refresh and time.monotonic()-self.last_stats_refresh>=1:
            self.write(self.k/'state','update_schemes_stats')
            self.cached_raw={name:{p.name:int(p.read_text()) for p in (self.c/f'schemes/{i}/stats').iterdir()}
                    for i,name in enumerate(NAMES)}
            self.last_stats_refresh=time.monotonic()
        return self.cached_raw

    def pause_main(self):
        if self.main_running:
            raw=self.raw_stats(refresh=False)
            for name,values in raw.items():
                for key,value in values.items():
                    self.completed_stats[name][key]=self.completed_stats[name].get(key,0)+value
            self.write(self.k/'state','off');self.main_running=False


class HotPriority:
    """Mark revalidated, observed-refault spans accessed on the native LRU.

The main DAMOS address exclusions alone cannot influence memory.high/direct
reclaim. A second tiny physical context explicitly prioritizes these resident
folios; it neither swaps pages in nor raises an upper capacity target.
"""
    def __init__(self,k,group):
        self.k,self.c,self.running=k,k/'contexts/0',False
        self.write=Placement.write
        self.accumulated_bytes=0
        self.last_bytes=0
        self.last_refresh=-float('inf')
        self.write(k/'contexts/nr_contexts',1)
        self.write(self.c/'operations','paddr')
        for name,value in [('sample_us',10000),('aggr_us',100000),('update_us',1000000)]:
            self.write(self.c/'monitoring_attrs/intervals'/name,value)
        # damon_set_attrs() rejects min_nr_regions < 3 (also for paddr).
        self.write(self.c/'monitoring_attrs/nr_regions/min',3)
        self.write(self.c/'monitoring_attrs/nr_regions/max',128)
        self.write(self.c/'targets/nr_targets',1)
        self.write(self.c/'schemes/nr_schemes',1)
        s=self.c/'schemes/0'
        self.write(s/'action','lru_prio')
        self.write(s/'apply_interval_us',100000)
        for field,lo,hi in [('sz',4096,1<<63),('nr_accesses',0,(1<<32)-1),('age',0,(1<<32)-1)]:
            self.write(s/f'access_pattern/{field}/min',lo)
            self.write(s/f'access_pattern/{field}/max',hi)
        self.write(s/'quotas/ms',1)
        self.write(s/'quotas/bytes',64*MIB)
        self.write(s/'quotas/reset_interval_ms',100)
        self.write(s/'watermarks/metric','none')
        self.write(s/'filters/nr_filters',1)
        self.write(s/'filters/0/type','memcg')
        self.write(s/'filters/0/matching','N')
        self.write(s/'filters/0/memcg_path','/'+str(group.relative_to('/sys/fs/cgroup')))

    def update(self,ranges):
        if not ranges:
            if self.running:
                self.stats(refresh=False);self.accumulated_bytes+=self.last_bytes
                self.write(self.k/'state','off');self.running=False;self.last_bytes=0
            return
        self.write(self.c/'targets/0/regions/nr_regions',len(ranges))
        for i,(lo,hi) in enumerate(ranges):
            self.write(self.c/f'targets/0/regions/{i}/start',lo)
            self.write(self.c/f'targets/0/regions/{i}/end',hi)
        self.write(self.k/'state','commit' if self.running else 'on')
        if not self.running:self.last_refresh=-float('inf')
        self.running=True

    def stats(self,refresh=True):
        if self.running and refresh and time.monotonic()-self.last_refresh>=1:
            self.write(self.k/'state','update_schemes_stats')
            self.last_bytes=int((self.c/'schemes/0/stats/sz_applied').read_text())
            self.last_refresh=time.monotonic()
        return dict(mark_accessed_bytes=self.accumulated_bytes+self.last_bytes,
                    running=self.running,swapin=False,capacity_override=False)

    def close(self):
        if self.running:self.write(self.k/'state','off');self.running=False
