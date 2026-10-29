#!/usr/bin/env python3
"""SSD page-placement ablations at identical immutable phase capacities."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback
H=Path(__file__).resolve().parent
W=H.parents[1]
sys.path.insert(0,str(H))
from analyze import audit
from controller import PhaseQuota
def read(path):
    return json.loads(Path(path).read_text())

def write(path, data):
    path = Path(path)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, indent=2) + '\n')
    temp.replace(path)

def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(8 << 20), b''):
            h.update(block)
    return h.hexdigest()

def background():
    vms={}
    for cmdline in Path('/proc').glob('[0-9]*/cmdline'):
        try:
            args=cmdline.read_bytes().split(b'\0')
            if args and args[0].endswith(b'/cloud-hypervisor'):
                fields=(cmdline.parent/'stat').read_text().rsplit(')',1)[1].split()
                vms[cmdline.parent.name]=dict(command=[s.decode(errors='replace') for s in args if s],
                    start_ticks=int(fields[19]),cpu_ticks=int(fields[11])+int(fields[12]))
        except (OSError,ValueError):
            pass
    return dict(vms=vms,loadavg=Path('/proc/loadavg').read_text().strip(),
                memory_psi=Path('/proc/pressure/memory').read_text(),
                io_psi=Path('/proc/pressure/io').read_text())

def cases():
    main = read(W/'results/capacity-v2-20261009/catalog.json')['primary'][:3]
    extra = read(W/'results/generalization-v45-20261019/inputs/catalog.json')['primary']
    # The reviewed replayer checks Read-before-Edit for all six extension traces.
    return [dict(c, guard=i >= 3) for i, c in enumerate(main + extra)]

def execute(root, case, label, lane, medium='ssd', quota=None, timeout=1800,topology_control=False, mode='native'):
    name = case['name'] + '/' + label
    folder = root/name
    folder.parent.mkdir(parents=True, exist_ok=True)
    phase = quota is not None
    artifact = H.parent/'artifacts'/('vmm-v54-ssd-placement-readguard' if case['guard'] else 'vmm-v54-ssd-placement')
    settings = root/'settings'/case['name']/(label+'.json')
    settings.parent.mkdir(parents=True, exist_ok=True)
    write(settings, dict(capacity=quota or dict(active_mib=6144, wait_mib=6144),placement=dict(mode=mode)))
    runtime = Path('/tmp')/('sp54-'+hashlib.sha256(str(root).encode()).hexdigest()[:10]+f'-l{lane}')
    runtime.mkdir(parents=True, mode=0o700, exist_ok=True)
    env = dict(os.environ, VM_LANE=str(lane), TMUX_TMPDIR=str(runtime),
        ROOT_DISK=str(W/'validated-root-disks/root-demeter.qcow2'),
        PHASE_ARTIFACT_DIR=str(artifact), COLD_MEDIA=medium,
        BASELINE_POLICY='phase' if phase else 'dram',
        HOST_FAST_NUMA_NODE='0', HOST_SLOW_NUMA_NODE='2' if (phase or topology_control) and medium=='cxl' else '0',
        VMM_PRIVATE_NODE='0', GUEST_SYSTEM_TIER='fast', GUEST_CPU_WITH_FAST='1',
        FAST_RAW_MIB='8192', SLOW_RAW_MIB='8192', FREE_PAGE_REPORTING='on',
        PAGE_REPORTING_ORDER='5', WAIT_REPORTING_ORDER='0' if phase else '-1',
        BARRIER_REPORTING_START='1' if phase else '0', CAPACITY_POLICY='static',
        DEMETER_REPORTING_RECEIPTS='1', PHASE_BUDGET_MIB='6144', STARTUP_HEADROOM_MIB='0',
        SB_FAST_NODE='0', SB_SLOW_NODE='1', SB_ALLOCATION='1', SB_PLACEMENT='1',
        SB_RECYCLE_CREDIT='1', SB_SAMPLE_BATCH='1', SB_SAMPLE_WAKEUP='0',
        SB_LIFECYCLE_WAKEUP='0', SB_SAMPLING='0', SB_FUTURE_BENEFIT='1',
        PHASE_NO_EVENTS='0', REPLAY_TIMING='gaps', REPLAY_TIMEOUT_S=str(timeout),
        REPLAY_SEARCH_BACKEND='ripgrep', PHASE_COUNTERS='0', BIND_TOOL_NODE='-1',
        PYTEST_COMPAT='1' if case['name']=='dask-5510' else '0',
        ADAPTIVE_MODE='1' if phase else '0', LATENCY_LIMIT='.2', ADAPTIVE_INITIAL_MIB='0',
        SSD_ACTIVE_HEADROOM_MIB='0', REFERENCE_PROFILE=str(settings),
        SSD_PAGE_CLUSTER='3' if phase else '0', SSD_CHUNK_MIB='32', ATTACH_COLD_DISK='1')
    env.pop('TMUX', None)
    env.pop('TMUX_PANE', None)
    command = ['bash', str(H/'run_single.sh'), 'phase', case['golden'], case['trace'],
               str(folder), '6144', '6144', '0', '0', '1']
    job = dict(name=name, trace=case['name'], label=label, lane=lane, quota=quota,
        topology_control=topology_control,placement_mode=mode,
        timeout_s=timeout, config=dict(media=medium, phase=phase, baseline=not phase,
        zones=[0, 2 if (phase or topology_control) and medium=='cxl' else 0], quota=(quota or {}).get('active_mib',6144)))
    write(root/(name+'.job.json'), job)
    start = time.time()
    before=background()
    with (root/(name+'.log')).open('x') as log:
        process = subprocess.run(command, cwd=W, env=env, stdout=log, stderr=subprocess.STDOUT)
    write(root/(name+'.receipt.json'), dict(exit_code=process.returncode,
        started_epoch=start, finished_epoch=time.time(), wall_s=time.time()-start,
        background_before=before,background_after=background()))
    row = audit(root, case, job)
    write(root/(name+'.audit.json'), row)
    print('AUDITED', case['name'], label, 'valid='+str(row['valid']),
          'e2e='+str(row.get('e2e_s')), 'reasons='+str(row.get('reasons')), flush=True)
    return row


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--root',type=Path,required=True)
    p.add_argument('--traces',nargs='+',required=True)
    p.add_argument('--lanes',nargs='+',type=int,required=True)
    p.add_argument('--repeats',type=int,default=1)
    p.add_argument('--modes',nargs='+',default=['native','cold','cold-cache'],choices=['native','cold','cold-cache'])
    p.add_argument('--capacities',type=Path)
    a=p.parse_args()
    root=a.root.resolve();root.mkdir(parents=True,exist_ok=True)
    if (root/'campaign-plan.json').exists():p.error('fresh output directory required')
    if not a.lanes or any(l not in range(1,8) for l in a.lanes) or len(set(a.lanes))!=len(a.lanes):p.error('invalid lanes')
    selected=[c for c in cases() if c['name'] in a.traces]
    if set(a.traces)!={c['name'] for c in selected}:p.error('unknown trace')
    settings=read(a.capacities) if a.capacities else {c['name']:[dict(active_mib=6144,wait_mib=16)] for c in selected}
    for c in selected:
        for q in settings[c['name']]:PhaseQuota(**q)
    files=[f for f in H.iterdir() if f.suffix in ('.py','.sh')]
    for name in ['vmm-v54-ssd-placement','vmm-v54-ssd-placement-readguard']:
        files += [f for f in (H.parent/'artifacts'/name).rglob('*') if f.is_file()]
    files += [H.parent/'cold_media/analyze.py',H.parent/'active-policy.json']
    for c in selected:
        files += [Path(c['trace'])/'tool_calls.json']
        if (Path(c['trace'])/'trace.jsonl').exists():files.append(Path(c['trace'])/'trace.jsonl')
    plan=dict(version='ssd-placement-v54',traces=selected,capacities=settings,modes=a.modes,
              repeats=a.repeats,lanes=a.lanes,files={str(f.resolve()):sha(f) for f in files},
              timing='gaps',speed=1,latency_limits_offline=[.05,.10,.15,.20],
              upper='immutable ACTIVE/WAIT pair; no lower capacity override',
              ablation='native versus scoped cold pages versus scoped cold pages plus explicitly expanded Guest file-cache scope',
              background=background())
    write(root/'campaign-plan.json',plan)
    def verify():
        for name,expected in plan['files'].items():
            if sha(name)!=expected:raise RuntimeError('frozen input drift: '+name)
    def block(task):
        c,repeat,lane=task
        state=dict(stage='running',trace=c['name'],repeat=repeat,lane=lane,completed=[])
        status=root/c['name']/f'r{repeat}-status.json';status.parent.mkdir(parents=True,exist_ok=True)
        write(status,state)
        try:
            verify()
            base=execute(root,c,f'r{repeat}-dram',lane,timeout=1800)
            state['completed'].append(base);write(status,state)
            if not base['valid']:raise RuntimeError('invalid fresh DRAM: '+str(base['reasons']))
            arms=[(q,m) for q in settings[c['name']] for m in a.modes]
            shift=(repeat-1)%len(arms);arms=arms[shift:]+arms[:shift]
            for quota,mode in arms:
                if (root/'stop-after-current').exists():raise InterruptedError('stop after current')
                verify()
                label=f"r{repeat}-{mode}-a{quota['active_mib']}-w{quota['wait_mib']}"
                state['current']=label;write(status,state)
                row=execute(root,c,label,lane,quota=quota,mode=mode,timeout=math.ceil(1.5*base['e2e_s']+30))
                row['baseline_label']=f'r{repeat}-dram'
                if row.get('complete') and json.dumps(row.get('signature'),sort_keys=True)!=json.dumps(base.get('signature'),sort_keys=True):
                    row['reasons'].append('workload signature differs from fresh DRAM');row['valid']=False
                write(root/(row['name']+'.audit.json'),row)
                state['completed'].append(row);write(status,state)
                if not row['valid'] and not row.get('censored'):raise RuntimeError('invalid candidate: '+str(row['reasons']))
            state.update(stage='finished',finished_epoch=time.time());write(status,state)
        except BaseException as e:
            state.update(stage='failed',error=repr(e),traceback=traceback.format_exc());write(status,state)
        return state
    # Each lane runs a queue; different repeats may occupy different lanes,
    # while every arm is paired with its own same-lane fresh baseline.
    tasks=[(c,r) for r in range(1,a.repeats+1) for c in selected]
    def lane_worker(lane,queue):return [block((c,r,lane)) for c,r in queue]
    state=dict(stage='running',pid=os.getpid(),started_epoch=time.time(),completed=[])
    write(root/'status.json',state)
    with ThreadPoolExecutor(max_workers=len(a.lanes)) as pool:
        futures=[pool.submit(lane_worker,lane,tasks[i::len(a.lanes)]) for i,lane in enumerate(a.lanes)]
        for f in as_completed(futures):
            state['completed']+=f.result();write(root/'status.json',state)
    state.update(stage='finished' if all(s['stage']=='finished' for s in state['completed']) else 'failed',finished_epoch=time.time())
    write(root/'status.json',state)
    if state['stage']!='finished':raise SystemExit(1)


if __name__=='__main__':main()
