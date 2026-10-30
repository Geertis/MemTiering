#!/usr/bin/env python3
"""Capacity-first experiments: phase quotas are immutable and medium independent."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import errno
import json
from pathlib import Path
import selectors
import socket
import time

MIB = 1 << 20
PAGE = 4096
LRU_FIELDS = ('active_anon','inactive_anon','active_file','inactive_file','unevictable')


@dataclass(frozen=True)
class PhaseQuota:
    active_mib: int
    wait_mib: int

    def __post_init__(self):
        if (type(self.active_mib) is not int or type(self.wait_mib) is not int or
                not 0 <= self.wait_mib <= self.active_mib <= 6144):
            raise ValueError('invalid capacity settings')

    def target(self, active):
        return self.active_mib if active else self.wait_mib


def pairs(path):
    try:
        return {k:int(v) for k,v in (s.split() for s in path.read_text().splitlines())}
    except FileNotFoundError:
        return {}


def numa(path):
    result = {}
    for line in path.read_text().splitlines():
        fields = line.split()
        if not fields or fields[0] not in ('anon','file','swapcached',*LRU_FIELDS):
            continue
        for field in fields[1:]:
            key,value = field.split('=')
            result.setdefault(int(key[1:]),{})[fields[0]] = int(value)
    return result


def residency(nodes, fast_node, medium):
    # Disjoint LRUs include unmapped resident SwapCache. Do not add swapcached
    # to mapped anon: mapped SwapCache pages overlap the two counters.
    amounts={n:max(v.get('anon',0)+v.get('file',0),
                   sum(v.get(k,0) for k in LRU_FIELDS)) for n,v in nodes.items()}
    resident=sum(amounts.values())
    fast=amounts.get(fast_node,0) if medium=='cxl' else resident
    return fast, max(0,resident-fast)


def user_charge(current, stats):
    if 'kernel' not in stats:
        raise ValueError('kernel charge counter required; never compensate missing user pages')
    # MEMCG_SOCK is charged independently of MEMCG_KMEM in this kernel.
    return max(0,current-stats['kernel']-stats.get('sock',0))


class Controller:
    def __init__(self,args,log):
        self.a,self.log,self.seq = args,log,0
        self.group=Path(json.loads(args.config.read_text())[0]['cgroup'])
        settings=json.loads(args.reference_profile.read_text())
        if set(settings) != {'capacity','placement'}:
            raise ValueError('capacity-only settings required; no latency/task-time reference accepted')
        placement=settings['placement']
        if set(placement) not in ({'mode'}, {'mode','fast_wait_cache'}) or placement['mode'] not in ('native','cold','cold-cache'):
            raise ValueError('unknown lower placement setting')
        self.placement_mode=placement['mode']
        self.fast_wait_cache=placement.get('fast_wait_cache',False)
        if type(self.fast_wait_cache) is not bool:
            raise ValueError('fast_wait_cache must be boolean')
        self.lower=None
        self.page_history=None
        self.last_history=0.
        cap=settings['capacity']
        if set(cap) != {'active_mib','wait_mib'}:
            raise ValueError('unknown capacity setting')
        self.policy=PhaseQuota(**cap)
        self.active=False
        self.phase_id=None
        self.kind=0
        self.wait_since=time.monotonic()
        self.started=None
        self.pool=ThreadPoolExecutor(max_workers=1)
        self.future=None
        self.applied_high=None
        self.last_reclaim=0.
        self.interface=None
        self.errors=0
        from cxl_controller import ReportingGate
        self.reporting=ReportingGate(Path('/sys/module/page_reporting/parameters/page_reporting_order'),
            args.active_reporting_order,args.wait_reporting_order,args.wait_reporting_delay_ms,
            barrier_path=Path('/sys/module/page_reporting/parameters/page_reporting_barrier'))
        (self.group/'memory.max').write_text('max')
        (self.group/'memory.high').write_text('max')
        if args.cold_media=='cxl':
            sysfs=Path('/sys/kernel/mm/demeter/sandboxes')
            (sysfs/'nr_sandboxes').write_text('1')
            (sysfs/'budget_pages').write_text(str(6144*MIB//PAGE))
            self.interface=sysfs/'0'
            (self.interface/'cgroup').write_text('/'+str(self.group.relative_to('/sys/fs/cgroup')))
            self.apply_cxl(new_phase=True)
        else:
            from history import PageHistory
            self.page_history=PageHistory(self.group)
            (self.group/'memory.swap.max').write_text(str(16*1024*MIB))
            Path('/proc/sys/vm/swappiness').write_text('60')
            if self.placement_mode!='native':
                from placement import Placement
                self.lower=Placement(self.group,self.placement_mode,self.emit,fast_wait_cache=self.fast_wait_cache)
        self.emit('capacity_init',backend=args.cold_media,quota=cap,
                  upper_uses_latency=False,upper_uses_medium=False,
                  accounting_version='resident-lru-kmem-sock-v1',compensated_fields=['kernel','sock'],
                  quota_scope='Sandbox resident user pages, including resident SwapCache, in physical DRAM',
                  whole_vm_cap=False,placement_mode=self.placement_mode,fast_wait_cache=self.fast_wait_cache,settings=vars(args))

    @property
    def target_mib(self):
        target = self.policy.target(self.active)
        path = Path('/tmp/host-pressure.json')
        if path.exists():
            try:
                control = json.loads(path.read_text())
                grant = control.get('global_quota_mib')
                if type(grant) is int and 0 <= grant <= 6144:
                    self.host_grant_mib = grant
                    self.host_grant_seq = control['global_quota_seq']
            except (ValueError, KeyError, OSError):
                pass
        return min(target, getattr(self, 'host_grant_mib', target))

    def emit(self,event,**kw):
        self.seq+=1
        self.log.write(json.dumps(dict(event=event,event_seq=self.seq,
            monotonic_ns=time.monotonic_ns(),controller_cpu_ns=time.process_time_ns(),**kw),default=str)+'\n')
        self.log.flush()

    def measure(self):
        nodes=numa(self.group/'memory.numa_stat')
        fast,slow=residency(nodes,self.a.fast_node,self.a.cold_media)
        current=int((self.group/'memory.current').read_text())
        stats=pairs(self.group/'memory.stat')
        charged_user=user_charge(current,stats)
        lru_bytes=fast+slow
        if self.a.cold_media=='ssd':
            # Every Guest node is on Host DRAM: this includes all resident
            # SwapCache, even while pages are temporarily off an LRU.
            fast,slow=charged_user,0
        return dict(fast_bytes=fast,slow_bytes=slow,charge_bytes=current,
                    overhead_charge_bytes=stats['kernel']+stats.get('sock',0),kernel_charge_bytes=stats['kernel'],
                    socket_charge_bytes=stats.get('sock',0),
                    user_charge_bytes=charged_user,lru_user_bytes=lru_bytes,
                    mapped_anon_file_bytes=sum(v.get('anon',0)+v.get('file',0) for v in nodes.values()),
                    swapcached_bytes=stats.get('swapcached',0),
                    charge_lru_gap_bytes=charged_user-lru_bytes,nodes=nodes,
                    swap_bytes=int((self.group/'memory.swap.current').read_text()))

    def apply_cxl(self,new_phase=False):
        before=int((self.interface/'allocation_snapshot').read_text())
        m=self.measure()
        (self.interface/'residency').write_text(f"{m['fast_bytes']//PAGE} {m['slow_bytes']//PAGE} {before}\n")
        target=self.target_mib*MIB//PAGE
        if int((self.interface/'quota_pages').read_text())!=target:
            (self.interface/'quota_pages').write_text(str(target))
        if new_phase:
            # A constant lower-level horizon; no workload-duration prediction.
            (self.interface/'phase').write_text(f'{int(self.active)} {self.kind} 1000\n')
        return m

    def drain(self):
        if self.future is None or not self.future.done():
            return
        result=self.future.result()
        self.future=None
        if result['event']=='capacity_high_applied' and result.get('error') is None:
            self.applied_high=result['high_bytes']
        self.emit(**result)
        if result.get('error') not in (None,errno.EAGAIN):
            self.errors+=1

    def io_job(self,high,amount,phase_id):
        started=time.monotonic()
        error=None
        before=int((self.group/'memory.current').read_text())
        stat0=pairs(self.group/'memory.stat')
        try:
            if high is not None:
                (self.group/'memory.high').write_text(str(high))
            elif amount:
                (self.group/'memory.reclaim').write_text(str(amount))
        except OSError as exc:
            error=exc.errno
        stat1=pairs(self.group/'memory.stat')
        return dict(event='capacity_high_applied' if high is not None else 'capacity_reclaim',
            phase_id=phase_id,high_bytes=high,requested_bytes=amount,
            before_bytes=before,after_bytes=int((self.group/'memory.current').read_text()),
            pgsteal_pages=max(0,stat1.get('pgsteal',0)-stat0.get('pgsteal',0)),
            elapsed_s=time.monotonic()-started,error=error)

    def schedule_ssd(self,now,m,new_phase=False):
        if self.future is not None:
            return
        # Compensate only explicit KMEM and socket charges. current-(anon+file) also
        # contains unmapped resident SwapCache and must never raise this limit.
        # The extra 4MiB is declared actuator tolerance, not an upper quota change.
        high=self.target_mib*MIB+m['overhead_charge_bytes']+4*MIB
        if self.applied_high is None or new_phase or abs(high-self.applied_high)>=4*MIB:
            self.future=self.pool.submit(self.io_job,high,0,self.phase_id)
        elif self.lower is not None and now-self.last_reclaim>=.2:
            self.future=self.pool.submit(self.placement_job,m,self.target_mib,self.phase_id)
            self.last_reclaim=now
        elif self.lower is None and self.page_history is not None and now-self.last_history>=.5:
            self.future=self.pool.submit(self.history_job,self.phase_id)
            self.last_history=now
        elif (self.lower is None and m['fast_bytes']>self.target_mib*MIB+MIB and
              now-self.last_reclaim>=.1 and (self.active or now-self.wait_since>=.5)):
            amount=min(self.a.chunk_mib*MIB,m['fast_bytes']-self.target_mib*MIB)
            self.future=self.pool.submit(self.io_job,None,amount,self.phase_id)
            self.last_reclaim=now

    def placement_job(self,m,target_mib,phase_id):
        started=time.monotonic()
        history=self.page_history.sample(started)
        m=self.measure()
        target_mib=self.target_mib
        result=self.lower.observe(started,m,target_mib,pairs(Path('/proc/vmstat')),history)
        if result['native_fallback']:
            reclaim=self.io_job(None,min(self.a.chunk_mib*MIB,result['excess_bytes']),phase_id)
            result['fallback_result']=reclaim
            result['error']=reclaim['error']
        result.update(phase_id=phase_id,elapsed_s=time.monotonic()-started)
        return result

    def history_job(self,phase_id):
        return dict(event='page_history',phase_id=phase_id,history=self.page_history.sample())

    def event(self,message):
        if str(message.get('sandbox'))!='0':
            raise ValueError('one Sandbox per VM')
        action=message['event']
        if action not in ('start','end','fallback'):
            raise ValueError('unknown phase event')
        self.drain()
        self.active=action=='start'
        if self.lower is not None:
            self.lower.phase(self.active)
        self.kind=int(message.get('kind',0))
        if self.active:
            self.phase_id=message['id']
            self.started=time.monotonic()
            self.reporting.restore()
        else:
            self.wait_since=time.monotonic()
        m=self.apply_cxl(new_phase=True) if self.interface else self.measure()
        if not self.interface:
            self.schedule_ssd(time.monotonic(),m,new_phase=True)
        self.emit('capacity_phase',action=action,active=self.active,phase_id=self.phase_id,
                  kind=self.kind,target_mib=self.target_mib,measurement=m,
                  reporting_order=self.reporting.current)
        preferred=(self.a.fast_node if not self.interface or m['fast_bytes']<self.target_mib*MIB
                   else self.a.slow_node)
        return dict(preferred_node=preferred,target_mib=self.target_mib,
                    backend='fixed-phase-'+self.a.cold_media)

    def tick(self):
        self.drain()
        now=time.monotonic()
        self.reporting.update(not self.active,time.monotonic_ns())
        m=self.apply_cxl() if self.interface else self.measure()
        if not self.interface:
            self.schedule_ssd(now,m)
        self.emit('capacity_tick',active=self.active,phase_id=self.phase_id,
            target_mib=self.target_mib,global_grant_mib=getattr(self,'host_grant_mib',None),global_grant_seq=getattr(self,'host_grant_seq',None),measurement=m,actuator_busy=self.future is not None,
            applied_high_bytes=self.applied_high,reporting_order=self.reporting.current,
            actual_over_target_bytes=max(0,m['fast_bytes']-self.target_mib*MIB),
            actuator_errors=self.errors)


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--cold-media',choices=['cxl','ssd'],required=True)
    for name in ['config','socket','log','reference-profile']:
        p.add_argument('--'+name,type=Path,required=True)
    for name,default in [('budget-mib',6144),('startup-headroom-mib',0),('fast-node',0),
                         ('slow-node',1),('wait-mib',16),('chunk-mib',32),
                         ('active-headroom-mib',0),('initial-mib',0),('wait-reporting-order',0),
                         ('active-reporting-order',5),('wait-reporting-delay-ms',500)]:
        p.add_argument('--'+name,type=int,default=default)
    # Compatibility with the archived launcher; none of these controls quotas.
    for name,default in [('latency-limit',.2),('file-wait-s',.5),('anon-wait-s',.5)]:
        p.add_argument('--'+name,type=float,default=default)
    p.add_argument('--barrier-reporting-start',action='store_true')
    a=p.parse_args()
    if a.socket.exists() or len(json.loads(a.config.read_text()))!=1:
        p.error('existing socket or non-single-Sandbox configuration')
    with a.log.open('x') as log,socket.socket(socket.AF_UNIX) as server,selectors.DefaultSelector() as selector:
        c=Controller(a,log)
        server.bind(str(a.socket));server.listen(16);selector.register(server,selectors.EVENT_READ)
        deadline=time.monotonic()+.1
        try:
            while True:
                for _,_ in selector.select(max(0,deadline-time.monotonic())):
                    with server.accept()[0] as connection:
                        connection.settimeout(2)
                        try:
                            response=c.event(json.loads(connection.makefile('rb').readline(16384)))
                        except Exception as exc:
                            c.emit('error',error=repr(exc));response={'error':repr(exc)}
                        connection.sendall(json.dumps(response).encode()+b'\n')
                if time.monotonic()>=deadline:
                    c.tick();deadline=time.monotonic()+.1
        finally:
            c.pool.shutdown(wait=True)
            if c.lower is not None:c.lower.close()
            c.reporting.close();a.socket.unlink(missing_ok=True)


if __name__=='__main__':main()
