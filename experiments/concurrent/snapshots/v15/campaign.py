"""Durable serial cohort coordinator for the approved P1 formal experiment."""
import fcntl
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
from outer import ROOT, W, KEY, PORT, ssh, start, stop, save
from deploy import transfer, provision
from audit import audit, read

H = Path(__file__).resolve().parent
VERIFIED = set()


def update(stage, **values):
    path = ROOT / 'status.json'
    state = read(path)
    state.update(stage=stage, updated_epoch=time.time(), **values)
    save(path, state)
    report=Path('/r0/home/geerties/HW/report/261029PhaseTieing并发超售正式实验进展.md')
    report.write_text('# PhaseTieing 从无超售开始的并发实验进展\n\n'
        f"更新时间：{time.strftime('%Y-%m-%d %H:%M:%S %z')}；阶段：`{stage}`；当前组：`{state.get('active')}`。\n\n"
        'L1：36 vCPU、96 GiB DRAM，禁用 swap；每 L2：4 vCPU、12 GiB 名义内存、一条完整轨迹。'
        '最多八个 L2 分别使用 CPU 0–31 内互不重叠的四核池；宿主控制面使用 CPU 32–35。\n\n'
        f"已完整审计通过 {len(state.get('completed', []))} 组；异常 {len(state.get('failed', []))} 组。"
        '先做单实例峰值与自然完成标定，再做 N=4、N=8 的无超售混合并发。\n\n'
        '主容量判据为 L1 OOM；L2 OOM、VMM 退出、业务正确性与时延分开报告。工具、轨迹和整组均无执行时限。'
        '任一异常停止后续点，保存诊断并检查原因；系统问题修复后使用新标签重测。\n\n'
        f'本批数据：`{ROOT}`。旧 v1/v2 不恢复、不合并。\n')
    print(time.strftime('%Y-%m-%d %H:%M:%S'), stage, values.get('active', ''), flush=True)


def source_guard():
    for name, expected in read(ROOT / 'source-manifest.json').items():
        data = Path(name).read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise RuntimeError('frozen source drift: ' + name)


def host_audit(label):
    state = read(ROOT / label / 'outer.json')
    pid = state['pid']
    result = dict(epoch=time.time(), pid=pid, outer=state)
    status = Path(f'/proc/{pid}/status').read_text()
    result['status'] = status
    result['numa_maps'] = subprocess.check_output(['sudo','-n','cat',f'/proc/{pid}/numa_maps'],text=True)
    result['affinity'] = {p.name: (p / 'status').read_text().split('Cpus_allowed_list:')[1].splitlines()[0].strip()
        for p in Path(f'/proc/{pid}/task').iterdir() if (p / 'status').exists()}
    assert 'VmSwap:\t       0 kB' in status or int(status.split('VmSwap:')[1].split()[0]) == 0
    if int(status.split('VmLck:')[1].split()[0])*1024 < (state['dram_gib']+state['cxl_gib'])*(1<<30):
        raise RuntimeError('outer RAM is not fully locked')
    wanted = {1, 2} if state['cxl_gib'] else {1}
    for line in result['numa_maps'].splitlines():
        # The fixed-size RAM mappings are anonymous, at least one GiB each;
        # their exact coverage is independently recorded by QMP and smaps.
        if 'bind:' in line:
            nodes={int(p.split('=')[0][1:]) for p in line.split() if p.startswith('N') and '=' in p}
            if not nodes <= wanted:
                raise RuntimeError('outer backing escaped declared physical nodes')
    save(ROOT / label / 'l0-backing-audit.json', result)
    return result


def prepare_l1(spec):
    source_guard()
    bootstrap=ROOT/'bootstrap-ready.json'
    if bootstrap.exists() and read(bootstrap)['label']==spec['label']:
        bootstrap.rename(ROOT/'bootstrap-consumed.json')
    else:
        start(spec['label'], spec['dram_gib'], spec['cxl_gib'])
    host_audit(spec['label'])
    provision()
    command = f'''set -e
test -f {ROOT}/protocol.json
sudo mkdir -p /pilot-swap
if [ {int(spec['medium']=='ssd' and spec['strategy'] in ['P','F'])} = 1 ]; then
  sudo mount /dev/vdc /pilot-swap
  sudo chown hs:hs /pilot-swap
fi
sudo python3 - <<'CLEAN'
from pathlib import Path
root=Path('{W}')
assert (Path('{ROOT}')/'protocol.json').exists()
for p in root.glob('.scratch-*'):
 if p.is_file() and p.suffix in ['.ext4','.qcow2']:p.unlink()
for p in Path('/pilot-swap').glob('.scratch-swap-*.raw'):
 if p.is_file():p.unlink()
CLEAN
'''
    ssh(command, timeout=120, check=True)


def retrieve(label):
    remote_folder=ROOT/'cohorts'/label
    command=f'''sudo python3 - <<'COMPRESS'
from pathlib import Path
import gzip,shutil,os,json,hashlib
folder=Path('{remote_folder}')
for path in folder.rglob('*.jsonl'):
 if 'protocol-inputs' in path.parts:continue
 if path.stat().st_size:
  with path.open('rb') as f:
   f.seek(max(0,path.stat().st_size-1048576));tail=f.read()
  if not tail.endswith(b'\\n'):
   last=tail.rsplit(b'\\n',1)[-1]
   try:json.loads(last)
   except (ValueError,UnicodeDecodeError):
    size=path.stat().st_size
    with path.open('rb') as f:digest=hashlib.file_digest(f,'sha256').hexdigest()
    path.with_name(path.name+'.partial-tail.bin').write_bytes(last)
    path.with_name(path.name+'.interruption.json').write_text(json.dumps(dict(original_bytes=size,
        original_sha256=digest,removed_partial_tail_bytes=len(last))))
    with path.open('r+b') as f:f.truncate(size-len(last))
 temp=path.with_name(path.name+'.gz.tmp')
 with path.open('rb') as src,gzip.open(temp,'wb',compresslevel=3) as dst:shutil.copyfileobj(src,dst)
 temp.replace(str(path)+'.gz');path.unlink()
COMPRESS
sudo chown -R hs:hs {remote_folder}
tar --exclude='*/protocol-inputs/*' -C {ROOT}/cohorts -cf - {shlex.quote(label)}
'''
    producer=subprocess.Popen(['ssh','-i',str(KEY),'-p',str(PORT),'-o','StrictHostKeyChecking=no',
        '-o','UserKnownHostsFile=/dev/null','hs@127.0.0.1',command],stdout=subprocess.PIPE,
        stderr=(ROOT/(label+'.retrieve.log')).open('w'))
    receiver=subprocess.run(['tar','--no-same-owner','-C',str(ROOT/'cohorts'),'-xf','-'],stdin=producer.stdout)
    producer.stdout.close()
    code=producer.wait()
    if code or receiver.returncode:raise RuntimeError(('archive transfer',label,code,receiver.returncode))
    folder=ROOT/'cohorts'/label
    for manifest_path in folder.glob('slot-*/manifest.json'):
        target=manifest_path.parent/'protocol-inputs';target.mkdir(exist_ok=True)
        for original,expected in read(manifest_path)['files'].items():
            source=Path(original.replace('/pilot/demeter/', '/r0/home/geerties/HW/Demeter/'))
            blob=ROOT/'_input_blobs'/expected
            if not blob.exists():
                digest=None
                if source.exists():
                    with source.open('rb') as stream:
                        digest=hashlib.file_digest(stream,'sha256').hexdigest()
                if digest==expected:
                    shutil.copy2(source,blob)
                else:
                    # Preserve an earlier adapter snapshot even if the working
                    # source was subsequently corrected before formal freeze.
                    with blob.open('xb') as stream:
                        remote=ssh(f'cat {ROOT}/_input_blobs/{expected}',stdout=stream,check=True)
                    with blob.open('rb') as stream:
                        if hashlib.file_digest(stream,'sha256').hexdigest()!=expected:
                            raise RuntimeError('remote archived blob mismatch: '+original)
                VERIFIED.add(expected)
            elif expected not in VERIFIED:
                with blob.open('rb') as stream:
                    if hashlib.file_digest(stream,'sha256').hexdigest()!=expected:
                        raise RuntimeError('blob integrity failure: '+expected)
                VERIFIED.add(expected)
            destination=target/source.name
            if not destination.exists():os.link(blob,destination)
    return folder


def run(spec, existing_outer=False):
    plan=ROOT/'plans'/(spec['label']+'.json')
    save(plan,spec)
    if not existing_outer:prepare_l1(spec)
    transfer([plan],spec['label']+'-plan')
    update('running-cohort',active=spec['label'])
    try:
        with (ROOT/(spec['label']+'.driver.log')).open('x') as log:
            process=ssh(f'sudo -n python3 {H}/cohort.py {plan}',
                timeout=None,stdout=log,stderr=subprocess.STDOUT)
        save(ROOT/(spec['label']+'.receipt.json'),dict(returncode=process.returncode,epoch=time.time()))
        folder=retrieve(spec['label'])
        result=read(folder/'status.json')
        if spec.get('ready_only'):return result
        return audit(spec['label'])
    finally:
        if not existing_outer:stop(spec['label'])


def calibration(n, medium, slots):
    path=ROOT/f'calibration-{medium}-n{n}.json'
    if path.exists():return read(path)
    observations=[]
    for repetition in range(1,4):
        spec=dict(label=f'P0-calibrate-{medium}-n{n}-r{repetition}',category='P0',
            strategy='P',medium=medium,n=n,repetition=repetition,seed=20261029,
            dram_gib=8,cxl_gib=64 if medium=='cxl' else 0,reference=None,
            ready_only=True,slots=slots)
        state=run(spec)
        if state['result']!='ready-calibration-completed':
            result=dict(valid=False,reason='ready calibration failed',label=spec['label'])
            save(path,result);return result
        for point in read(ROOT/'cohorts'/spec['label']/'ready-observations.json'):
            samples=point['slots']
            if any(p is None for p in samples):continue
            m=point['l1']['memory']
            # Retain shmem (the VMM RAM mappings) when excluding reclaimable L1
            # filesystem cache. This avoids subtracting Guest RAM as page cache.
            used=8192*(1<<20)-m['MemFree']-max(0,m['Cached']-m['Shmem'])-m['Buffers']-m['SReclaimable']
            users=sum(max(0,p['guest']['memory_current']-p['guest']['memory_stat']['kernel']-
                p['guest']['memory_stat'].get('sock',0)) for p in samples)
            if medium=='cxl':
                # Count fast-node occupied/reserved physical memory, retaining
                # shmem-backed Guest RAM while excluding reclaimable L1 cache.
                fast=point['l1']['nodes']['node0']
                used=8192*(1<<20)-fast['MemFree']-max(0,fast['FilePages']-fast['Shmem'])-fast['SReclaimable']
                users=0
                for sample in samples:
                    fields={}
                    for line in sample['guest']['memory_numa_stat'].splitlines():
                        parts=line.split()
                        fields[parts[0]]=int(dict(v.split('=') for v in parts[1:]).get('N0',0))
                    users+=max(fields.get('anon',0)+fields.get('file',0),
                        sum(fields.get(k,0) for k in ['active_anon','inactive_anon',
                            'active_file','inactive_file','unevictable']))
            observations.append(max(0,used-users))
    if not observations:raise RuntimeError('no fresh ready calibration observations')
    overhead=math.ceil(max(observations)/(16*(1<<20)))*16
    quota=min(6144,math.floor((8192-overhead-512)/n/16)*16)
    result=dict(valid=quota>=16,overhead_mib=overhead,quota_mib=quota,
        observations=len(observations),reason=None if quota>=16 else 'static quota infeasible')
    save(path,result);return result


def main():
    with (ROOT/'campaign.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        save(ROOT/'campaign-process.json',dict(pid=os.getpid(),started_epoch=time.time()))
        matrix=read(ROOT/'matrix.json')
        completed=[];failed=[];pending=[];censored=[]
        try:
            for original in matrix:
                spec=json.loads(json.dumps(original))
                output=ROOT/'cohorts'/spec['label']/'audit.json'
                if output.exists():
                    result=read(output)
                else:
                    if spec['reference']:
                        baseline=read(ROOT/'cohorts'/spec['reference']/'audit.json')
                        if not baseline['valid']:
                            pending.append(dict(label=spec['label'],reason='fresh paired reference failed',reference=spec['reference']))
                            update('reference-gate',active=None,completed=completed,failed=failed,pending=pending)
                            continue
                        fresh={r['slot']:r['e2e_s'] for r in baseline['rows']}
                        for slot in spec['slots']:
                            slot.update(deadline_s=None, deadline_authority='no execution cutoff')
                    if spec['strategy']=='F':
                        cap=calibration(spec['n'],spec['medium'],spec['slots'])
                        if not cap['valid']:
                            pending.append(dict(label=spec['label'],reason=cap['reason']))
                            update('calibration-gate',active=None,pending=pending)
                            continue
                        spec['quota_mib']=cap['quota_mib']
                    result=run(spec)
                target=censored if result.get('classification')=='censored-or-incomplete' else completed if result['valid'] else failed
                target.append(spec['label'])
                update('cohort-audited',active=None,completed=completed,failed=failed,pending=pending,censored=censored)
            update('finished' if not pending else 'finished-with-unresolved-gates',
                active=None,completed=completed,failed=failed,pending=pending,censored=censored)
        except BaseException as error:
            update('needs-recovery',error=repr(error),completed=completed,failed=failed,pending=pending,censored=censored)
            raise


if __name__=='__main__':
    main()
