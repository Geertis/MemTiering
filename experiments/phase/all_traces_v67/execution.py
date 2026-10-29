"""V57 launch path: reviewed environments unchanged; new images use their packaged testbed environment."""
from contextlib import contextmanager
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from isolation import run_isolated,check as check_isolation

H=Path(__file__).resolve().parent
W=H.parents[1]
FROZEN=W/'phase/ssd_placement_v54'
sys.path.insert(0,str(FROZEN))
from analyze import audit as physical_audit
from study import background,read,sha

def write(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+'.tmp');tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n');tmp.replace(path)


ROOT=W/'results/all-traces-v67-20261026'
QUOTA=dict(active_mib=6144,wait_mib=16)
PROFILES=['dram','compact-dram','stock','cxl-layout','ssd','cxl']


def audit(root,case,job):
    row=physical_audit(root,case,job)
    if row.get('complete'):
        package_path=root/job['name']/'guest-out/environment-packages.json'
        if package_path.exists():
            packages=read(package_path)
            row['dependency_fingerprint']=hashlib.sha256(json.dumps(packages,sort_keys=True).encode()).hexdigest()
            row['signature']['environment_packages_sha256']=row['dependency_fingerprint']
        else:
            row['reasons'].append('missing post-run dependency inventory');row['valid']=False
        points=[json.loads(line) for line in (root/job['name']/'host-capacity.jsonl').open()]
        init=next(p for p in points if p['event']=='init')
        sizes=[z['size'] for z in init['info']['config']['memory']['zones']]
        expected=job['raw_mib_per_zone']*(1<<20)
        if sizes!=[expected,expected]:row['reasons'].append('raw geometry differs from preregistered profile')
        row['actual_raw_zone_bytes']=sizes
        balloon=(job['raw_mib_per_zone']-6144)*(1<<20)
        if init['nominal_balloon']!=[balloon,balloon]:row['reasons'].append('nominal usable capacity differs from 6144+6144 MiB')
        boot=(root/job['name']/'boot-footprint.txt').read_text()
        if ('sysctl.vm.mem_profiling=never' in boot)!=job['compact_geometry']:row['reasons'].append('generic allocation-profiling boot mode differs from profile')
        row['reasons']=sorted(set(row['reasons']));row['valid']=not row['reasons']
    return row


def execute(root,case,label,lane,profile,timeout=1800,barrier=None):
    if profile not in PROFILES:raise ValueError(profile)
    medium='cxl' if profile in ['cxl','cxl-layout'] else 'ssd'
    phase=profile not in ['dram','compact-dram','cxl-layout'];topology=profile=='cxl-layout'
    compact=profile not in ['dram','stock']
    mode='cold-cache' if medium=='ssd' and phase else 'native'
    quota=dict(QUOTA) if phase else None
    name=case['name']+'/'+label;folder=root/name
    folder.parent.mkdir(parents=True,exist_ok=True)
    artifact=W/'phase/artifacts'/(('vmm-v66-ssd-compact' if compact else 'vmm-v65-ssd-scope')+('-readguard' if case['guard'] else ''))
    if compact and medium=='cxl':artifact=W/'phase/artifacts'/('vmm-v67-compact-cxl'+('-readguard' if case['guard'] else ''))
    settings=root/'settings'/case['name']/(label+'.json');settings.parent.mkdir(parents=True,exist_ok=True)
    write(settings,dict(capacity=quota or dict(active_mib=6144,wait_mib=6144),placement=dict(mode=mode,fast_wait_cache=profile=='ssd')))
    runtime=Path('/tmp')/('ac67-'+hashlib.sha256(str(root).encode()).hexdigest()[:10]+f'-l{lane}')
    runtime.mkdir(parents=True,mode=0o700,exist_ok=True)
    env=dict(PHASE_TIEING_ROOT=str(W.parent),HOME='/r0/home/geerties',PATH='/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',LANG='C.UTF-8',VM_LANE=str(lane),TMUX_TMPDIR=str(runtime),
        ROOT_DISK=str(W/'validated-root-disks/root-demeter.qcow2'),PHASE_ARTIFACT_DIR=str(artifact),
        COLD_MEDIA=medium,BASELINE_POLICY='phase' if phase else 'dram',
        HOST_FAST_NUMA_NODE='0',HOST_SLOW_NUMA_NODE='2' if medium=='cxl' else '0',
        VMM_PRIVATE_NODE='0',GUEST_SYSTEM_TIER='fast',GUEST_CPU_WITH_FAST='1',
        FAST_RAW_MIB='6144' if compact else '8192',SLOW_RAW_MIB='6144' if compact else '8192',FREE_PAGE_REPORTING='on',
        PAGE_REPORTING_ORDER='5',WAIT_REPORTING_ORDER='0' if phase else '-1',
        BARRIER_REPORTING_START='1' if phase else '0',CAPACITY_POLICY='static',
        DEMETER_REPORTING_RECEIPTS='1',PHASE_BUDGET_MIB='6144',STARTUP_HEADROOM_MIB='0',
        SB_FAST_NODE='0',SB_SLOW_NODE='1',SB_ALLOCATION='1',SB_PLACEMENT='1',
        SB_RECYCLE_CREDIT='1',SB_SAMPLE_BATCH='1',SB_SAMPLE_WAKEUP='0',
        SB_LIFECYCLE_WAKEUP='0',SB_SAMPLING='0',SB_FUTURE_BENEFIT='1',
        REPLAY_USE_IMAGE_TEST_ENV='1' if case.get('tool_environment_override')=='image-testbed' or not case.get('reviewed_environment') else '0',
        PHASE_NO_EVENTS='0',REPLAY_TIMING='gaps',REPLAY_TIMEOUT_S=str(timeout),
        REPLAY_SEARCH_BACKEND='ripgrep',PHASE_COUNTERS='0',BIND_TOOL_NODE='-1',
        PYTEST_COMPAT='1' if case.get('pytest_compat') else '0',
        ADAPTIVE_MODE='1' if phase else '0',LATENCY_LIMIT='.2',ADAPTIVE_INITIAL_MIB='0',
        SSD_ACTIVE_HEADROOM_MIB='0',REFERENCE_PROFILE=str(settings),
        SSD_PAGE_CLUSTER='3' if phase else '0',SSD_CHUNK_MIB='32',ATTACH_COLD_DISK='1')
    env.pop('TMUX',None);env.pop('TMUX_PANE',None)
    if barrier is not None:env.update(REPLAY_BARRIER_DIR=str(barrier),REPLAY_FINISH_BARRIER_DIR=str(barrier))
    env['REPLAY_NETWORK_PROXY']='1' if case.get('network_repair') else '0'
    env['V63_PACKAGE_PROXY_HELPER']=str(H/'package_proxy.py')
    env.update(REPLAY_READY_TIMEOUT_S='600',REPLAY_EXPORT_TIMEOUT_S=str(timeout+600))
    env['REPLAY_CREATE_SCRATCHPAD']='1' if case.get('restore_scratchpad') else '0'
    job=dict(name=name,trace=case['name'],label=label,lane=lane,profile=profile,
        quota=quota,topology_control=topology,placement_mode=mode,timeout_s=timeout,
        experiment_version='all-traces-v67',compact_geometry=compact,nominal_mib_per_zone=6144,raw_mib_per_zone=6144 if compact else 8192,fast_wait_cache=profile=='ssd',config=dict(media=medium,phase=phase,baseline=not phase,zones=[0,2 if medium=='cxl' else 0],quota=6144))
    write(root/(name+'.job.json'),job)
    before=background();started=time.time()
    cmd=['bash',str(H/'run_single.sh'),'phase',case['golden'],case['trace'],str(folder),'6144','6144','0','0','1']
    with (root/(name+'.log')).open('x') as log:
        process=run_isolated(cmd,cwd=W,env=env,stdout=log,lane=lane,folder=folder)
    write(root/(name+'.receipt.json'),dict(exit_code=process.returncode,started_epoch=started,
        finished_epoch=time.time(),wall_s=time.time()-started,background_before=before,background_after=background()))
    row=audit(root,case,job)
    isolation_errors,isolation=check_isolation(folder,lane)
    row['host_isolation']=isolation
    row['reasons']=sorted(set(row['reasons']+isolation_errors))
    row['valid']=not row['reasons']
    if row.get('complete'):
        data=read(folder/'guest-out/result.json')
        calls=read(Path(case['trace'])/'tool_calls.json')
        row['phase_overhead_s']=sum(s.get('phase_overhead_s',0) for s in data['steps'] if not s.get('skipped'))
        row['test_binary_missing']=any(s.get('tool')=='Bash' and 'pytest' in str(calls[s['source_index']].get('input',{}).get('command','')) and
            ('pytest: command not found' in s.get('stderr_tail','') or 'No module named pytest' in s.get('stderr_tail','')) for s in data['steps'])
        row['runtime_phase_errors']=data.get('phase_runtime_errors')
        row['tool_environment']=read(folder/'guest-out/tool-environment.json')
    write(root/(name+'.audit.json'),row)
    print('AUDITED',name,'valid='+str(row['valid']),'e2e='+str(row.get('e2e_s')),str(row['reasons']),flush=True)
    return row


def archive(root):
    """Run only at a no-VM barrier; lossless compression and content dedup."""
    store=ROOT/'shared/sha256';store.mkdir(parents=True,exist_ok=True)
    for folder in sorted(p for p in root.rglob('protocol-inputs') if p.is_dir()):
        for source in folder.iterdir():
            if not source.is_file():continue
            digest=sha(source);shared=store/digest
            if not shared.exists():
                shutil.copy2(source,shared);shared.chmod(0o444)
            if source.stat().st_ino!=shared.stat().st_ino:
                temporary=source.with_name(source.name+'.dedup');os.link(shared,temporary);temporary.replace(source)
    journal=[]
    for p in sorted(root.rglob('*')):
        if not p.is_file() or 'protocol-inputs' in p.parts or p.stat().st_size<128*1024:continue
        if p.suffix not in ['.jsonl','.json','.log']:continue
        # Keep campaign plans/status and source input authorities directly readable.
        if p.name in ['campaign-plan.json','status.json'] or p.name.endswith('.audit.json'):continue
        digest=sha(p);target=p.with_name(p.name+'.gz')
        if target.exists():raise RuntimeError('compression target exists '+str(target))
        with p.open('rb') as source,gzip.open(target,'wb',compresslevel=3) as output:shutil.copyfileobj(source,output)
        with gzip.open(target,'rb') as f:
            h=hashlib.sha256()
            for b in iter(lambda:f.read(1<<20),b''):h.update(b)
        if h.hexdigest()!=digest:raise RuntimeError('compression altered content '+str(p))
        journal.append(dict(path=str(p),sha256=digest,compressed=str(target),compressed_sha256=sha(target),bytes=p.stat().st_size))
        p.unlink()
    if journal:
        log=root/'lossless-archives.jsonl'
        with log.open('a') as f:
            for row in journal:f.write(json.dumps(row)+'\n')


@contextmanager
def restore_for_audit(folder):
    restored=[]
    try:
        for p in folder.rglob('*.gz'):
            if p.suffix!='.gz':continue
            target=p.with_suffix('')
            if target.exists():continue
            with gzip.open(p,'rb') as source,target.open('xb') as out:shutil.copyfileobj(source,out)
            restored.append(target)
        yield
    finally:
        for p in restored:p.unlink()
