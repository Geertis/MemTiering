"""Checkpoint and independent selection of first measured outcomes."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time
from execution import ROOT,H,W,read,write,audit,restore_for_audit
from eligibility import check
from isolation import check as isolation_check

def key(sid,repeat,profile):return f'{sid}|{repeat}|{profile}'
def catalog():
    rows=[];selected={};infra={}
    for p in (ROOT/'formal').glob('*/*.audit.json'):
        row=dict(read(p));row.setdefault('repeat',int(re.match(r'^r(\d+)-',row['label']).group(1)))
        meta=read(ROOT/'formal'/(row['name']+'.orchestration.json'))
        released=(Path(meta['barrier'])/f'lane{row["lane"]}.go').exists() or bool(row.get('complete'))
        row.update(attempt_kind='measured' if released else 'pre-e2e-infrastructure',started_epoch=meta['started_epoch'])
        k=key(row['trace'],row['repeat'],row['profile'])
        if released:
            if k in selected:raise RuntimeError('Two measured attempts for '+k)
            selected[k]=row
        else:infra.setdefault(k,[]).append(row['name'])
        rows.append(row)
    rows.sort(key=lambda r:(r['started_epoch'],r['name']))
    return rows,selected,infra

def review(case,job,baseline=None):
    row=audit(ROOT/'formal',case,job)
    errors,iso=isolation_check(ROOT/'formal'/job['name'],job['lane'])
    row['host_isolation']=iso;row['reasons']=sorted(set(row['reasons']+errors));row['valid']=not row['reasons']
    return check(ROOT/'formal',case,row,baseline=baseline,persist=False)

def recover_orphans():
    for p in (ROOT/'formal').glob('*/*.orchestration.json'):
        meta=read(p);name=meta['name'];out=ROOT/'formal'/name;saved=ROOT/'formal'/(name+'.audit.json')
        if saved.exists() and read(saved).get('finalized'):continue
        if meta.get('cancelled_before_launch'):continue
        barrier=Path(meta['barrier']);(barrier/'abort').touch();(barrier/'export.go').touch()
        iso_path=ROOT/'formal'/(name+'.isolation.json')
        if not iso_path.exists():
            meta['cancelled_before_launch']=True;write(p,meta);continue
        iso=read(iso_path)
        if not iso['unit'].startswith('acg67-'):raise RuntimeError('Unexpected resource domain')
        subprocess.run(['sudo','-n','systemctl','stop',iso['unit']],check=False,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        receipt=ROOT/'formal'/(name+'.receipt.json')
        if not receipt.exists():write(receipt,dict(exit_code=137 if (barrier/f'lane{iso["lane"]}.go').exists() else 98,started_epoch=meta['started_epoch'],finished_epoch=time.time(),reason='Interrupted coordinator; own unit stopped'))
        case=read(ROOT/'case-plans'/(name.split('/')[0]+'.json'));job=read(ROOT/'formal'/(name+'.job.json'))
        baseline=read(ROOT/'formal'/(meta['baseline']+'.audit.json')) if meta.get('baseline') else None
        with restore_for_audit(out):row=review(case,job,baseline)
        row.update(repeat=meta['repeat'],finalized=True,recovered_after_coordinator_exit=True)
        write(saved,row)

def freeze_plan(cases):
    p=ROOT/'campaign-plan.json'
    if p.exists():return read(p)
    files=[f for f in H.iterdir() if f.suffix in ['.py','.sh']]
    for prefix in ['vmm-v65-ssd-scope','vmm-v66-ssd-compact','vmm-v67-compact-cxl']:
        for suffix in ['', '-readguard']:
            files.extend(f for f in (W/'phase/artifacts'/(prefix+suffix)).rglob('*') if f.is_file() and '__pycache__' not in f.parts)
    output={};cache={}
    for f in files:
        stat=f.stat();identity=(stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns)
        if identity not in cache:
            h=hashlib.sha256()
            with f.open('rb') as stream:
                for block in iter(lambda:stream.read(8<<20),b''):h.update(block)
            cache[identity]=h.hexdigest()
        output[str(f)]=cache[identity]
    plan=dict(created_epoch=time.time(),cases=cases,source_sha256=output,profiles=['dram','compact-dram','stock','cxl-layout','ssd','cxl'],repeats=3,max_formal_vms=len(cases)*18,
        quota=dict(active_mib=6144,wait_mib=16),scope='All 228 source directories; 186 replayable enter new baseline preflight, all others retain reasons',
        geometry='dram/stock raw8192+8192 and balloon2048+2048; all other profiles raw6144+6144, balloon0, unused generic mem_profiling=never; all nominal6144+6144',
        concurrency='First two fixed representative cases block1 are formal serial references. Before block2 run 12 separate engineering VMs, two per profile, to assess two-way execution. Common two-lane envelope only if every profile and both traces pass; otherwise serial. All arms within each formal block share the envelope. Network-restored/long traces serial.',
        concurrency_thresholds=dict(service_increase_pct=5,service_absolute_tolerance_s=.1,e2e_increase_pct=1,absolute_mean_dram_change_pct=5,
            rule='Service rejects only when increase exceeds both 5% and100ms, to avoid percent noise on short services. No resource-domain violations. Global PSI is diagnostic after quiet start.'),
        correctness='First measured outcome is final regardless of performance; fresh DRAM and compact-DRAM must pass before candidates. Candidate signature must equal both same-block baselines; baseline fingerprints stable across3 repeats for strict summary.',
        retries='At most3 pre-E2E VM attempts per logical slot; bounded180s idle resource admission and180s quiet gate. Defer one case on exhaustion. Second deferred sweep; no indefinite resource-wait loop.',
        image_policy='At most3 image groups per window; no preparation/compression/retirement while measured VMs run. All OCI digests and regenerated golden authorities recorded. No mutation of older results.',
        deadlines='Uniform1800s external harness bound,7200s for source span>1200s. Not a controller workload-duration estimate.',
        latency_limits_offline=[5,10,15,20],initial_reference_case_ids=[c['name'] for c in cases[:2]])
    write(p,plan);return plan
