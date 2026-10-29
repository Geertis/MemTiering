#!/usr/bin/env python3
"""Frozen, fresh-VM baseline comparisons. Failed directories are never replaced."""
import argparse
import hashlib
import json
import os
import random
import shutil
import subprocess
import time
from pathlib import Path

W=Path(__file__).resolve().parents[2]
H=Path(__file__).resolve().parent
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--root',type=Path,required=True)
    p.add_argument('--traces',nargs='+')
    p.add_argument('--speed',type=float,default=10)
    p.add_argument('--repeats',type=int,default=2)
    p.add_argument('--policies',nargs='+',default=['dram','cxl'],choices=['dram','cxl'])
    p.add_argument('--static-fast-weight',type=int,default=1)
    p.add_argument('--static-slow-weight',type=int,default=4)
    p.add_argument('--plan-only',action='store_true')
    a=p.parse_args();R=a.root.resolve();A=W/'phase/artifacts/baseline-v9-whole-ram'
    assert os.uname().release=='6.10.0-demeterhost-loadfb-20261005'
    assert a.repeats>0 and a.speed>0
    cases=json.loads((W/'results/capacity-v2-20261009/catalog.json').read_text())['primary']
    if a.traces:
        assert set(a.traces)<={c['name'] for c in cases}
        cases=[c for c in cases if c['name'] in a.traces]
    for c in cases:
        assert Path(c['golden']).exists()
        for n,expected in c['input_hashes'].items():assert sha(Path(c['trace'])/n)==expected
    rng=random.Random(20261009);jobs=[]
    for repeat in range(1,a.repeats+1):
        ordered=cases[:];rng.shuffle(ordered)
        for c in ordered:
            order=a.policies[:]
            if repeat%2==0:order.reverse()
            if repeat==3:order=order[1:]+order[:1]
            for policy in order:
                budget=64 if c['name'] in ['streamlink-3485','github3-673'] else 256
                jobs.append({'trace':c['name'],'policy':policy,'strategy':policy,'budget_mib':budget,
                    'repeat':repeat,'speed':a.speed,'guest_tool_bind_node':0,
                    'host_ram_node':0 if policy=='dram' else 2,
                    'name':f"{c['name']}-{policy}-r{repeat}"})
    sources=[H/n for n in ['run_whole_matrix.py','run_whole_single.sh','host_monitor_whole.py','guest_state.py','startup_monitor.py','policy_exec.c']]
    protocol={'version':3,'host_kernel':os.uname().release,'artifact':str(A),'jobs':jobs,'cases':cases,
        'serial':True,'speed':a.speed,'repeats':a.repeats,'guest_fast_mib':6144,'guest_slow_mib':6144,
        'raw_zones_mib':[8192,8192],'static_weights':[a.static_fast_weight,a.static_slow_weight],
        'free_page_reporting':'on','page_reporting_order':9,'phase_counters':False,
        'policies':{'dram':'MPOL_BIND Guest node0; both RAM zones Host node0; no placement/controller',
            'cxl':'MPOL_BIND Guest node0; both RAM zones Host node2; no placement/controller'},
        'scope':'ALL Guest RAM mapped to Host DDR0 or CXL2; no placement/controller; private VMM/kernel costs remain; no density claim',
        'files':{str(x):sha(x) for x in sources},'artifact_files':json.loads((A/'SHA256SUMS.json').read_text())}
    R.mkdir(parents=True,exist_ok=True);saved=R/'plan.json'
    if saved.exists():assert json.loads(saved.read_text())==protocol,'protocol drift'
    else:
        saved.write_text(json.dumps(protocol,indent=2)+'\n')
        archive=R/'protocol';archive.mkdir()
        for x in sources:shutil.copy2(x,archive/x.name)
        (archive/'SHA256SUMS.json').write_text(json.dumps({x.name:sha(x) for x in sources},indent=2)+'\n')
    print(json.dumps({'planned':len(jobs),'speed':a.speed,'root':str(R)}),flush=True)
    if a.plan_only:return
    base={**os.environ,'VM_LANE':'0','ROOT_DISK':str(W/'validated-root-disks/root-demeter.qcow2'),
        'PHASE_ARTIFACT_DIR':str(A),'FREE_PAGE_REPORTING':'on','PAGE_REPORTING_ORDER':'9',
        'FAST_RAW_MIB':'8192','SLOW_RAW_MIB':'8192','CAPACITY_POLICY':'static','REPLAY_TIMEOUT_S':'1800',
        'REPLAY_SEARCH_BACKEND':'ripgrep','PHASE_COUNTERS':'0','BIND_TOOL_NODE':'-1',
        'SB_SAMPLE_WAKE_US':'2000','STATIC_FAST_WEIGHT':str(a.static_fast_weight),
        'STATIC_SLOW_WEIGHT':str(a.static_slow_weight)}
    for job in jobs:
        receipt=R/(job['name']+'.receipt.json')
        if receipt.exists():
            assert json.loads(receipt.read_text())['exit_code']==0,'retained failure requires inspection'
            continue
        assert not (R/job['name']).exists(),'refuse overwrite incomplete run'
        for x,expected in protocol['files'].items():assert sha(Path(x))==expected,x
        for x,expected in protocol['artifact_files'].items():assert sha(A/x)==expected,x
        c=next(c for c in cases if c['name']==job['trace'])
        enabled=str(int(job['policy']=='phase'))
        env={**base,'HOST_RAM_NODE':str(job['host_ram_node']),'BASELINE_POLICY':'dram','PHASE_BUDGET_MIB':str(job['budget_mib']),
            **{k:enabled for k in ['SB_ALLOCATION','SB_SAMPLE_BATCH','SB_SAMPLE_WAKEUP','SB_LIFECYCLE_WAKEUP','SB_PLACEMENT']}}
        cmd=['bash',str(H/'run_whole_single.sh'),'phase',c['golden'],c['trace'],str(R/job['name']),
             '6144','6144','0','0',str(a.speed)]
        print('START',job['name'],flush=True);start=time.time()
        with (R/(job['name']+'.launch.log')).open('x') as log:
            result=subprocess.run(cmd,cwd=W,env=env,stdout=log,stderr=subprocess.STDOUT)
        receipt.write_text(json.dumps({'exit_code':result.returncode,'job':job,
            'started_epoch':start,'finished_epoch':time.time(),'wall_s':time.time()-start},indent=2)+'\n')
        print('END',job['name'],result.returncode,round(time.time()-start,2),flush=True)
        if result.returncode:raise SystemExit(result.returncode)
    (R/'finished.json').write_text(json.dumps({'planned':len(jobs),'epoch':time.time(),'exit_code':0})+'\n')

if __name__=='__main__':main()
