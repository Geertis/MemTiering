#!/usr/bin/env python3
"""Report failures before speedups; retain independent-VM observations."""
import argparse
import csv
import hashlib
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path

def read(path): return json.loads(path.read_text())
def percentile(v,q):
    if not v:return None
    s=sorted(v);return s[max(0,math.ceil(q*len(s))-1)]
def ratio(a,b):return a/b if a is not None and b else None

def trial(root,job):
    path=root/job['name'];out=path/'guest-out';receipt=root/(job['name']+'.receipt.json')
    r={**job,'complete':False,'valid':False,'reasons':[]}
    if (path/'INVALID.json').exists():r['reasons'].append(read(path/'INVALID.json')['reason'])
    if not receipt.exists():r['reasons'].append('unfinished');return r
    r['runner_exit']=read(receipt)['exit_code']
    if r['runner_exit']:r['reasons'].append('runner failure')
    if not (out/'result.json').exists():r['reasons'].append('missing result');return r
    d=read(out/'result.json');m=read(path/'manifest.json')
    # Verify exact measured module, replay, controller and shell, not whatever
    # happens to be present later in the working tree.
    for file,sha in m['files'].items():
        archive=path/'protocol-inputs'/Path(file).name
        if archive.exists():actual=archive
        else:actual=Path(file) # raw trace inputs (immutable hashes checked)
        if not actual.exists() or hashlib.sha256(actual.read_bytes()).hexdigest()!=sha:
            r['reasons'].append('input hash mismatch: '+file)
    steps=[s for s in d['steps'] if not s.get('skipped')]
    times=[s.get('execution_s',s['elapsed_s']) for s in steps]
    resources=d['resource_samples'];memory=[]
    for s in resources:
        n=s.get('memory_numa_stat',{})
        memory.append((s['monotonic_s'],sum(n.get(k,{}).get('N0',0) for k in ['anon','file']),s.get('tool') in ['thinking','idle','trace_tail']))
    wait_area=overshoot_area=0
    for (t,b,wait),(next_t,_,_) in zip(memory,memory[1:]):
        dt=max(0,next_t-t)
        if wait:wait_area+=b*dt
        overshoot_area+=max(0,b-job['budget_mib']*(1<<20))*dt
    records=[]
    if (out/'controller.jsonl').exists():
        records=[json.loads(l) for l in (out/'controller.jsonl').read_text().splitlines()]
    placements=[z['stats'] for z in records if z.get('event')=='placement']
    stats=placements[-1] if placements else {}
    if d['phase_runtime_errors']:r['reasons'].append('runtime event error')
    if d['executed_calls']!=len(steps):r['reasons'].append('partial replay')
    oom=max((s.get('memory_events',{}).get('oom',0) for s in resources),default=0)
    oomkill=max((s.get('memory_events',{}).get('oom_kill',0) for s in resources),default=0)
    if oom or oomkill:r['reasons'].append('cgroup OOM')
    kernel=(path/'dmesg.log').read_text(errors='replace') if (path/'dmesg.log').exists() else ''
    if any(x in kernel for x in ['BUG:', 'Oops:', 'Kernel panic', 'general protection fault']):r['reasons'].append('Guest kernel exception')
    if stats.get('sample_format_errors',0):r['reasons'].append('sample format errors')
    if not stats.get('load_overflows',0):r['reasons'].append('no LOAD callbacks')
    for z in records:
        if z.get('event')=='quota' and sum(s['quota'] for s in z['states'].values())>z['budget_pages']:
            r['reasons'].append('quota ledger violation');break
    callbacks=stats.get('load_overflows',0)+stats.get('store_overflows',0)
    lost=stats.get('ring_overruns',0)
    r.update(complete=True,valid=not r['reasons'],tool_sum_s=sum(times),tool_p95_s=percentile(times,.95),
             tool_p99_s=percentile(times,.99),wall_s=d['elapsed_s'],executed_calls=d['executed_calls'],
             failed_calls=d['failed_calls'],checked_original_outcomes=d['checked_original_outcomes'],
             original_outcome_mismatches=d['outcome_mismatches'],
             signature={'statuses':d['status_vector'],'diff_from_original_head':d['git_diff_sha256'],
                        'original_head':d['original_git_head'],'status':d['git_status_sha256']},
             final_commit=d['final_git_head'],
             peak_fast_mib=max((b for _,b,_ in memory),default=0)/(1<<20),
             wait_fast_mib_s=wait_area/(1<<20),overshoot_mib_s=overshoot_area/(1<<20),
             oom=oom,oom_kill=oomkill,stats=stats,loss_fraction=lost/max(1,callbacks),
             tool_steps=[{'source_index':s['source_index'],'tool':s['tool'],'kind':s['phase_kind'],
                          'seconds':s.get('execution_s',s['elapsed_s']),'status':s.get('status')} for s in steps])
    return r

def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True)
    p.add_argument('--replacements',type=Path);a=p.parse_args()
    protocol=read(a.root/'plan.json');rows=[trial(a.root,j) for j in protocol['jobs']]
    excluded=[]
    replacements=read(a.replacements) if a.replacements else {}
    for index,job in enumerate(protocol['jobs']):
        if job['name'] not in replacements:continue
        original=rows[index]
        if original['valid']:raise ValueError('cannot replace a valid original after seeing performance')
        location=Path(replacements[job['name']]);replacement=trial(location.parent,job)
        if not replacement['valid']:raise ValueError('replacement is not valid')
        replacement['replacement_for']=str(a.root/job['name'])
        excluded.append(original);rows[index]=replacement
    grouped=defaultdict(list)
    for r in rows:grouped[r['trace'],r['budget_mib'],r['strategy']].append(r)
    comparisons=[]
    for (trace,budget,strategy),group in grouped.items():
        if strategy=='reference':continue
        refs=grouped[trace,budget,'reference']
        finished=[r for r in refs+group if r['complete']]
        signatures=[r['signature'] for r in finished]
        equivalent=bool(signatures) and all(s==signatures[0] for s in signatures)
        stable=all(r['valid'] for r in refs+group) and equivalent
        med=lambda g,key: statistics.median(r[key] for r in g) if all(r['complete'] for r in g) else None
        c={'trace':trace,'budget_mib':budget,'strategy':strategy,'independent_vms':len(group),
           'complete':all(r['complete'] for r in refs+group),'valid':stable,'semantic_equivalence':equivalent,
           'tool_sum_ratio':ratio(med(group,'tool_sum_s'),med(refs,'tool_sum_s')),
           'tool_p99_ratio':ratio(med(group,'tool_p99_s'),med(refs,'tool_p99_s')),
           'peak_fast_mib_reference':med(refs,'peak_fast_mib'),'peak_fast_mib_strategy':med(group,'peak_fast_mib'),
           'loss_fraction_reference':med(refs,'loss_fraction'),'loss_fraction_strategy':med(group,'loss_fraction'),
           'worker_cpu_s_reference':statistics.median(r['stats'].get('worker_cpu_ns',0)/1e9 for r in refs if r['complete']) if any(r['complete'] for r in refs) else None,
           'worker_cpu_s_strategy':statistics.median(r['stats'].get('worker_cpu_ns',0)/1e9 for r in group if r['complete']) if any(r['complete'] for r in group) else None,
           'exclusions':[{'trial':r['name'],'reasons':r['reasons']} for r in refs+group if not r['valid']]}
        if not equivalent and c['complete']:c['exclusions'].append({'reasons':['different status vector, original HEAD, final diff, or Git status']})
        c['performance_gate_pass']=stable and c['tool_p99_ratio'] is not None and c['tool_p99_ratio']<=1.2
        comparisons.append(c)
    summary={'complete':all(r['complete'] for r in rows),'evidence_class':protocol['evidence_class'],
             'planned':len(rows),'finished':sum(r['complete'] for r in rows),'valid_trials':sum(r['valid'] for r in rows),
             'trials':rows,'comparisons':comparisons,'excluded_originals':excluded,
             'limitations':['compressed waits cannot establish 1x WAIT savings or density',
                            'two VMs per strategy are pilot evidence; tool calls are correlated',
                            'same-Guest payload quota excludes Guest OS, file policy exceptions and VMM overhead',
                            'original task correctness requires an evaluator; identical replay does not establish SWE success']}
    (a.root/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    columns=['trace','strategy','budget_mib','repeat','complete','valid','tool_sum_s','tool_p95_s','tool_p99_s',
             'peak_fast_mib','overshoot_mib_s','wait_fast_mib_s','loss_fraction','failed_calls','original_outcome_mismatches']
    with (a.root/'trials.csv').open('w') as f:
        w=csv.DictWriter(f,columns,extrasaction='ignore');w.writeheader();w.writerows(rows)
    lines=['# 多轨迹实测汇总','',f"已完成 {summary['finished']}/{summary['planned']}，有效基础检查 {summary['valid_trials']}；证据级别：{summary['evidence_class']}。",'',
           '|轨迹|策略|有效对照|工具合计比|工具P99比|DRAM峰值MiB 参考→策略|丢失比例 参考→策略|',
           '|---|---|---|---:|---:|---:|---:|']
    fmt=lambda x:'—' if x is None else f'{x:.3f}'
    for c in comparisons:
        lines.append(f"|{c['trace']}|{c['strategy']}|{c['valid'] if c['complete'] else '待完成'}|{fmt(c['tool_sum_ratio'] if c['valid'] else None)}|{fmt(c['tool_p99_ratio'] if c['valid'] else None)}|{fmt(c['peak_fast_mib_reference'])} → {fmt(c['peak_fast_mib_strategy'])}|{fmt(c['loss_fraction_reference'])} → {fmt(c['loss_fraction_strategy'])}|")
    lines += ['','任何无效或语义不稳定配置均不能作为性能结论。工具 P99 与总耗时来自每个独立 VM，表格取 VM 中位数；不将多个工具视为独立重复。','']+['- '+x for x in summary['limitations']]
    (a.root/'summary.md').write_text('\n'.join(lines)+'\n')
    print(json.dumps({k:summary[k] for k in ['complete','planned','finished','valid_trials']}))

if __name__=='__main__':main()
