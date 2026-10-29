#!/usr/bin/env python3
"""Serial, order-balanced fresh-VM trials; immutable plans and resume receipts."""
import argparse
import hashlib
import json
import os
import random
import subprocess
import time
from pathlib import Path

W = Path(__file__).resolve().parents[2]
H = Path(__file__).resolve().parent
STRATEGIES = {'reference':(0,0,0,0), 'allocation':(1,0,0,0), 'sampling':(0,1,1,1), 'full':(1,1,1,1), 'full_no_wake':(1,1,0,1), 'full_no_lifecycle':(1,1,1,0)}

def plan(cases, strategies, budgets, repeats, speed):
    jobs=[]
    # Every repeat reverses strategy order. The fixed seed randomizes trace
    # order without choosing it based on observed performance.
    rng=random.Random(20261008)
    for repeat in range(repeats):
        ordered=list(cases); rng.shuffle(ordered)
        for c in ordered:
            for budget in budgets:
                order=list(strategies)
                if repeat % 2: order.reverse()
                for strategy in order:
                    jobs.append({'trace':c['name'],'strategy':strategy,'budget_mib':budget,
                                 'repeat':repeat+1,'speed':speed,
                                 'name':f"{c['name']}-{strategy}-q{budget}-r{repeat+1}"})
    return jobs

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--catalog',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--artifact',type=Path,required=True)
    p.add_argument('--strategies',nargs='+',choices=STRATEGIES,default=list(STRATEGIES))
    p.add_argument('--budgets',type=int,nargs='+',default=[64,128,256])
    p.add_argument('--repeats',type=int,default=3)
    p.add_argument('--speed',type=float,default=1)
    p.add_argument('--lane',type=int,default=0)
    p.add_argument('--load-period',type=int,default=65535)
    p.add_argument('--traces',nargs='+')
    p.add_argument('--semantic-audit',type=Path)
    p.add_argument('--plan-only',action='store_true')
    p.add_argument('--search-backend',choices=['legacy','ripgrep'],default='ripgrep')
    p.add_argument('--phase-counters',action='store_true')
    p.add_argument('--bind-tool-node',type=int,choices=[0,1])
    p.add_argument('--wake-us',type=int,default=2000)
    p.add_argument('--placement',type=int,choices=[0,1],default=1)
    a=p.parse_args()
    a.catalog=a.catalog.resolve()
    a.artifact=a.artifact.resolve()
    a.output=a.output.resolve()
    if a.repeats<1 or a.speed<=0 or any(b<16 for b in a.budgets): p.error('invalid repeat/speed/budget')
    cases=json.loads(a.catalog.read_text())['primary']
    if a.traces:
        unknown=set(a.traces)-{c['name'] for c in cases}
        if unknown: p.error(f'unknown traces: {unknown}')
        cases=[c for c in cases if c['name'] in a.traces]
    audit=json.loads(a.semantic_audit.read_text()) if a.semantic_audit else {}
    for c in cases:
        if c['unsupported_side_effects']:
            accepted=audit.get(c['name'],{})
            if accepted.get('tool_calls_sha256')!=c['input_hashes']['tool_calls.json'] or set(accepted.get('allowed_tools',[]))!=set(c['unsupported_side_effects']):
                p.error(f"semantic audit required: {c['name']}")
        if not Path(c['golden']).exists(): p.error(f"missing golden: {c['name']}")
    jobs=plan(cases,a.strategies,a.budgets,a.repeats,a.speed)
    protocol={'version':2,'search_backend':a.search_backend,'phase_counters':a.phase_counters,'bind_tool_node':a.bind_tool_node,'placement_enabled':a.placement,'sample_wake_us':a.wake_us,'strategy_switches':STRATEGIES,'jobs':jobs,'artifact':str(a.artifact.resolve()),'cases':cases,
              'host_kernel':os.uname().release,'load_period':a.load_period,'store_period':65535,'semantic_audit':audit,
              'guest_fast_mib':1280,'guest_slow_mib':4096,'serial':True,
              'evidence_class':'formal-1x' if a.speed==1 and a.repeats>=3 else 'pilot',
              'sampling_unit':'independent VM, not individual correlated tool calls',
              'performance_gate':{'tool_p99_ratio_max':1.2,'relative_semantic_equivalence':True,'oom_max':0},
              'files':{str(x):hashlib.sha256(x.read_bytes()).hexdigest() for x in [a.catalog,H/'run_matrix.py',H/'run_single.sh']}}
    a.output.mkdir(parents=True,exist_ok=True)
    saved=a.output/'plan.json'
    if saved.exists():
        if json.loads(saved.read_text()) != protocol: raise SystemExit('resume plan/input/environment mismatch')
    else: saved.write_text(json.dumps(protocol,indent=2)+'\n')
    print(json.dumps({'jobs':len(jobs),'evidence_class':protocol['evidence_class'],'plan':str(saved)}),flush=True)
    if a.plan_only:return
    # Verify frozen inputs before admitting any trial, including module/kernel.
    frozen=json.loads((a.artifact/'SHA256SUMS.json').read_text())
    for n,sha in frozen.items():
        if hashlib.sha256((a.artifact/n).read_bytes()).hexdigest()!=sha: raise SystemExit(f'changed artifact: {n}')
    for job in jobs:
        output=a.output/job['name']; receipt=a.output/(job['name']+'.receipt.json')
        if receipt.exists():
            if json.loads(receipt.read_text())['exit_code']==0:continue
            print('RETAINED_FAILED',job['name'],flush=True);continue
        if output.exists(): raise SystemExit(f'incomplete trial requires inspection; refuse overwrite: {output}')
        case=next(c for c in cases if c['name']==job['trace'])
        allocation,batch,wakeup,lifecycle=STRATEGIES[job['strategy']]
        env={**os.environ,'VM_LANE':str(a.lane),'PHASE_ARTIFACT_DIR':str(a.artifact.resolve()),
             'ROOT_DISK':str(W/'validated-root-disks/root-demeter.qcow2'),
             'PHASE_BUDGET_MIB':str(job['budget_mib']),'SB_ALLOCATION':str(allocation),
             'SB_SAMPLE_BATCH':str(batch),'SB_SAMPLE_WAKEUP':str(wakeup),'SB_LIFECYCLE_WAKEUP':str(lifecycle),'SB_SAMPLE_WAKE_US':str(a.wake_us),'REPLAY_SEARCH_BACKEND':a.search_backend,
             'PHASE_COUNTERS':str(int(a.phase_counters)),'BIND_TOOL_NODE':str(a.bind_tool_node) if a.bind_tool_node is not None else '-1','SB_PLACEMENT':str(a.placement),'SB_LOAD_PERIOD':str(a.load_period),'REPLAY_TIMEOUT_S':'1800'}
        cmd=['bash',str(H/'run_single.sh'),'phase',case['golden'],case['trace'],str(output.resolve()),
             '1280','4096','0','0',str(job['speed'])]
        print('BEGIN',job['name'],flush=True); start=time.monotonic()
        with (a.output/(job['name']+'.launch.log')).open('w') as log:
            status=subprocess.run(cmd,env=env,cwd=W,stdout=log,stderr=subprocess.STDOUT).returncode
        receipt.write_text(json.dumps({'job':job,'exit_code':status,'wall_s':time.monotonic()-start},indent=2)+'\n')
        print('DONE',job['name'],status,round(time.monotonic()-start,1),flush=True)
    (a.output/'runner-finished.json').write_text(json.dumps({'jobs':len(jobs),'finished_at':time.time()})+'\n')

if __name__=='__main__':main()
