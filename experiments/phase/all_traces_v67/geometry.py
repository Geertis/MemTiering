"""Short compact-CXL functionality gate before broad rollout; excluded from formal n=3."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time
from execution import ROOT,H,read,write,archive
from eligibility import check
from waves import wave

A=Path('/r0/home/geerties/HW/report/assets/261026新实现全量复测')
def main():
    while not (A/'old-cohort-parked.json').exists():time.sleep(2)
    # Stop only the superseded cohort's private image daemon.
    if subprocess.run(['systemctl','is-active','--quiet','codex-agenttraces-v63-docker-20261025.service']).returncode==0:
        subprocess.run(['sudo','-n','systemctl','stop','codex-agenttraces-v63-docker-20261025.service'],check=True)
    parser=argparse.ArgumentParser();parser.add_argument('--retry',default=None);args=parser.parse_args()
    root=ROOT/(args.retry or 'geometry');root.mkdir(exist_ok=False)
    case=next(dict(c) for c in read(ROOT/'inventory.json')['rows'] if 'all_images_haiku-streamlink__streamlink-3485-' in c['name'])
    assert Path(case['golden']).exists()
    write(root/'plan.json',dict(case=case,profiles=['compact-dram','cxl-layout','cxl'],source_sha256={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in H.iterdir() if p.suffix in ['.py','.sh']},purpose='Validate compact geometry, static CXL and phase CXL; not formal performance data.'))
    state=dict(stage='running',completed=[]);write(root/'status.json',state)
    global_state=read(ROOT/'status.json')
    baseline=None
    profiles=['compact-dram','cxl-layout','cxl']
    if args.retry:
        previous=read(ROOT/'geometry/status.json');baseline=previous['completed'][0];state['completed']=previous['completed'][:2];profiles=['cxl']
    write(ROOT/'geometry-validation.json',dict(stage='running',authority=str(root)))
    try:
        for profile in profiles:
            global_state.update(stage='geometry-check',active=[dict(case=case['name'],profile=profile,lane=1)]);write(ROOT/'status.json',global_state)
            task=dict(case=case,label='geometry-'+profile,lane=1,profile=profile,timeout_s=1800)
            rows,released,error=wave(root/'formal',[task],root/'barriers'/profile)
            row=check(root/'formal',case,rows[0],baseline=baseline)
            if profile=='compact-dram':baseline=row
            state['completed'].append(row);write(root/'status.json',state)
            if not released or not row['valid']:raise RuntimeError('Geometry gate: '+str(error)+' '+str(row['reasons']))
            archive(root/'formal'/row['name'])
        state.update(stage='finished',finished_epoch=time.time());write(root/'status.json',state)
        write(ROOT/'geometry-validation.json',dict(stage='finished',authority=str(root),completed=state['completed']))
        global_state.update(stage='geometry-passed',active=[]);write(ROOT/'status.json',global_state)
    except Exception as exc:
        state.update(stage='failed',reason=repr(exc));write(root/'status.json',state)
        write(ROOT/'geometry-validation.json',dict(stage='failed',authority=str(root),reason=repr(exc)))
        global_state.update(stage='needs-recovery',reason=repr(exc),active=[]);write(ROOT/'status.json',global_state);raise

if __name__=='__main__':main()
