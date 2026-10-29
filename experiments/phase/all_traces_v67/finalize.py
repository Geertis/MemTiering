"""Independently rebuild all attempts, then summarize total and same-layout gains."""
import csv
import hashlib
import json
import os
from pathlib import Path
import statistics as st
import subprocess
import time
from execution import ROOT,H,read,write,sha,restore_for_audit
from records import catalog,key,review

A=Path('/r0/home/geerties/HW/report/assets/261026新实现全量复测')
REPORT=A.parents[1]/'261026新实现AgentCgroup全量双介质复测.md'

def main():
    rows,selected,infra=catalog();state=read(ROOT/'status.json');cases={p.stem:read(p) for p in (ROOT/'case-plans').glob('*.json')}
    failures=[];checked=[]
    for saved in rows:
        case=cases[saved['trace']];job=read(ROOT/'formal'/(saved['name']+'.job.json'))
        baseline=selected.get(key(saved['trace'],saved['repeat'],'dram')) if job['profile']!='dram' else None
        try:
            with restore_for_audit(ROOT/'formal'/saved['name']):
                rebuilt=review(case,job,baseline)
                compact=selected.get(key(saved['trace'],saved['repeat'],'compact-dram'))
                if rebuilt.get('complete') and job['profile'] not in ['dram','compact-dram'] and compact:
                    if json.dumps(rebuilt['signature'],sort_keys=True)!=json.dumps(compact['signature'],sort_keys=True):
                        rebuilt['reasons']=sorted(set(rebuilt['reasons']+['workload signature differs from same-block compact DRAM']));rebuilt['valid']=False
            keys=['complete','valid','reasons','signature','e2e_s','ddr_mean_mib','ddr_peak_mib','ddr_integral_gib_s','comparison_scope','actual_raw_zone_bytes']
            drift=[k for k in keys if json.dumps(saved.get(k),sort_keys=True)!=json.dumps(rebuilt.get(k),sort_keys=True)]
            if drift:failures.append(dict(name=saved['name'],drift=drift))
            checked.append(dict(name=saved['name'],matched=not drift,valid=rebuilt['valid']))
        except Exception as exc:failures.append(dict(name=saved['name'],error=repr(exc)))
    for p,want in read(ROOT/'campaign-plan.json')['source_sha256'].items():
        if sha(Path(p))!=want:failures.append(dict(path=p,error='frozen source changed'))
    import gzip
    archives=0
    for journal in (ROOT/'formal').rglob('lossless-archives.jsonl'):
        for line in journal.read_text().splitlines():
            item=json.loads(line);p=Path(item['compressed']);h=hashlib.sha256()
            if sha(p)!=item['compressed_sha256']:failures.append(dict(path=str(p),error='compressed hash mismatch'))
            with gzip.open(p,'rb') as f:
                for block in iter(lambda:f.read(1<<20),b''):h.update(block)
            if h.hexdigest()!=item['sha256']:failures.append(dict(path=str(p),error='uncompressed hash mismatch'))
            archives+=1
    write(A/'independent-audit.json',dict(epoch=time.time(),attempts=len(checked),archives=archives,failures=failures,rows=checked))
    if failures:raise RuntimeError('Independent audit failed: '+str(failures[:3]))
    groups=[];pairs=[]
    for sid,case in cases.items():
        for media in ['ssd','cxl']:
            blocks=[];issues=[]
            for repeat in range(1,4):
                b=selected.get(key(sid,repeat,'dram'));n=selected.get(key(sid,repeat,'compact-dram'));c=selected.get(key(sid,repeat,media));layout=selected.get(key(sid,repeat,'cxl-layout'))
                stock=selected.get(key(sid,repeat,'stock'))
                required=[b,n,c]+([layout] if media=='cxl' else [])
                if not all(r and r['valid'] for r in required):
                    issues.append(dict(repeat=repeat,reason='missing/invalid control or candidate'));continue
                same=all(json.dumps(r['signature'],sort_keys=True)==json.dumps(b['signature'],sort_keys=True) for r in required)
                context=all(r.get('concurrency_size')==b.get('concurrency_size') for r in required)
                resolution=all(r['e2e_s']>=.25 for r in required)
                if not(same and context and resolution):
                    issues.append(dict(repeat=repeat,signature_match=same,concurrency_match=context,resolution_sufficient=resolution));continue
                reference=layout if media=='cxl' else n
                p=dict(case=sid,media=media,repeat=repeat,comparison_scope=b['comparison_scope'],baseline=b['name'],candidate=c['name'],compact_baseline=n['name'],same_layout_baseline=reference['name'],
                    e2e_growth_pct=100*(c['e2e_s']/b['e2e_s']-1),same_layout_e2e_growth_pct=100*(c['e2e_s']/reference['e2e_s']-1),
                    service_growth_pct=100*(c['service_s']/b['service_s']-1),service_growth_ms=1000*(c['service_s']-b['service_s']),
                    geometry_static_saved_pct=100*(1-n['ddr_mean_mib']/b['ddr_mean_mib']),
                    phase_increment_pp=100*(reference['ddr_mean_mib']-c['ddr_mean_mib'])/b['ddr_mean_mib'],
                    same_layout_mean_saved_pct=100*(1-c['ddr_mean_mib']/reference['ddr_mean_mib']),
                    baseline_e2e_s=b['e2e_s'],candidate_e2e_s=c['e2e_s'],baseline_mean_mib=b['ddr_mean_mib'],candidate_mean_mib=c['ddr_mean_mib'],baseline_signature=b['signature'])
                for metric,field in [('mean','ddr_mean_mib'),('peak','ddr_peak_mib'),('integral','ddr_integral_gib_s')]:p[metric+'_saved_pct']=100*(1-c[field]/b[field])
                if stock and stock['valid']:
                    p['old_ssd_mean_saved_pct']=100*(1-stock['ddr_mean_mib']/b['ddr_mean_mib'])
                    p['improvement_over_old_ssd_pp']=100*(stock['ddr_mean_mib']-c['ddr_mean_mib'])/b['ddr_mean_mib'] if media=='ssd' else None
                if media=='cxl':
                    p['cxl_topology_increment_pp']=100*(n['ddr_mean_mib']-layout['ddr_mean_mib'])/b['ddr_mean_mib']
                blocks.append(p);pairs.append(p)
            stable=len({json.dumps(p['baseline_signature'],sort_keys=True) for p in blocks})==1
            g=dict(case=sid,source=case['relative_source'],media=media,valid_pairs=len(blocks),strict=len(blocks)==3 and stable,issues=issues)
            if g['strict']:
                g['comparison_scope']=blocks[0]['comparison_scope']
                fields=['mean_saved_pct','peak_saved_pct','integral_saved_pct','e2e_growth_pct','same_layout_e2e_growth_pct','service_growth_pct','service_growth_ms','geometry_static_saved_pct','phase_increment_pp','same_layout_mean_saved_pct','improvement_over_old_ssd_pp']
                for field in fields:
                    values=[p[field] for p in blocks if p.get(field) is not None]
                    if values:g[field]=st.median(values)
                g['e2e_worst_pct']=max(p['e2e_growth_pct'] for p in blocks)
                g['mean_saved_min_pct']=min(p['mean_saved_pct'] for p in blocks)
                g['confirmed_limits']=[lim for lim in [5,10,15,20] if g['e2e_worst_pct']<=lim and g['mean_saved_min_pct']>0]
            groups.append(g)
    for p in pairs:p.pop('baseline_signature',None)
    write(A/'results.json',dict(epoch=time.time(),groups=groups,pairs=pairs,all_attempts=rows,infrastructure_aborts=infra,coverage=state['cases']))
    fields=['case','source','media','comparison_scope','strict','valid_pairs','mean_saved_pct','peak_saved_pct','integral_saved_pct','e2e_growth_pct','e2e_worst_pct','same_layout_mean_saved_pct','phase_increment_pp','geometry_static_saved_pct','improvement_over_old_ssd_pp','confirmed_limits']
    with (A/'groups.csv').open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');writer.writeheader();writer.writerows(groups)
    strict=[g for g in groups if g['strict']]
    deferred=[sid for sid,c in state['cases'].items() if c['stage']=='deferred-infrastructure']
    state.update(stage='finished-with-deferrals' if deferred else 'finished',active=[],finished_epoch=time.time(),strict_media_groups=len(strict),strict_traces=len({g['case'] for g in strict}),deferred_cases=deferred)
    write(ROOT/'status.json',state)
    marker='\n## 最终独立审计结果\n';base=REPORT.read_text().split(marker,1)[0]
    lines=[marker,f'归档 {len(rows)} 次正式尝试；独立重建 {len(checked)} 次，无损归档 {archives} 项，无重建差异。严格三重复可比 {state["strict_traces"]} 条／{len(strict)} 个介质组。延期 {len(deferred)} 条，失败与负收益均保留。','',
        '|轨迹|介质／环境口径|均值节省|峰值节省|积分节省|E2E中位／最差|同布局阶段节省|','|---|---|---:|---:|---:|---:|---:|']
    for g in strict:lines.append('|'+g['source']+'|'+g['media']+'/'+g['comparison_scope']+'|'+'|'.join(f'{g[k]:+.3f}%' for k in ['mean_saved_pct','peak_saved_pct','integral_saved_pct'])+'|'+f'{g["e2e_growth_pct"]:+.3f}%/{g["e2e_worst_pct"]:+.3f}%|{g["same_layout_mean_saved_pct"]:+.3f}%|')
    REPORT.write_text(base+'\n'.join(lines)+'\n')
    subprocess.run(['sudo','-n','systemctl','stop','codex-agenttraces-v67-docker.service'],check=True)
    subprocess.run(['/usr/bin/python3',str(A/'progress.py')],check=True)
    manifest=[];cache={}
    for parent in [ROOT,H,A]:
        for directory,dirs,names in os.walk(parent):
            folder=Path(directory);dirs[:]=[d for d in dirs if d!='__pycache__' and folder/d not in [ROOT/'image-cache',ROOT/'golden-cache']]
            for name in names:
                if name in ['campaign.log','progress-history.jsonl','final-manifest.json'] or name.endswith('.tmp'):continue
                p=folder/name;s=p.stat();identity=(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns)
                if identity not in cache:
                    try:cache[identity]=sha(p)
                    except PermissionError:cache[identity]=subprocess.run(['sudo','-n','sha256sum','--',str(p)],check=True,capture_output=True,text=True).stdout.split()[0]
                manifest.append(dict(path=str(p),bytes=s.st_size,sha256=cache[identity]))
    write(A/'final-manifest.json',dict(epoch=time.time(),files=manifest,excluded='Disposable image/golden caches, bytecode, live coordinator stdout and progress history'))
    print('NEW IMPLEMENTATION FULL RERUN FINISHED',len(strict),'strict media groups',flush=True)
if __name__=='__main__':main()
