#!/usr/bin/env python3
import argparse
import importlib.util
import json
import statistics
from pathlib import Path

H=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('baseline',H/'analyze.py')
base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)
M=1<<20
p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);a=p.parse_args();R=a.root.resolve()
plan=base.read(R/'plan.json');cases={c['name']:c for c in plan['cases']};batches=[]
for job in plan['jobs']:
    receipt=R/(job['name']+'.receipt.json');out=R/job['name'];row={**job,'complete':False,'valid':False}
    if not receipt.exists():batches.append(row);continue
    actual=base.read(receipt)
    trials=[]
    for lane,name in enumerate(job['traces']):
        j={'name':f'lane{lane}','trace':name,'policy':job['policy'],'strategy':job['policy'],'repeat':1,'budget_mib':256,'speed':1}
        trials.append(base.trial(out,j,cases[name]))
    releases=base.read(out/'release-times.json');start=min(r['host_monotonic_ns']/1e9 for r in releases)
    finish=[]
    for lane in [0,1]:
        d=base.read(out/f'lane{lane}/guest-out/result.json')
        samples=[json.loads(s) for s in (out/f'lane{lane}/host-capacity.jsonl').read_text().splitlines()]
        samples=[s for s in samples if s['event']=='sample']
        offset=statistics.median(s['host_monotonic_ns']/1e9-s['guest']['guest_monotonic_ns']/1e9 for s in samples)
        finish.append(max(s['monotonic_s'] for s in d['resource_samples'])+offset)
    end=max(finish)
    all_samples=[json.loads(s) for s in (out/'joint-host.jsonl').read_text().splitlines()]
    samples=[s for s in all_samples if start<=s['host_monotonic_ns']/1e9<=end]
    area=observed=maxgap=0
    for first,last in zip(all_samples,all_samples[1:]):
        l=first['host_monotonic_ns']/1e9;r=last['host_monotonic_ns']/1e9
        dt=max(0,min(r,end)-max(l,start))
        if not dt:continue
        maxgap=max(maxgap,r-l)
        if r-l>1:continue
        observed+=dt;area+=first['ddr_rss_bytes']/M*dt
    reasons=[]
    if actual['exit_code'] or actual['monitor_code']:reasons.append('runner/joint observer failure')
    if not all(t['valid'] for t in trials):reasons.append('invalid component VM')
    if observed<.95*(end-start) or maxgap>1:reasons.append('joint coverage/gap')
    row.update(complete=True,valid=not reasons,reasons=reasons,trials=trials,
        joint_ddr_peak_mib=max(s['ddr_rss_bytes']/M for s in samples),
        joint_cxl_peak_mib=max(s['cxl_rss_bytes']/M for s in samples),
        joint_total_peak_mib=max((s['ddr_rss_bytes']+s['cxl_rss_bytes'])/M for s in samples),
        joint_ddr_mean_mib=area/observed,joint_coverage=observed/(end-start),
        joint_max_sample_gap_s=maxgap,paired_replay_span_s=end-start,
        aggregate_tool_service_s=sum(t['tool_service_sum_s'] for t in trials))
    batches.append(row)
for row in batches:
    if not row['complete']:continue
    ref=next(r for r in batches if r['schedule']==row['schedule'] and r['policy']=='dram')
    if ref['complete']:
        equivalent=all(t['signature']==r['signature'] for t,r in zip(row['trials'],ref['trials']))
        row['relative_equivalent']=equivalent
        row['valid']=row['valid'] and ref['valid'] and equivalent
        row['service_ratio_to_dram']=row['aggregate_tool_service_s']/ref['aggregate_tool_service_s']
        row['ddr_peak_ratio_to_dram']=row['joint_ddr_peak_mib']/ref['joint_ddr_peak_mib']
        row['per_trace_service_ratios']={t['trace']:t['tool_service_sum_s']/r['tool_service_sum_s'] for t,r in zip(row['trials'],ref['trials'])}
result={'planned_pairs':len(batches),'finished_pairs':sum(r['complete'] for r in batches),
    'valid_pairs':sum(r['valid'] for r in batches),'batches':batches,
    'limits':['One independent pair per policy/schedule; exploratory, no confidence interval',
        'No enforced Host per-node physical pool; measured residency is not maximum admissible N',
        'Simultaneous start is not synchronized ACTIVE for every tool',
        'Guest-to-Host clock offsets are estimated from observer streams; not atomic RSS snapshots',
        'Sum of RSS counts shared mappings conservatively and excludes some kernel costs']}
(R/'analysis.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps({k:result[k] for k in ['planned_pairs','finished_pairs','valid_pairs']}))
