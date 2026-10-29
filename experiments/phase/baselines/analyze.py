#!/usr/bin/env python3
"""Compare complete independent VMs; expose invalid and unequal-cost comparisons."""
import argparse
import csv
import hashlib
import json
import math
import re
import statistics
from collections import defaultdict
from pathlib import Path

M=1<<20
read=lambda p:json.loads(p.read_text())
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
med=statistics.median

def quantile(v,q):
    return sorted(v)[max(0,math.ceil(q*len(v))-1)] if v else None

def signature(d):
    steps=[s for s in d['steps'] if not s.get('skipped')]
    return {'statuses':d['status_vector'],'head':d['original_git_head'],
        'diff':d['git_diff_sha256'],'git_status':d['git_status_sha256'],
        'outputs':{str(s['source_index']):s.get('stdout_sha256') for s in steps if s['tool'] in ['Grep','Read','Glob']},
        'test_counts':{str(s['source_index']):re.findall(r'\b(\d+) (passed|failed|errors?|skipped|deselected|xfailed|xpassed)\b',
            s.get('stdout_tail','')+'\n'+s.get('stderr_tail','')) for s in steps if s['tool']=='Bash'}}

def trial(root,job,case):
    row={**job,'complete':False,'valid':False,'reasons':[]}
    p=root/job['name'];receipt=root/(job['name']+'.receipt.json')
    if not receipt.exists():return row
    row['runner_exit']=read(receipt)['exit_code']
    if row['runner_exit']:row['reasons'].append('runner failed')
    if not (p/'guest-out/result.json').exists():return row
    d=read(p/'guest-out/result.json');m=read(p/'manifest.json')
    for name,expected_sha in case['input_hashes'].items():
        if sha(Path(case['trace'])/name)!=expected_sha:row['reasons'].append('catalog input drift '+name)
    for file,expected in m['files'].items():
        copied=p/'protocol-inputs'/Path(file).name
        actual=copied if copied.exists() else Path(file)
        if not actual.exists() or sha(actual)!=expected:row['reasons'].append('input mismatch '+file)
    if sorted(s['source_index'] for s in d['steps'])!=list(range(len(read(Path(case['trace'])/'tool_calls.json')))):
        row['reasons'].append('partial source-index coverage')
    if d['phase_runtime_errors']:row['reasons'].append('phase runtime errors')
    preflight=(p/'policy-preflight.log').read_text()
    module_present=bool(re.search(r'^demeter_placement\s',preflight,re.M))
    if module_present!=(job['policy']=='phase'):row['reasons'].append('actual placement module mismatch')
    expected={'dram':(2,1),'cxl':(2,2),'static':(6,3)}.get(job['policy'])
    if 'guest_tool_bind_node' in job:
        expected=(2,1<<job['guest_tool_bind_node'])
        if m.get('whole_guest_ram_host_node')!=job['host_ram_node']:
            row['reasons'].append('whole Guest RAM Host mapping manifest mismatch')
    actual=d.get('initial_memory_policy',{})
    if expected and (actual.get('mode'),actual.get('mask'))!=expected:row['reasons'].append('initial memory policy mismatch')
    if d['phase_aware']!=(job['policy']=='phase'):row['reasons'].append('actual runtime controller mismatch')
    kernel=(p/'dmesg.log').read_text(errors='replace')
    if any(x in kernel for x in ['BUG:','Oops:','Kernel panic','general protection fault','VM_FAULT_OOM leaked']):
        row['reasons'].append('Guest kernel exception/fault retry')
    resources=d['resource_samples'];start=min(s['monotonic_s'] for s in resources);end=max(s['monotonic_s'] for s in resources)
    if any(s.get('memory_events',{}).get('oom',0) or s.get('memory_events',{}).get('oom_kill',0) for s in resources):
        row['reasons'].append('Guest cgroup OOM')
    records=[json.loads(line) for line in (p/'host-capacity.jsonl').read_text().splitlines()]
    init=next((s for s in records if s['event']=='init'),None)
    final=next((s for s in reversed(records) if s['event']=='end'),None)
    if 'host_ram_node' in job and init:
        zones=init['info']['config']['memory']['zones']
        if len(zones)!=2 or any(z['host_numa_node']!=job['host_ram_node'] for z in zones):
            row['reasons'].append('actual whole Guest RAM Host mapping mismatch')
    if not init or not final or final.get('errors'):row['reasons'].append('Host observer incomplete/errors')
    all_samples=[s for s in records if s['event']=='sample']
    samples=[s for s in all_samples if start<=s['guest']['guest_monotonic_ns']/1e9<=end]
    if not init or not init['info']['config']['balloon']['free_page_reporting']:row['reasons'].append('reporting disabled')
    if any(s['balloon_actual_bytes']!=init['nominal_balloon'] for s in all_samples):row['reasons'].append('capacity changed')
    ddr=lambda s:sum(s['rss_node_bytes'].get(n,0) for n in ['N0','N1'])/M
    cxl=lambda s:s['rss_node_bytes'].get('N2',0)/M
    area=slow_area=observed=maxgap=0
    for first,last in zip(all_samples,all_samples[1:]):
        left=first['guest']['guest_monotonic_ns']/1e9;right=last['guest']['guest_monotonic_ns']/1e9
        dt=max(0,min(end,right)-max(start,left))
        if not dt:continue
        maxgap=max(maxgap,right-left)
        if right-left>1:continue
        observed+=dt;area+=ddr(first)*dt;slow_area+=cxl(first)*dt
    if not samples or observed<.95*(end-start) or maxgap>1:row['reasons'].append('Host sampling coverage/gap')
    steps=[s for s in d['steps'] if not s.get('skipped')]
    execution=[s['execution_s'] for s in steps];service=[s['elapsed_s'] for s in steps]
    fast=[];slow=[];anon_fast=[];anon_slow=[]
    for s in resources:
        numa=s.get('memory_numa_stat',{})
        fast.append(sum(numa.get(k,{}).get('N0',0) for k in ['anon','file'])/M)
        slow.append(sum(numa.get(k,{}).get('N1',0) for k in ['anon','file'])/M)
        anon_fast.append(numa.get('anon',{}).get('N0',0)/M)
        anon_slow.append(numa.get('anon',{}).get('N1',0)/M)
    anon_total_peak=max((x+y for x,y in zip(anon_fast,anon_slow)),default=0)
    bind_node=job.get('guest_tool_bind_node',0 if job['policy']=='dram' else 1)
    wrong=max(anon_slow,default=0) if bind_node==0 else max(anon_fast,default=0)
    if expected and job['policy']!='static' and wrong>max(2,anon_total_peak*.02):
        row['reasons'].append('unexpected anonymous residency under BIND')
    if 'host_ram_node' in job:
        # Both Guest NUMA nodes have the same physical tier in this cohort.
        total=[x+y for x,y in zip(fast,slow)]
        anon_total=[x+y for x,y in zip(anon_fast,anon_slow)]
        zeros=[0.0]*len(total)
        fast,slow=(total,zeros) if job['host_ram_node']==0 else (zeros,total)
        anon_fast,anon_slow=(anon_total,zeros) if job['host_ram_node']==0 else (zeros,anon_total)
    stats={}
    if (p/'guest-out/diagnostics/final.json').exists():stats=read(p/'guest-out/diagnostics/final.json')['slots'][0]['stats']
    if job['policy']=='phase':
        for flag in ['sample_format_errors','metadata_overflow']:
            if stats.get(flag,0):row['reasons'].append('phase diagnostics '+flag)
        for flag in ['allocation_enabled','sample_batch_enabled','sample_wakeup_enabled','lifecycle_wakeup_enabled']:
            if stats.get(flag)!=1:row['reasons'].append('phase switch mismatch '+flag)
        if not stats.get('load_overflows',0):row['reasons'].append('no LOAD samples')
    lifecycle=[json.loads(line) for line in (p/'host-lifecycle.jsonl').read_text().splitlines()]
    lifecycle=[s for s in lifecycle if 'rss_node_bytes' in s]
    row.update(complete=True,valid=not row['reasons'],signature=signature(d),executed_calls=len(steps),
        tool_execution_sum_s=sum(execution),tool_service_sum_s=sum(service),phase_overhead_sum_s=sum(service)-sum(execution),
        tool_execution_max_s=max(execution),tool_execution_p95_s=quantile(execution,.95),
        tool_execution_empirical_p99_s=quantile(execution,.99),replay_wall_s=d['elapsed_s'],
        payload_ddr_peak_mib=max(fast),payload_cxl_peak_mib=max(slow),
        payload_total_peak_mib=max(x+y for x,y in zip(fast,slow)),
        payload_anon_ddr_peak_mib=max(anon_fast),payload_anon_cxl_peak_mib=max(anon_slow),
        host_ddr_replay_peak_mib=max(map(ddr,samples),default=0),host_ddr_replay_mean_mib=area/observed if observed else None,
        host_cxl_replay_peak_mib=max(map(cxl,samples),default=0),host_cxl_replay_mean_mib=slow_area/observed if observed else None,
        host_total_replay_peak_mib=max((ddr(s)+cxl(s) for s in samples),default=0),
        host_ddr_start_and_replay_peak_mib=max(map(ddr,lifecycle),default=0),
        host_sample_gap_max_s=maxgap,host_coverage=observed/(end-start),
        guest_slow_free_min_mib=min((s['guest']['nodes']['1']['MemFree']/M for s in samples),default=None),
        initial_memory_policy=actual,stats=stats,original_outcome_mismatches=d['outcome_mismatches'],
        tool_steps=[{k:s.get(k) for k in ['source_index','tool','phase_kind','execution_s','elapsed_s','status']} for s in steps])
    return row

def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);a=p.parse_args();R=a.root.resolve()
    plan=read(R/'plan.json');cases={c['name']:c for c in plan['cases']}
    rows=[trial(R,j,cases[j['trace']]) for j in plan['jobs']]
    groups=defaultdict(list)
    for r in rows:groups[r['trace'],r['policy']].append(r)
    summaries=[]
    for (trace,policy),g in groups.items():
        refs=groups.get((trace,'dram'),[])
        complete=all(r['complete'] for r in g+refs)
        equivalent=complete and all(r['signature']==refs[0]['signature'] for r in g+refs)
        out={'trace':trace,'policy':policy,'independent_vms':len(g),'complete':complete,
             'relative_equivalent':equivalent,'valid':complete and equivalent and all(r['valid'] for r in g+refs)}
        if complete:
            keys=['tool_execution_sum_s','tool_service_sum_s','tool_execution_max_s','tool_execution_p95_s',
                'host_ddr_replay_peak_mib','host_ddr_replay_mean_mib','host_cxl_replay_peak_mib',
                'host_ddr_start_and_replay_peak_mib','payload_ddr_peak_mib']
            out['medians']={k:med(r[k] for r in g) for k in keys}
            out['ratios_to_dram']={k:out['medians'][k]/med(r[k] for r in refs) for k in keys if med(r[k] for r in refs)}
            out['paired_execution_ratios']=[r['tool_execution_sum_s']/next(x for x in refs if x['repeat']==r['repeat'])['tool_execution_sum_s'] for r in g]
            out['paired_service_ratios']=[r['tool_service_sum_s']/next(x for x in refs if x['repeat']==r['repeat'])['tool_service_sum_s'] for r in g]
        summaries.append(out)
    result={'planned':len(rows),'finished':sum(r['complete'] for r in rows),'basic_valid':sum(r['valid'] for r in rows),
        'speed':plan['speed'],'trials':rows,'summaries':summaries,
        'limits':['Independent VM is repetition unit; few repetitions do not establish production p99',
            'Whole-VMM RSS includes shared mappings and excludes some KVM/kernel costs',
            'Static weighted fraction is not a matched observed DDR budget',
            'BIND affects new allocations; existing OS and shared cached mappings may remain DDR',
            'No live LLM; recorded tools serialized; original test-range differences remain']}
    if all('host_ram_node' in j for j in plan['jobs']):
        result['scope']='Whole Guest RAM mapped DDR/CXL; tool MPOL_BIND Guest node0 in both'
        result['limits'][3]='Both Guest RAM zones mapped to one Host node; private VMM/kernel costs may remain DDR'
    (R/'analysis.json').write_text(json.dumps(result,indent=2)+'\n')
    columns=['trace','policy','repeat','complete','valid','tool_execution_sum_s','tool_service_sum_s',
        'host_ddr_replay_peak_mib','host_ddr_replay_mean_mib','host_cxl_replay_peak_mib','payload_ddr_peak_mib']
    with (R/'trials.csv').open('w') as f:
        w=csv.DictWriter(f,columns,extrasaction='ignore');w.writeheader();w.writerows(rows)
    print(json.dumps({k:result[k] for k in ['planned','finished','basic_valid']}))
    for r in rows:
        if r['complete'] and not r['valid']:print('INVALID',r['name'],r['reasons'])

if __name__=='__main__':main()
