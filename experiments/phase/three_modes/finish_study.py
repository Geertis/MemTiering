#!/usr/bin/env python3
"""Verify completed cohorts and record hashes for the generated research report."""
import argparse,hashlib,json,os,subprocess,time
from pathlib import Path
W=Path(__file__).resolve().parents[2];R=W/'results/three-modes-20261010'
p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args()
def sha(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for b in iter(lambda:f.read(8<<20),b''):h.update(b)
 return h.hexdigest()
cohorts={};artifacts={};plans={}
for name,expected in [('validated-pilot-v3',12),('confirmation-v13',75),('pressure-v14',28),('reporting5-v15',18),('signals-v16',12)]:
 d=json.loads((R/name/'analysis.json').read_text());plan=json.loads((R/name/'plan.json').read_text())
 assert d['planned']==d['finished']==d['basic_valid']==expected,name
 assert all(s['valid'] for s in d['summaries']),name+' work equivalence'
 assert all(t['guest_ram_ddr_peak_mib']==0 for t in d['trials'] if t['strategy']=='cxl'),name+' CXL physical endpoint'
 cohorts[name]=dict(independent_vms=expected,complete=True,valid=True,
  cxl_guest_ram_ddr_peak_mib=0 if any(t['strategy']=='cxl' for t in d['trials']) else None,
  analysis_sha256=sha(R/name/'analysis.json'))
 plans[name]=sha(R/name/'plan.json')
 artifact=Path(plan['artifact'])
 for n,expected_hash in plan['artifact_files'].items():
  assert sha(artifact/n)==expected_hash,'artifact drift '+str(artifact/n)
 artifacts[str(artifact)]=plan['artifact_files']
root=W/'validated-root-disks/root-demeter.qcow2'
expected_root=json.loads((R/'host-environment.json').read_text())['root-image-sha256'].split()[0]
assert sha(root)==expected_root,'root image drift'
assert os.uname().release=='6.10.0-demeterhost-loadfb-20261005'
for cohort in ['confirmation-v13','pressure-v14','reporting5-v15','signals-v16']:
 subprocess.run(['python3',str(Path(__file__).with_name('phase_signal.py')),'--root',str(R/cohort)],check=True,cwd=W)
subprocess.run(['python3',str(Path(__file__).with_name('build_report.py')),'--output',str(a.output)],check=True,cwd=W)
files={str(p):sha(p) for p in Path(__file__).parent.glob('*.py')}
record=dict(epoch=time.time(),host_kernel=os.uname().release,valid_independent_vms=sum(x['independent_vms'] for x in cohorts.values()),
 cohorts=cohorts,plans_sha256=plans,artifacts=artifacts,analysis_scripts_sha256=files,
 root_image_sha256=expected_root,report=str(a.output.resolve()),report_sha256=sha(a.output),
 scope='Separate cohorts; whole VMM RSS, not all Host costs; no shared-VM budget or density claim')
(R/'final-audit.json').write_text(json.dumps(record,indent=2)+'\n')
print('FINAL_AUDIT',record['valid_independent_vms'],'valid independent VMs')
