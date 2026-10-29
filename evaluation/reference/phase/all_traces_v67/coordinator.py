"""Resumable full rerun of the optimized system, with a staged concurrency assessment."""
import collections
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from execution import ROOT,H,W,PROFILES,read,write,sha,archive
from eligibility import check
from records import key,catalog,recover_orphans,freeze_plan
from resources import Monitor,await_admission,ResourceDeferred,diagnostics
from images import prepare,retire,release_failed
from waves import wave

A=Path('/r0/home/geerties/HW/report/assets/261026新实现全量复测')
TERMINAL={'matrix-finished','baseline-rejected','compact-baseline-rejected','image-unavailable','sampling-insufficient'}
class Deferred(RuntimeError):pass

class Campaign:
    def __init__(self):
        self.state=read(ROOT/'status.json')
        cases=[dict(c) for c in read(ROOT/'inventory.json')['rows'] if c['eligibility']=='ready-for-replay-preflight']
        first=['all_images_haiku-streamlink__streamlink-3485-','all_images_haiku-sigmavirus24__github3.py-673-']
        refs=[next(c for c in cases if part in c['name']) for part in first]
        cases=refs+sorted([c for c in cases if c not in refs],key=lambda c:(not bool(c.get('golden')),c['name']))
        self.plan=freeze_plan(cases);self.cases=self.plan['cases']
        self.state.update(stage='recovering',coordinator_service='codex-agenttraces-v67-campaign.service',pid=os.getpid(),active=[])
        self.state.pop('reason',None);self.refresh()
        self.checked={}
    def save(self):
        self.state['updated_epoch']=time.time();write(ROOT/'status.json',self.state)
    def refresh(self):
        self.rows,self.selected,self.infra=catalog();self.state['completed']=self.rows
        write(ROOT/'selection.json',{k:r['name'] for k,r in self.selected.items()});self.save()
    def notify(self,stage):
        self.state['stage']=stage;self.save()
    def verify(self):
        for p,want in self.plan['source_sha256'].items():
            path=Path(p);s=path.stat();identity=(s.st_size,s.st_mtime_ns,s.st_ino)
            if self.checked.get(p)!=identity:
                if sha(path)!=want:raise RuntimeError('Frozen input changed: '+p)
                self.checked[p]=identity
    def execute_tasks(self,tasks,kind='formal'):
        root=ROOT/'formal' if kind=='formal' else ROOT/'concurrency/formal'
        await_admission(len(tasks));self.verify()
        for t in tasks:t['timeout_s']=7200 if (t['case'].get('recorded_span_s') or 0)>1200 else 1800
        self.state.update(stage='formal-running' if kind=='formal' else 'concurrency-assessment',
            active=[dict(case=t['case']['name'],label=t['label'],profile=t['profile'],lane=t['lane'],kind=kind) for t in tasks]);self.save()
        tag=hashlib.sha256(json.dumps([(t['case']['name'],t['label']) for t in tasks]).encode()).hexdigest()[:16]
        barrier=(ROOT/'barriers' if kind=='formal' else ROOT/'concurrency/barriers')/tag
        notify=self.notify if kind=='formal' else lambda ignored:self.notify('concurrency-assessment')
        rows,released,error=wave(root,tasks,barrier,notify)
        for t,row in zip(tasks,rows):
            actual=(barrier/f'lane{t["lane"]}.go').exists() or row.get('complete',False)
            if actual:
                row=check(root,t['case'],row,baseline=t.get('baseline'),persist=False)
                compact=t.get('compact_baseline')
                if row.get('complete') and compact is not None and json.dumps(row['signature'],sort_keys=True)!=json.dumps(compact['signature'],sort_keys=True):
                    row['reasons']=sorted(set(row['reasons']+['workload signature differs from same-block compact DRAM']));row['valid']=False
                if row.get('complete') and kind=='formal':row['host_diagnostics']=diagnostics(root,row)
            row.update(repeat=t.get('repeat'),finalized=True,measurement_released=bool(actual),concurrency_size=len(tasks),orchestration_error=error)
            if t.get('baseline'):row['paired_baseline_name']=t['baseline']['name']
            if t.get('compact_baseline'):row['compact_baseline_name']=t['compact_baseline']['name']
            write(root/(row['name']+'.audit.json'),row)
        if kind=='formal':self.refresh()
        # All guests have exported and stopped before compression.
        for row in rows:archive(root/row['name'])
        self.state['active']=[];self.save()
        return [read(root/(r['name']+'.audit.json')) for r in rows]
    def profile(self,cases,repeat,profile):
        result={}
        pending=[]
        for c in cases:
            k=key(c['name'],repeat,profile)
            if k in self.selected:result[c['name']]=self.selected[k]
            else:pending.append(c)
        limit=self.state.get('profile_slots',1)
        if any(c.get('network_repair') or (c.get('recorded_span_s') or 0)>1200 for c in pending):limit=1
        if len({c['lane'] for c in pending})!=len(pending):limit=1
        # Block1 is always serial; only fully checked previous blocks enter pairing.
        if repeat==1:limit=1
        for offset in range(0,len(pending),limit):
            batch=pending[offset:offset+limit]
            for attempt in range(3):
                tasks=[]
                for c in batch:
                    k=key(c['name'],repeat,profile)
                    if k in self.selected:continue
                    if len(self.infra.get(k,[]))>=3:raise Deferred('Three pre-E2E VM aborts: '+k)
                    label=f'r{repeat}-{profile}'
                    parent=ROOT/'formal'/c['name']
                    if list(parent.glob(label+'*')):
                        n=len(self.infra.get(k,[]))+1
                        while list(parent.glob(label+f'-a{n}'+'*')):n+=1
                        label+=f'-a{n}'
                    baseline=self.selected.get(key(c['name'],repeat,'dram')) if profile!='dram' else None
                    compact=self.selected.get(key(c['name'],repeat,'compact-dram')) if profile not in ['dram','compact-dram'] else None
                    tasks.append(dict(case=c,profile=profile,label=label,repeat=repeat,lane=c['lane'],baseline=baseline,compact_baseline=compact,baseline_name=baseline and baseline['name']))
                if not tasks:break
                try:self.execute_tasks(tasks)
                except ResourceDeferred as exc:raise Deferred(str(exc))
                if all(key(c['name'],repeat,profile) in self.selected for c in batch):break
                self.notify('retry-backoff');time.sleep(30 if attempt==0 else 60)
            for c in batch:
                k=key(c['name'],repeat,profile)
                if k not in self.selected:raise Deferred('Pre-E2E budget exhausted: '+k)
                result[c['name']]=self.selected[k]
        return result
    def assess_concurrency(self,refs):
        path=ROOT/'concurrency/status.json'
        if path.exists() and read(path).get('stage')=='finished':
            self.state['profile_slots']=read(path)['common_slots'];self.save();return
        state=dict(stage='running',completed=[],decisions={});write(path,state)
        for profile in PROFILES:
            serial=[self.selected.get(key(c['name'],1,profile)) for c in refs]
            if not all(r and r['valid'] for r in serial):
                state['decisions'][profile]=dict(slots=1,reason='Formal reference failed correctness');write(path,state);continue
            tasks=[dict(case=c,lane=c['lane'],profile=profile,label='parallel-'+profile,repeat=1,baseline=self.selected[key(c['name'],1,'dram')]) for c in refs]
            existing=[ROOT/'concurrency/formal'/(c['name']+'/parallel-'+profile+'.audit.json') for c in refs]
            if all(p.exists() for p in existing):parallel=[read(p) for p in existing]
            else:
                try:parallel=self.execute_tasks(tasks,kind='pilot')
                except ResourceDeferred as exc:
                    state['decisions'][profile]=dict(slots=1,reason=str(exc));write(path,state);continue
            comparisons=[]
            for b,c in zip(serial,parallel):
                good=b['valid'] and c['valid'];changes={}
                if good:
                    changes=dict(service_pct=100*(c['service_s']/b['service_s']-1),service_s=c['service_s']-b['service_s'],
                        e2e_pct=100*(c['e2e_s']/b['e2e_s']-1),mean_pct=100*(c['ddr_mean_mib']/b['ddr_mean_mib']-1))
                    good=json.dumps(c['signature'],sort_keys=True)==json.dumps(b['signature'],sort_keys=True) and not(changes['service_pct']>5 and changes['service_s']>.1) and changes['e2e_pct']<=1 and abs(changes['mean_pct'])<=5
                comparisons.append(dict(case=b['trace'],passed=good,changes=changes,reasons=c['reasons']))
            state['completed'].extend(parallel);state['decisions'][profile]=dict(slots=2 if all(c['passed'] for c in comparisons) else 1,comparisons=comparisons);write(path,state)
        state.update(stage='finished',common_slots=min(d['slots'] for d in state['decisions'].values()),finished_epoch=time.time());write(path,state)
        self.state['profile_slots']=state['common_slots'];self.save()
    def block(self,group,repeat):
        alive=[c for c in group if self.state['cases'][c['name']]['stage'] not in TERMINAL]
        if not alive:return
        try:
            for profile in ['dram','compact-dram']:
                rows=self.profile(alive,repeat,profile)
                for c in list(alive):
                    if not rows[c['name']]['valid']:
                        self.state['cases'][c['name']].update(stage='baseline-rejected' if profile=='dram' else 'compact-baseline-rejected',reasons=rows[c['name']]['reasons']);alive.remove(c)
                    elif rows[c['name']].get('e2e_s',0)<.25:
                        self.state['cases'][c['name']].update(stage='sampling-insufficient',reasons=['Fresh E2E shorter than250ms physical sampling period']);alive.remove(c)
                self.save()
                if not alive:return
            arms=['stock','cxl-layout','ssd','cxl'];shift=(repeat-1)%4
            for profile in arms[shift:]+arms[:shift]:self.profile(alive,repeat,profile)
            for c in alive:self.state['cases'][c['name']].update(stage='matrix-finished' if repeat==3 else 'running',completed_repeats=repeat)
            self.save()
        except Deferred as exc:
            for c in alive:self.state['cases'][c['name']].update(stage='deferred-infrastructure',defer_reason=str(exc))
            self.state['active']=[];self.save()
    def case_plan(self,c,lane):
        c=dict(c);c['lane']=lane;p=ROOT/'case-plans'/(c['name']+'.json')
        if p.exists():
            old=read(p)
            if old['trace']!=c['trace']:raise RuntimeError('Case input authority changed')
            return old
        if c.get('golden'):
            if sha(Path(c['golden']))!=c['golden_sha256']:raise RuntimeError('Reviewed golden changed: '+c['name'])
        write(p,c);return c
    def retire_complete(self):
        for p in (ROOT/'image-preparation').glob('*/prepared.json'):
            if (p.parent/'retired.json').exists():continue
            record=read(p);members=[c for c in self.cases if not c.get('golden') and c.get('preparation_image')==record['image']]
            if members and all(self.state['cases'][c['name']]['stage'] in TERMINAL for c in members):
                retire(record);write(p.parent/'retired.json',dict(epoch=time.time(),reason='All consumers terminal; no VM active'))
    def run(self):
        refs=[self.case_plan(c,i) for i,c in enumerate(self.cases[:2],1)]
        self.block(refs,1)
        self.assess_concurrency(refs)
        for repeat in [2,3]:self.block(refs,repeat)
        groups=collections.defaultdict(list)
        for c in self.cases[2:]:groups[c.get('golden') or c['preparation_image']].append(c)
        order=list(groups)
        for sweep in range(2):
            for start in range(0,len(order),3):
                ready=[]
                for image in order[start:start+3]:
                    pending=[dict(c) for c in groups[image] if self.state['cases'][c['name']]['stage'] not in TERMINAL]
                    if not pending:continue
                    self.state.update(stage='image-preparation',active=[dict(image=image)]);self.save()
                    if pending[0].get('golden'):ready.extend(pending);continue
                    meta=None;errors=[]
                    for attempt in range(3):
                        try:meta=prepare(image);break
                        except Exception as exc:
                            errors.append(dict(epoch=time.time(),attempt=attempt+1,error=repr(exc)))
                            write(ROOT/'image-retries'/hashlib.sha256(image.encode()).hexdigest()[:16]/f'sweep{sweep}.json',errors)
                            if attempt<2:time.sleep(30*(attempt+1))
                    if meta is None:
                        for c in pending:self.state['cases'][c['name']].update(stage='image-unavailable',errors=errors)
                        release_failed(image);self.save();continue
                    for c in pending:
                        c.update(golden=meta['golden'],golden_sha256=meta['golden_sha256'],oci_id=meta['oci_id'],repo_digests=meta['repo_digests']);ready.append(c)
                cases=[self.case_plan(c,i%2+1) for i,c in enumerate(ready)]
                for repeat in range(1,4):
                    local=[c for c in cases if not c.get('network_repair') and (c.get('recorded_span_s') or 0)<=1200]
                    solo=[c for c in cases if c not in local]
                    chunks=[local[i:i+2] for i in range(0,len(local),2)]+[[c] for c in solo]
                    for chunk in chunks:self.block(chunk,repeat)
                self.state.update(stage='image-retirement',active=[]);self.save();self.retire_complete()
            if not any(c['stage']=='deferred-infrastructure' for c in self.state['cases'].values()):break
            if sweep==0:self.notify('deferred-cooldown');time.sleep(300)
        self.refresh();self.state.update(stage='independent-audit',active=[],formal_finished_epoch=time.time());self.save()

def start_docker():
    config=ROOT/'docker-isolated.json'
    if not config.exists():write(config,dict(hosts=['unix:///run/codex-agenttraces-v67.sock'],**{'data-root':str(ROOT/'image-cache/docker'),'exec-root':'/run/codex-agenttraces-v67','pidfile':'/run/codex-agenttraces-v67.pid','bridge':'none','iptables':False,'ip6tables':False,'ip-forward':False,'ip-masq':False,'max-concurrent-downloads':2,'storage-driver':'overlay2','features':{'containerd-snapshotter':False},'containerd-namespace':'codex-agenttraces-v67','containerd-plugins-namespace':'codex-agenttraces-v67-plugins'}))
    if subprocess.run(['systemctl','is-active','--quiet','codex-agenttraces-v67-docker.service']).returncode:
        subprocess.run(['sudo','-n','systemd-run','--unit=codex-agenttraces-v67-docker','--property=Restart=on-failure','--property=RestartSec=10','--property=CPUAffinity=24-31','--property=StandardOutput=append:'+str(ROOT/'docker.log'),'--property=StandardError=inherit','/usr/bin/dockerd','--config-file',str(config)],check=True)
    deadline=time.monotonic()+120
    from images import verify_scope
    while True:
        try:verify_scope();return
        except Exception:
            if time.monotonic()>deadline:raise
            time.sleep(2)

def main():
    while True:
        p=ROOT/'geometry-validation.json'
        if p.exists():
            stage=read(p)
            if stage['stage']=='failed':raise RuntimeError('Compact-CXL geometry gate failed: '+stage['reason'])
            if stage['stage']=='finished':break
        time.sleep(2)
    with (ROOT/'coordinator.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);recover_orphans()
        campaign=Campaign();monitor=Monitor();monitor.start()
        try:
            try:start_docker();campaign.run()
            finally:monitor.close()
            subprocess.run(['/usr/bin/python3',str(H/'finalize.py')],check=True)
        except BaseException as exc:
            campaign.state.update(stage='needs-recovery',reason=repr(exc),active=[]);campaign.save();raise
if __name__=='__main__':main()
