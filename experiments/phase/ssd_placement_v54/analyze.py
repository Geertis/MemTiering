#!/usr/bin/env python3
"""Audited physical Host RSS and common Sandbox page-scope measurements."""
import importlib.util
import json
from pathlib import Path
import statistics as st

H=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('cold_analysis',H.parent/'cold_media/analyze.py')
base=importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
MIB=1<<20


def read(path):
    return json.loads(Path(path).read_text())


def page_mib(guest, medium):
    if medium=='ssd':
        if 'kernel' not in guest['memory_stat']:
            raise ValueError('missing kernel charge counter')
        return max(0,guest['memory_current']-guest['memory_stat']['kernel']-
                   guest['memory_stat'].get('sock',0))/MIB
    nodes={}
    for line in guest['memory_numa_stat'].splitlines():
        fields=line.split()
        if not fields or fields[0] not in ('anon','file','active_anon','inactive_anon',
                                          'active_file','inactive_file','unevictable'):
            continue
        for item in fields[1:]:
            key,value=item.split('=')
            nodes.setdefault(key,{})[fields[0]]=int(value)
    def resident(v):
        return max(v.get('anon',0)+v.get('file',0),sum(v.get(k,0) for k in
            ['active_anon','inactive_anon','active_file','inactive_file','unevictable']))
    return resident(nodes.get('N0',{}))/MIB


def audit(root,case,job):
    plan=dict(cases=[case],speed=1,host_cpus=list(range(4*job['lane'],4*job['lane']+4)))
    folder=root/job['name']
    if not (folder/'guest-out/result.json').exists():
        code=(folder/'ssh-status').read_text().strip() if (folder/'ssh-status').exists() else None
        return dict(**job,complete=False,valid=False,censored=code in ['124','137'],
            reasons=['external replay deadline exceeded' if code in ['124','137'] else 'missing result'],
            runner_exit=read(root/(job['name']+'.receipt.json'))['exit_code'])
    row=base.trial(root,plan,job)
    row['censored']=False
    # The archived audit predates phase-varying total-page quotas and fine reporting.
    # Replace only those two obsolete assumptions; preserve its other physical checks.
    row['reasons']=[r for r in row['reasons'] if r not in
                    ['SSD memory.high mismatch','reporting order drift']]
    data=read(folder/'guest-out/result.json')
    start,end=data['e2e_started_monotonic_s'],data['e2e_finished_monotonic_s']
    samples=[json.loads(line) for line in (folder/'host-capacity.jsonl').read_text().splitlines()
             if '"event": "sample"' in line]
    area=covered=0.
    selected=[]
    for left,right in zip(samples,samples[1:]):
        lo,hi=[s['guest']['guest_monotonic_ns']/1e9 for s in (left,right)]
        dt=max(0,min(end,hi)-max(start,lo))
        if not dt or hi-lo>1 or not left['guest_fresh'] or not right['guest_fresh']:
            continue
        value=page_mib(left['guest'],job['config']['media'])
        area+=value*dt;covered+=dt;selected.append((left,value,dt))
    row.update(managed_mean_mib=area/covered if covered else None,
        managed_peak_mib=max((v for _,v,_ in selected),default=0),
        managed_integral_gib_s=area/1024)
    allowed={0,5} if job['config']['phase'] else {5}
    if any(s['guest']['page_reporting_order'] not in allowed for s in samples):
        row['reasons'].append('unexpected reporting order')
    if job['config']['phase']:
        events=[json.loads(line) for line in (folder/'guest-out/controller.jsonl').read_text().splitlines()]
        init=next((e for e in events if e['event']=='capacity_init'),{})
        if init.get('quota')!=job['quota'] or init.get('upper_uses_latency') is not False or init.get('upper_uses_medium') is not False:
            row['reasons'].append('upper capacity policy mismatch')
        if init.get('accounting_version')!='resident-lru-kmem-sock-v1' or init.get('compensated_fields')!=['kernel','sock']:
            row['reasons'].append('resident accounting version mismatch')
        points=[e for e in events if e['event']=='capacity_tick' and start<=e['monotonic_ns']/1e9<=end]
        if not points:
            row['reasons'].append('no capacity samples')
        for e in events:
            if e['event'] in ['capacity_phase','capacity_tick']:
                expected=job['quota']['active_mib' if e['active'] else 'wait_mib']
                if e['target_mib']!=expected:
                    row['reasons'].append('quota changed outside phase setting')
            if e['event']=='capacity_phase' and e['active'] and e['reporting_order']!=5:
                row['reasons'].append('ACTIVE reporting barrier mismatch')
        row['quota_overage_mean_mib']=st.mean(max(0,e['measurement']['fast_bytes']/MIB-e['target_mib']) for e in points) if points else None
        row['quota_attainment_fraction']=st.mean(e['measurement']['fast_bytes']/MIB<=e['target_mib']+8 for e in points) if points else None
        for active,label in [(True,'active'),(False,'wait')]:
            group=[e for e in points if e['active']==active]
            row[label+'_managed_mean_mib']=st.mean(e['measurement']['fast_bytes']/MIB for e in group) if group else None
            row[label+'_attainment_fraction']=st.mean(e['measurement']['fast_bytes']/MIB<=e['target_mib']+8 for e in group) if group else None
        if any(e.get('actuator_errors',0) for e in points):
            row['reasons'].append('lower actuator errors')
        mode=job.get('placement_mode','native')
        if init.get('placement_mode')!=mode:
            row['reasons'].append('placement mode mismatch')
        if mode!='native':
            setup=next((e for e in events if e['event']=='placement_init'),None)
            updates=[e for e in events if e['event']=='placement_update' and start<=e['monotonic_ns']/1e9<=end]
            if not setup or not updates:row['reasons'].append('missing page placement evidence')
            if any(e.get('capacity_override') is not False for e in updates):
                row['reasons'].append('lower placement changed capacity')
            row['placement_updates']=len(updates)
            row['native_fallback_operations']=sum(e['native_fallback'] for e in updates)
            row['anon_cooldown_updates']=sum(e['anon_cooldown'] for e in updates)
            row['damos_reclaimed_bytes']={name:max((e['statistics'][name]['sz_applied'] for e in updates),default=0)
                for name in ['sandbox_file','sandbox_anon','vm_file']}
        if job['config']['media']=='ssd':
            if any(e['measurement']['fast_bytes']!=max(0,e['measurement']['charge_bytes']-
                   e['measurement']['kernel_charge_bytes']-e['measurement']['socket_charge_bytes']) for e in points):
                row['reasons'].append('SSD resident user charge accounting mismatch')
            gaps=[abs(e['measurement']['charge_lru_gap_bytes'])/MIB for e in points]
            row['charge_lru_gap_median_mib']=st.median(gaps) if gaps else None
            row['charge_lru_gap_max_mib']=max(gaps,default=0)
        row['reclaim_operations']=sum(e['event']=='capacity_reclaim' for e in events)
        row['reclaim_eagain']=sum(e['event']=='capacity_reclaim' and e.get('error')==11 for e in events)
    row['reasons']=sorted(set(row['reasons']));row['valid']=not row['reasons']
    return row
