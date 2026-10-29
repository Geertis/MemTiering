#!/usr/bin/env python3
"""Two real independent VMMs. Observe concurrency, do not infer hard-pool density."""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

W=Path(__file__).resolve().parents[2];H=Path(__file__).resolve().parent
A=W/'phase/artifacts/baseline-v8-common'
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()

def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True)
    p.add_argument('--plan-only',action='store_true');a=p.parse_args();R=a.root.resolve()
    assert os.uname().release=='6.10.0-demeterhost-loadfb-20261005'
    cases=json.loads((W/'results/capacity-v2-20261009/catalog.json').read_text())['primary']
    cases=[next(c for c in cases if c['name']==name) for name in ['dask-5510','xonsh-haiku']]
    jobs=[]
    for schedule,offsets,policies in [
        ('same_start',[0,0],['dram','cxl','static','phase']),
        ('offset_180s',[0,180],['phase','static','cxl','dram'])]:
        for policy in policies:
            jobs.append({'name':f'{schedule}-{policy}','schedule':schedule,'policy':policy,
                'offsets_s':offsets,'traces':[c['name'] for c in cases], 'repeat':1})
    sources=[H/n for n in ['run_mixed.py','run_single.sh','host_monitor.py','guest_state.py','startup_monitor.py','policy_exec.c','joint_monitor.py']]
    plan={'host_kernel':os.uname().release,'jobs':jobs,'cases':cases,'speed':1,'vms_per_job':2,
        'guest_fast_mib':6144,'guest_slow_mib':6144,'raw_zones_mib':[8192,8192],
        'phase_budget_per_vm_mib':256,'static_weights':[1,4],'reporting':'on','order':9,
        'cpu_lanes':[[0,1,2,3],[4,5,6,7]],
        'scope':'two real traces; one fresh pair per configuration; observed costs, no physical hard pool or maximum N claim',
        'files':{str(x):sha(x) for x in sources},'artifact_files':json.loads((A/'SHA256SUMS.json').read_text())}
    R.mkdir(parents=True,exist_ok=True);path=R/'plan.json'
    if path.exists():assert json.loads(path.read_text())==plan
    else:
        path.write_text(json.dumps(plan,indent=2)+'\n');archive=R/'protocol';archive.mkdir()
        for x in sources:shutil.copy2(x,archive/x.name)
    print(json.dumps({'batches':len(jobs),'vms':2*len(jobs),'root':str(R)}),flush=True)
    if a.plan_only:return
    assert (R.parent/'single-vm-finished.json').exists(),'do not overlap single-VM timing'
    for job in jobs:
        receipt=R/(job['name']+'.receipt.json')
        if receipt.exists():assert json.loads(receipt.read_text())['exit_code']==0;continue
        out=R/job['name'];assert not out.exists();out.mkdir()
        for x,expected in plan['files'].items():assert sha(Path(x))==expected
        for x,expected in plan['artifact_files'].items():assert sha(A/x)==expected
        barrier=out/'barrier';barrier.mkdir();processes=[];logs=[];started=time.time()
        monitor_log=(out/'joint-monitor.log').open('x')
        monitor=subprocess.Popen(['sudo','-n',sys.executable,str(H/'joint_monitor.py'),'--root',str(out)],
            stdout=monitor_log,stderr=subprocess.STDOUT)
        print('START_PAIR',job['name'],flush=True)
        enabled=str(int(job['policy']=='phase'))
        try:
            # Boot sequentially to hold a live tmux server; workloads start at the barrier.
            for lane,c in enumerate(cases):
                env={**os.environ,'VM_LANE':str(lane),'ROOT_DISK':str(W/'validated-root-disks/root-demeter.qcow2'),
                    'PHASE_ARTIFACT_DIR':str(A),'FREE_PAGE_REPORTING':'on','PAGE_REPORTING_ORDER':'9',
                    'FAST_RAW_MIB':'8192','SLOW_RAW_MIB':'8192','CAPACITY_POLICY':'static',
                    'REPLAY_TIMEOUT_S':'1800','REPLAY_SEARCH_BACKEND':'ripgrep','PHASE_COUNTERS':'0','BIND_TOOL_NODE':'-1',
                    'SB_SAMPLE_WAKE_US':'2000','STATIC_FAST_WEIGHT':'1','STATIC_SLOW_WEIGHT':'4',
                    'BASELINE_POLICY':job['policy'],'PHASE_BUDGET_MIB':'256','REPLAY_BARRIER_DIR':str(barrier),
                    **{k:enabled for k in ['SB_ALLOCATION','SB_SAMPLE_BATCH','SB_SAMPLE_WAKEUP','SB_LIFECYCLE_WAKEUP','SB_PLACEMENT']}}
                log=(out/f'lane{lane}.launch.log').open('x');logs.append(log)
                cmd=['bash',str(H/'run_single.sh'),'phase',c['golden'],c['trace'],str(out/f'lane{lane}'),
                    '6144','6144','0','0','1']
                processes.append(subprocess.Popen(cmd,cwd=W,env=env,stdout=log,stderr=subprocess.STDOUT))
                deadline=time.monotonic()+150
                while not (barrier/f'lane{lane}.ready').exists():
                    if processes[-1].poll() is not None:raise RuntimeError('pre-barrier runner failed')
                    if time.monotonic()>deadline:raise TimeoutError('Guest barrier readiness')
                    time.sleep(.2)
            anchor=time.monotonic();release=[]
            for lane,offset in enumerate(job['offsets_s']):
                while time.monotonic()<anchor+offset:
                    if any(q.poll() not in [None,0] for q in processes):raise RuntimeError('runner failed while offset pending')
                    time.sleep(min(.1,anchor+offset-time.monotonic()))
                (barrier/f'lane{lane}.go').write_text(json.dumps({'host_monotonic_ns':time.monotonic_ns(),'epoch':time.time()})+'\n')
                release.append({'lane':lane,'offset_s':offset,'epoch':time.time(),'host_monotonic_ns':time.monotonic_ns()})
            (out/'release-times.json').write_text(json.dumps(release,indent=2)+'\n')
            codes=[q.wait() for q in processes]
            (out/'joint-stop').touch()
            monitor_code=monitor.wait(timeout=10)
            for lane,(c,code) in enumerate(zip(cases,codes)):
                lane_job={'trace':c['name'],'policy':job['policy'],'strategy':job['policy'],
                    'repeat':1,'budget_mib':256,'speed':1,'name':f'lane{lane}'}
                (out/f'lane{lane}.receipt.json').write_text(json.dumps({'job':lane_job,
                    'exit_code':code,'pair_started_epoch':started,'finished_epoch':time.time()})+'\n')
            receipt.write_text(json.dumps({'exit_code':max(codes),'runner_codes':codes,'job':job,
                'monitor_code':monitor_code,'started_epoch':started,'finished_epoch':time.time()},indent=2)+'\n')
            print('END_PAIR',job['name'],codes,flush=True)
            if any(codes):raise SystemExit(max(codes))
            if monitor_code:raise RuntimeError('joint observer failed')
        finally:
            (out/'joint-stop').touch()
            if monitor.poll() is None:monitor.wait(timeout=10)
            monitor_log.close()
            # Shell EXIT traps own VM disks, services and children. Do not affect other VMMs.
            for q in processes:
                if q.poll() is None:q.terminate()
            for q in processes:
                if q.poll() is None:
                    try:q.wait(timeout=45)
                    except subprocess.TimeoutExpired:q.kill();q.wait()
            for log in logs:log.close()
    (R/'finished.json').write_text(json.dumps({'batches':len(jobs),'vms':2*len(jobs),'epoch':time.time()})+'\n')

if __name__=='__main__':main()
