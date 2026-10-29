#!/usr/bin/env python3
"""Audit physical endpoints, comparable work and whole-VMM/time tradeoffs."""
import argparse
import csv
import hashlib
import json
import math
import random
import re
import statistics as st
from collections import defaultdict
from pathlib import Path

M = 1 << 20
SELECTED = {'numba-9636': 9, 'dask-5510': 76, 'pydicom-2065': 32,
            'xonsh-local': 21, 'xonsh-haiku': 8, 'streamlink-3485': 26, 'github3-673': 54}
METRICS = ['ddr_peak_mib', 'ddr_mean_mib', 'cxl_peak_mib', 'cxl_mean_mib',
           'service_s', 'execution_s', 'selected_service_s', 'selected_execution_s']
read = lambda p: json.loads(p.read_text())
sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()

def signature(data):
    steps = [s for s in data['steps'] if not s.get('skipped')]
    return dict(statuses=data['status_vector'], head=data['original_git_head'],
        diff=data['git_diff_sha256'], git_status=data['git_status_sha256'],
        outputs={str(s['source_index']): s.get('stdout_sha256') for s in steps if s['tool'] in ['Read','Grep','Glob']},
        tests={str(s['source_index']): re.findall(r'\b(\d+) (passed|failed|errors?|skipped|deselected|xfailed|xpassed)\b',
            (s.get('stdout_tail') or '')+'\n'+(s.get('stderr_tail') or '')) for s in steps if s['tool']=='Bash'})

def failures(s):
    out = (s.get('stdout_tail') or '') + '\n' + (s.get('stderr_tail') or '')
    flags = []
    for pattern, label in [(r'Temporary failure in name resolution', 'dns'),
        (r'collected 0 items|ERROR collecting|errors? during collection', 'collection'),
        (r'Read-only file system|Permission denied', 'readonly/permission'),
        (r'pytest\.warns\(None\)|exceptions must be derived from Warning', 'warning API'),
        (r'\b[1-9]\d* (?:failed|errors?)\b', 'test failure/error')]:
        if re.search(pattern, out, re.I): flags.append(label)
    return flags

def trial(root, job, case):
    row = dict(cohort=root.name, **job, complete=False, valid=False, reasons=[])
    out = root/job['name']
    receipt = root/(job['name']+'.receipt.json')
    if not receipt.exists(): return row
    row['runner_exit'] = read(receipt)['exit_code']
    if row['runner_exit']: row['reasons'].append('runner failed')
    if not (out/'guest-out/result.json').exists(): return row
    d, m = read(out/'guest-out/result.json'), read(out/'manifest.json')
    cfg = job['config']
    phase = cfg['mode']=='ours'
    boot_log = (out/'vm.log').read_text(errors='replace')
    for name, expected in m['files'].items():
        archived = out/'protocol-inputs'/Path(name).name
        source = archived if archived.exists() else Path(name)
        if not source.exists() or sha(source)!=expected: row['reasons'].append('input hash '+name)
    if sorted(s['source_index'] for s in d['steps'])!=list(range(len(read(Path(case['trace'])/'tool_calls.json')))):
        row['reasons'].append('partial source coverage')
    if d['phase_runtime_errors']: row['reasons'].append('runtime errors')
    if d['phase_aware']!=phase: row['reasons'].append('controller mismatch')
    preflight = (out/'policy-preflight.log').read_text()
    if bool(re.search(r'^demeter_placement\s',preflight,re.M))!=phase:
        row['reasons'].append('module mismatch')
    policy = d.get('initial_memory_policy',{})
    if not phase and (policy.get('mode'),policy.get('mask'))!=(2,1<<cfg.get('bind_node',0)):
        row['reasons'].append('endpoint tool binding mismatch')
    kernel = (out/'dmesg.log').read_text(errors='replace')
    if any(x in kernel for x in ['BUG:', 'Oops:', 'Kernel panic', 'general protection fault', 'VM_FAULT_OOM leaked']):
        row['reasons'].append('kernel fault')
    if re.search(r'Out of memory: Killed process|oom-kill:|Memory cgroup out of memory',kernel):
        row['reasons'].append('Guest kernel OOM')
    resources = d['resource_samples']
    if not resources: return row
    start, end = min(s['monotonic_s'] for s in resources), max(s['monotonic_s'] for s in resources)
    if any(s.get('memory_events',{}).get('oom',0) or s.get('memory_events',{}).get('oom_kill',0) for s in resources):
        row['reasons'].append('OOM')
    records = [json.loads(x) for x in (out/'host-capacity.jsonl').read_text().splitlines()]
    init = next((s for s in records if s['event']=='init'),None)
    final = next((s for s in reversed(records) if s['event']=='end'),None)
    if not init or not final or final.get('errors'): row['reasons'].append('observer incomplete/error')
    samples = [s for s in records if s['event']=='sample']
    selected = [s for s in samples if start<=s['guest']['guest_monotonic_ns']/1e9<=end]
    if init:
        cpu=init['info']['config']['cpus']
        if cpu['boot_vcpus']!=4 or {x['vcpu'] for x in cpu['affinity']}!={0,1,2,3} or any(x['host_cpus']!=[0,1,2,3] for x in cpu['affinity']):
            row['reasons'].append('actual Host CPU affinity mismatch')
        zones = init['info']['config']['memory']['zones']
        actual = [next(z['host_numa_node'] for z in zones if z['id']==k) for k in ['dram','slow']]
        if actual!=cfg['zones']: row['reasons'].append('actual zone mismatch')
        zone_host={z['id']:z['host_numa_node'] for z in zones}
        pxm_host={n['guest_numa_id']:zone_host[n['memory_zones'][0]]
                  for n in init['info']['config']['numa'] if n.get('memory_zones')}
        guest_host={int(n):pxm_host[int(pxm)] for n,pxm in re.findall(r'SRAT: Node (\d+) PXM (\d+) \[mem',boot_log)}
        row['guest_to_host_numa']=guest_host
        if len(guest_host)!=2:row['reasons'].append('Guest physical tier map unavailable')
        if any(s['balloon_actual_bytes']!=init['nominal_balloon'] for s in samples): row['reasons'].append('capacity changed')
    def metric(s, tier, category=None):
        values = s['rss_node_bytes'] if category is None else s['rss_breakdown'][category]
        return sum(v for n,v in values.items() if (int(n[1:]) in [0,1]) == (tier=='ddr'))/M
    areas = defaultdict(float)
    covered = maxgap = 0.0
    for left, right in zip(samples,samples[1:]):
        lo, hi = left['guest']['guest_monotonic_ns']/1e9, right['guest']['guest_monotonic_ns']/1e9
        dt = max(0,min(end,hi)-max(start,lo))
        if not dt: continue
        maxgap = max(maxgap,hi-lo)
        if hi-lo>1: continue
        covered += dt
        for tier in ['ddr','cxl']: areas[tier]+=metric(left,tier)*dt
    if not selected or covered<.95*(end-start) or maxgap>1: row['reasons'].append('sampling coverage/gap')
    stats = {}
    diag = out/'guest-out/diagnostics/final.json'
    if diag.exists(): stats = read(diag)['slots'][0]['stats']
    if phase:
        for flag, expected in {'fast_node':cfg.get('fast_node',1),'slow_node':cfg.get('slow_node',0),'future_benefit':cfg['benefit'],
            'sampling_enabled':cfg['sampling'],'placement_enabled':cfg['placement'],'allocation_enabled':cfg['allocation']}.items():
            if stats.get(flag)!=expected: row['reasons'].append('switch mismatch '+flag)
        if row.get('guest_to_host_numa',{}).get(stats.get('fast_node'))!=0 or row.get('guest_to_host_numa',{}).get(stats.get('slow_node'))!=2:
            row['reasons'].append('Guest Linux tier numbering does not match physical DDR/CXL')
        if cfg['sampling'] and not stats.get('load_overflows',0): row['reasons'].append('no load samples')
        if not cfg['sampling'] and (stats.get('samples_received',0) or stats.get('load_overflows',0)):
            row['reasons'].append('sampling remained active')
        for flag in ['sample_format_errors','metadata_overflow']:
            if stats.get(flag,0): row['reasons'].append('diagnostic '+flag)
    steps = [s for s in d['steps'] if not s.get('skipped')]
    long = next(s for s in steps if s['source_index']==SELECTED[job['trace']])
    row.update(complete=True, valid=not row['reasons'], signature=signature(d), stats=stats,
        executed_calls=len(steps), service_s=sum(s['elapsed_s'] for s in steps),
        execution_s=sum(s['execution_s'] for s in steps),
        control_s=sum(s['elapsed_s']-s['execution_s'] for s in steps), wall_s=d['elapsed_s'],
        selected_index=long['source_index'], selected_service_s=long['elapsed_s'],
        selected_execution_s=long['execution_s'], selected_status=long['status'],
        selected_flags=failures(long), selected_output=((long.get('stdout_tail') or '')+'\n'+(long.get('stderr_tail') or '')),
        original_outcome_mismatches=d['outcome_mismatches'], coverage=covered/(end-start), maxgap_s=maxgap,
        steps=[dict(source_index=s['source_index'],tool=s['tool'],status=s['status'],execution_s=s['execution_s'],
            service_s=s['elapsed_s'],flags=failures(s),stdout_tail=s.get('stdout_tail'),stderr_tail=s.get('stderr_tail')) for s in steps])
    for tier in ['ddr','cxl']:
        row[tier+'_peak_mib'] = max((metric(s,tier) for s in selected),default=0)
        row[tier+'_mean_mib'] = areas[tier]/covered if covered else None
        for cat in ['guest_ram','private_anon','other_file']:
            row[cat+'_'+tier+'_peak_mib'] = max((metric(s,tier,cat) for s in selected),default=0)
    if cfg['mode']=='cxl' and row['guest_ram_ddr_peak_mib']!=0: row['reasons'].append('CXL endpoint has DDR Guest RAM')
    if cfg['mode']=='dram' and row['guest_ram_cxl_peak_mib']!=0: row['reasons'].append('DRAM endpoint has CXL Guest RAM')
    lifecycle=[json.loads(x) for x in (out/'host-lifecycle.jsonl').read_text().splitlines()]
    row['startup_and_replay_ddr_peak_mib']=max((sum(s['rss_node_bytes'].get(n,0) for n in ['N0','N1'])/M for s in lifecycle if 'rss_node_bytes' in s),default=0)
    row['valid']=not row['reasons']
    return row

def ci_ratio(group, refs, metric):
    if len(group)<5 or len(refs)<5: return None
    paired=sorted(set(r['repeat'] for r in group)&set(r['repeat'] for r in refs))
    if len(paired)!=len(group) or len(paired)!=len(refs): return None
    g={r['repeat']:r[metric] for r in group};d={r['repeat']:r[metric] for r in refs}
    rng=random.Random(20261011);values=[]
    for _ in range(10000):
        ids=rng.choices(paired,k=len(paired))
        values.append(st.median(g[i] for i in ids)/st.median(d[i] for i in ids))
    values.sort()
    return [values[249],values[9749]]

def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);a=p.parse_args();R=a.root.resolve()
    plan=read(R/'plan.json');cases={c['name']:c for c in plan['cases']}
    rows=[trial(R,j,cases[j['trace']]) for j in plan['jobs']]
    groups=defaultdict(list)
    for r in rows: groups[r['trace'],r['strategy']].append(r)
    summaries=[]
    for (trace,strategy),group in groups.items():
        refs=groups.get((trace,'dram'),[])
        complete=bool(refs) and all(r['complete'] for r in group+refs)
        equivalent=complete and all(r['signature']==refs[0]['signature'] for r in group+refs)
        summary=dict(trace=trace,strategy=strategy,mode=group[0]['config']['mode'],
            independent_vms=len(group),complete=complete,relative_equivalent=equivalent,
            valid=complete and equivalent and all(r['valid'] for r in group+refs))
        if complete:
            summary['medians']={k:st.median(r[k] for r in group) for k in METRICS}
            summary['ratios']={k:summary['medians'][k]/st.median(r[k] for r in refs) for k in METRICS if st.median(r[k] for r in refs)}
            summary['bootstrap_95ci']={k:ci_ratio(group,refs,k) for k in ['service_s','selected_service_s','ddr_peak_mib','ddr_mean_mib']}
            summary['within_5pct_screen']=summary['ratios']['service_s']<=1.05 and summary['ratios']['selected_service_s']<=1.05
            summary['ranges']={k:[min(r[k] for r in group),max(r[k] for r in group)] for k in METRICS}
        summaries.append(summary)
    comparisons=[]
    for (trace,strategy),group in groups.items():
        if group[0]['config']['mode']!='ours' or strategy.startswith('full'): continue
        quota=group[0]['config'].get('quota')
        reference=f'full{quota}'
        refs=groups.get((trace,reference),[])
        complete=bool(refs) and all(r['complete'] for r in group+refs)
        valid=complete and all(r['valid'] and r['signature']==refs[0]['signature'] for r in group+refs)
        comparison=dict(trace=trace,strategy=strategy,reference=reference,complete=complete,valid=valid)
        if complete:
            comparison['ratios']={k:st.median(r[k] for r in group)/st.median(r[k] for r in refs)
                for k in ['service_s','selected_service_s','ddr_peak_mib','ddr_mean_mib'] if st.median(r[k] for r in refs)}
            comparison['bootstrap_95ci']={k:ci_ratio(group,refs,k) for k in comparison['ratios']}
        comparisons.append(comparison)
    result=dict(planned=len(rows),finished=sum(r['complete'] for r in rows),basic_valid=sum(r['valid'] for r in rows),
        trials=rows,summaries=summaries,selected_calls=SELECTED,
        ours_comparisons=comparisons,
        limits=['Peak is a sampled peak; independent VMs are repetition units',
            'Whole VMM RSS includes shared Host code and excludes some KVM/kernel costs',
            'Expected original failures remain; matched paths do not prove task success',
            '+5% is a proposed screening bound; bootstrap with few VMs is descriptive',
            'Ablations share quota, not necessarily equal measured DDR; no overcommit density claim'])
    (R/'analysis.json').write_text(json.dumps(result,indent=2)+'\n')
    columns=['cohort','trace','strategy','repeat','complete','valid','ddr_peak_mib','ddr_mean_mib','cxl_peak_mib',
        'service_s','execution_s','selected_service_s','guest_ram_ddr_peak_mib','private_anon_ddr_peak_mib','other_file_ddr_peak_mib']
    with (R/'trials.csv').open('w') as f:
        w=csv.DictWriter(f,columns,extrasaction='ignore');w.writeheader();w.writerows(rows)
    with (R/'tools.csv').open('w') as f:
        columns=['trace','strategy','repeat','source_index','tool','status','execution_s','service_s','flags']
        w=csv.DictWriter(f,columns,extrasaction='ignore');w.writeheader()
        for r in rows:
            for s in r.get('steps',[]): w.writerow({**r,**s})
    print(json.dumps(dict(planned=len(rows),finished=result['finished'],basic_valid=result['basic_valid'])))
    for r in rows:
        if r['reasons']: print('INVALID',r['name'],r['reasons'])
    for s in summaries:
        if s['complete']: print(s['trace'],s['strategy'],s['valid'],{k:round(v,3) for k,v in s['ratios'].items()})

if __name__=='__main__': main()
