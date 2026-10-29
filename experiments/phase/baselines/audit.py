#!/usr/bin/env python3
"""Post-run hashes and observed failures; no extra benchmark execution."""
import hashlib
import json
import subprocess
import time
from pathlib import Path

W=Path(__file__).resolve().parents[2];R=W/'results/baselines-20261009';H=Path(__file__).resolve().parent
def sha(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as f:
        while b:=f.read(8<<20):digest.update(b)
    return digest.hexdigest()
initial=json.loads((R/'initial-image-audit.json').read_text());images=[]
for item in initial['files']:
    actual=sha(item['path']);images.append({**item,'final_sha256':actual,'unchanged':actual==item['sha256']})
sources=[];archive=R/'final-source-inputs';archive.mkdir(exist_ok=True)
import shutil
for path in sorted(H.iterdir()):
    if path.is_file():
        shutil.copy2(path,archive/path.name);sources.append({'path':str(path),'sha256':sha(path)})
protocols=[]
for name in ['screen','realtime','mixed','whole-ram-v3']:
    plan=json.loads((R/name/'plan.json').read_text())
    protocols.append({'cohort':name,'source_match':all(sha(k)==v for k,v in plan['files'].items()),
        'artifact_match':all(sha(Path(plan.get('artifact',str(W/'phase/artifacts/baseline-v8-common')))/k)==v
            for k,v in plan['artifact_files'].items())})
started=min(json.loads(p.read_text())['started_epoch'] for p in (R/'screen').glob('*.receipt.json'))
kernel=subprocess.run(['sudo','-n','journalctl','-k','--since','@'+str(int(started)),'--no-pager'],capture_output=True,text=True)
(R/'host-kernel.log').write_text(kernel.stdout)
oom_lines=[line for line in kernel.stdout.splitlines() if any(x in line.lower() for x in ['out of memory','oom-kill','killed process'])]
ps=subprocess.check_output(['ps','-eo','pid,args'],text=True)
own_vms=[line for line in ps.splitlines() if '/cloud-hypervisor ' in line and '/HW/Demeter/experiments/agentcgroup-demeter/.scratch-' in line]
checks={'epoch':time.time(),'images':images,'protocols':protocols,'sources':sources,
        'host_oom_lines':oom_lines,'journal_exit_code':kernel.returncode,'remaining_experiment_vms':own_vms}
checks['pass']=all(x['unchanged'] for x in images) and all(x['source_match'] and x['artifact_match'] for x in protocols) and not oom_lines and not own_vms and kernel.returncode==0
(R/'final-audit.json').write_text(json.dumps(checks,indent=2)+'\n')
print(json.dumps({'audit_pass':checks['pass'],'images':len(images),'remaining_vms':len(own_vms),'host_oom':len(oom_lines)}))
if not checks['pass']:raise SystemExit(1)
