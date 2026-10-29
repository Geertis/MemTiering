#!/usr/bin/env python3
"""Small progress receipt; never infer experiment success from a live process."""
import json
import time
from pathlib import Path
W=Path(__file__).resolve().parents[2];R=W/'results/baselines-20261009'
cohorts=[]
for name,logname in [('screen','all'),('realtime','all'),('mixed','mixed'),('whole-ram-v3','whole')]:
    path=R/name/'plan.json'
    if not path.exists():continue
    plan=json.loads(path.read_text());receipts=list((R/name).glob('*.receipt.json'))
    values=[json.loads(p.read_text()) for p in receipts]
    logfile=Path('/tmp')/f'demeter-baseline-{logname}.log'
    loglines=logfile.read_text().splitlines() if logfile.exists() and logfile.stat().st_size else []
    last=loglines[-1] if loglines else ''
    elapsed=None;observer_age=None
    if len(receipts)<len(plan['jobs']) and last.startswith(('START ','START_PAIR ')):
        active=R/name/last.split()[-1]
        manifests=list(active.glob('manifest.json'))+list(active.glob('lane*/manifest.json'))
        streams=list(active.glob('host-lifecycle.jsonl'))+list(active.glob('lane*/host-lifecycle.jsonl'))
        if manifests:elapsed=round(time.time()-min(p.stat().st_mtime for p in manifests),1)
        if streams:observer_age=round(time.time()-max(p.stat().st_mtime for p in streams),1)
    cohorts.append({'cohort':name,'planned':len(plan['jobs']),'finished':len(receipts),
        'failed':sum(v['exit_code']!=0 for v in values),'latest':last,
        'active_setup_and_replay_elapsed_s':elapsed,'observer_age_s':observer_age,
        'pipeline_exception':any('Traceback (most recent call last)' in line for line in loglines[-20:])})
print(json.dumps({'epoch':time.time(),'cohorts':cohorts,
    'all_finished':(R/'all-finished').exists()},ensure_ascii=False))
