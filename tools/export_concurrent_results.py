#!/usr/bin/env python3
"""Export bounded, auditable summaries without disks, raw outputs or credentials."""
import argparse
import csv
from datetime import datetime
from zoneinfo import ZoneInfo
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
CAMPAIGNS={
 'v4':'PhaseTieing-concurrent-20261030-v4-tiered-pressure',
 'v5':'PhaseTieing-concurrent-20261030-v5-capacity-scan',
 'v6':'PhaseTieing-concurrent-20261030-v6-adaptive-reserve',
 'v7':'PhaseTieing-concurrent-20261030-v7-pressure-7g',
 'v8':'PhaseTieing-concurrent-20261030-v8-pressure-6g',
 'v9':'PhaseTieing-concurrent-20261030-v9-startup-admission',
 'v10':'PhaseTieing-concurrent-20261030-v10-scaled-reserve',
}
FIELDS=['version','label','dram_gib','n','medium','strategy','repetition','outcome',
 'correct_instances','l1_oom','minimum_live_vmms','batch_s','trace_makespan_s',
 'service_sum_s','budget_ceiling_mib','minimum_budget_mib','grant_acknowledgements',
 'actual_cold_tier_used','control_definition','observed_duration_s','user_directed_stop',
 'reserve_policy','physical_reserve_mib']


def read(path):return json.loads(Path(path).read_text())
def save(path,x):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(x,ensure_ascii=False,indent=2)+'\n')
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def export(source,output):
    rows=[];instances=[];snapshot={}
    row_fields=['slot','trace','complete','valid','reasons','e2e_s','service_s','execution_s',
        'executed_calls','longest_tool_s','sampling_coverage','fast_mean_mib','cold_mean_mib',
        'fast_sampled_peak_mib','cold_sampled_peak_mib','swap_peak_mib','pswpin_pages','pswpout_pages',
        'comparison_scope','original_observable_fidelity','signature']
    for version,name in CAMPAIGNS.items():
        root=source/name
        if not (root/'matrix.json').exists():continue
        state=read(root/'status.json');snapshot[version]={k:state.get(k) for k in ['stage','active','completed','failed','startup_blocked','severe_timeouts','updated_epoch']}
        save(output/'protocols'/f'{version}.json',read(root/'protocol.json'))
        save(output/'source-manifests'/f'{version}.json',read(root/'source-manifest.json'))
        for spec in read(root/'matrix.json'):
            folder=root/'cohorts'/spec['label'];ap=folder/'audit.json'
            row={k:spec.get(k) for k in ['label','dram_gib','n','medium','strategy','repetition']}
            row.update(version=version,outcome='running' if state.get('active')==spec['label'] else 'not-run',
                reserve_policy=spec.get('reserve_policy','fixed'),
                control_definition='immutable uniform upper quota' if version in ['v4','v5'] else 'phase-blind equal share with common adaptive reserve')
            if ap.exists():
                audit=read(ap);group=read(folder/'status.json');mp=folder/'mechanism-audit.json';m=read(mp) if mp.exists() else {}
                passed=bool(audit['valid'] and m.get('actual_cold_tier_used') and m.get('guest_global_grant_acknowledgements')==spec['n'])
                row.update(outcome='passed' if passed else audit['classification'],
                    correct_instances=audit['correctness_valid'],l1_oom=audit['l1_oom'],
                    minimum_live_vmms=audit['minimum_live_vmms'],
                    budget_ceiling_mib=m.get('budget_ceiling_mib',m.get('budget_mib')),
                    minimum_budget_mib=m.get('minimum_budget_mib',m.get('budget_mib')),
                    grant_acknowledgements=m.get('guest_global_grant_acknowledgements'),
                    actual_cold_tier_used=m.get('actual_cold_tier_used'))
                row['physical_reserve_mib']=m.get('reserve_mib',1024 if spec.get('reserve_policy','fixed')=='fixed' else None)
                if group.get('result')=='startup-resource-admission-blocked' and not audit['l1_oom']:
                    row['outcome']='startup-resource-admission-blocked'
                if 'all_done_epoch' in group:
                    row['batch_s']=group['all_done_epoch']-group['release_epoch']
                complete=[v for v in audit['rows'] if v.get('complete') and 'e2e_s' in v]
                if len(complete)==spec['n']:
                    row['trace_makespan_s']=max(v['e2e_s']+spec['slots'][v['slot']]['offset_s'] for v in complete)
                    row['service_sum_s']=sum(v['service_s'] for v in complete)
                clean={k:v for k,v in audit.items() if k!='rows'}
                clean['rows']=[{k:v[k] for k in row_fields if k in v} for v in audit['rows']]
                clean['mechanism']=m
                clean['evidence_hashes']={n:sha(folder/n) for n in ['audit.json','status.json','l1-resources.jsonl.gz','global-pool.jsonl.gz','l1-dmesg.log'] if (folder/n).exists()}
                clean['source_manifest_sha256']=sha(root/'source-manifest.json')
                save(output/'audits'/(spec['label']+'.json'),clean)
                for v in complete:
                    instances.append(dict(version=version,label=spec['label'],**{k:v[k] for k in row_fields if k in v and k!='signature'}))
            decision_path=folder/'user-stop.json'
            if decision_path.exists():
                decision=read(decision_path)
                if decision.get('classification')=='severe-timeout' and decision.get('user_directed'):
                    row.update(outcome='severe-timeout',observed_duration_s=decision['observed_duration_s'],user_directed_stop=True)
                    row.pop('batch_s',None)
                    save(output/'terminations'/(spec['label']+'.json'),decision)
            rows.append(row)
    output.mkdir(parents=True,exist_ok=True)
    with (output/'matrix.csv').open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=FIELDS,lineterminator='\n');writer.writeheader();writer.writerows(rows)
    save(output/'instances.json',instances)
    save(output/'matrix.json',rows)
    now=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()
    save(output/'snapshot.json',dict(as_of=now,campaigns=snapshot,
        completed_cohorts=sum(v['outcome']=='passed' for v in rows),
        complete_instances=sum(v.get('correct_instances',0) for v in rows if v['outcome']=='passed'),
        additional_complete_instances_in_stopped_cohorts=sum(v.get('correct_instances',0) for v in rows if v['outcome']=='severe-timeout'),
        note='Version-separated snapshot; running and unrun points are not successes. Raw binaries/traces/log streams are external.'))
    bad=source/CAMPAIGNS['v5']/'cohorts/v5-cxl-P-d8-n8-r1/l1-dmesg.log'
    lines=[s for s in bad.read_text().splitlines() if any(k in s for k in ['invoked oom-killer','oom-kill:','Out of memory:'])]
    save(output/'oom-evidence.json',dict(label='v5-cxl-P-d8-n8-r1',lines=lines,raw_sha256=sha(bad),
        note='NUMA policy-constrained DRAM-node OOM; incomplete traces are not eight independent semantic failures.'))
    for name in ['complete-resource-summary.json','failure-diagnosis.json']:
        for version,folder in CAMPAIGNS.items():
            p=source/folder/name
            if p.exists():save(output/(version+'-'+name),read(p))
    candidate=source/'PhaseTieing-concurrent-20261029-v3-baseline/paper-host-footprint-recheck.json'
    if candidate.exists():
        profile=read(candidate)
        profile.pop('candidate_ratios',None)
        profile['source_file_sha256']=sha(candidate)
        profile['ratio_note']='DRAM-only denominator; old total-RAM CXL ratio fields omitted as superseded. Numerator remains an unapproved cache-inclusive profiling candidate.'
        profile['candidate_dram_only_ratios']=[dict(dram_gib=d,
            ratio=profile['n8_candidate_peak_sum_gib']/d) for d in [12,10,8,7,6]]
        save(output/'offline-host-profile-candidate.json',profile)
    from render_concurrent_summary import render
    render(output)
    print(f'EXPORTED: {len(rows)} matrix rows; {len(instances)} complete instance records; {now}')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True)
    p.add_argument('--output',type=Path,default=ROOT/'published/concurrent-20261030')
    a=p.parse_args();export(a.source,a.output)
