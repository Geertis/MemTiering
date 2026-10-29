"""Fresh exhaustive census, with explicit provenance for prior faithful normalizations."""
import collections
import hashlib
import json
from pathlib import Path
import shutil
import time

from execution import ROOT,W,read,write,sha

SOURCE=W.parent/'agentcgroup'
OLD=W/'results/all-traces-v55-20261021'
FOLLOW=W/'results/agentcgroup-followup-v63-20261025'

def main():
    if (ROOT/'inventory.json').exists():return
    prior=read(OLD/'inventory-v56.json')['rows']
    overlays={}
    for file in [OLD/'supplement-v59/inventory.json',FOLLOW/'intake.json']:
        for row in read(file)['rows']:overlays[row['name']]=row
    known={r['relative_source']:r for r in prior}
    images={}
    for parent in [OLD,FOLLOW]:
        for p in (parent/'image-preparation').glob('*/oci-inspect.json'):
            info=read(p)
            for tag in info.get('RepoTags',[]):
                if info.get('RepoDigests'):images[tag.removesuffix(':latest')]=info['RepoDigests'][0]
    paths=sorted({p.parent for name in ['tool_calls.json','trace.jsonl'] for p in SOURCE.rglob(name)})
    rows=[];hash_cache={}
    def digest(p):
        p=Path(p)
        if str(p) not in hash_cache:hash_cache[str(p)]=sha(p)
        return hash_cache[str(p)]
    for folder in paths:
        rel=str(folder.relative_to(SOURCE));base=known.get(rel)
        if base is None:
            sid=rel.replace('/','-')[-130:]+'-'+hashlib.sha256(rel.encode()).hexdigest()[:8]
            rows.append(dict(name=sid,relative_source=rel,source=str(folder),eligibility='requires-new-normalization',reasons=['New source directory needs faithful schema/Task reconstruction'],golden=None));continue
        c=dict(overlays.get(base['name'],base));c.update(relative_source=rel,source=str(folder))
        source_checks={}
        for name in ['tool_calls.json','trace.jsonl','results.json','resources.json']:
            p=folder/name
            if p.exists():source_checks[str(p)]=digest(p)
        changed=[p for p,h in base.get('source_hashes',{}).items() if Path(p).exists() and digest(p)!=h]
        if changed:
            c.update(eligibility='requires-new-normalization',reasons=['Recorded source changed; previous IR not silently reused'],changed_sources=changed);rows.append(c);continue
        original_inputs=Path(c['trace']);destination=ROOT/'inputs'/c['name'];destination.mkdir(parents=True,exist_ok=False)
        input_sha={}
        for name in ['tool_calls.json','trace.jsonl']:
            p=original_inputs/name
            if p.exists():
                shutil.copy2(p,destination/name);input_sha[name]=digest(p)
                (destination/name).chmod(0o444)
        write(destination/'provenance.json',dict(source_raw_sha256=source_checks,normalized_source=str(original_inputs),normalized_sha256=input_sha,
            source_hashes_unchanged=True,commands_order_and_gaps_unchanged=True,prior_wrapper_proof=c.get('task_wrapper_proof'),prior_normalization=c.get('normalization')))
        c.update(trace=str(destination),input_hashes=input_sha,source_hashes=source_checks,prior_input_authority=str(original_inputs))
        replayable=base['eligibility']=='ready-for-replay-preflight' or c.get('eligibility')=='ready-for-supplemental-preflight' or any(x in c.get('recovery_actions',[]) for x in ['recovered-complete-Task-chain'])
        c['eligibility']='ready-for-replay-preflight' if replayable else 'not-currently-replayable'
        if replayable:c['reasons']=[]
        c['network_repair']=bool(c.get('network_repair',False))
        c['restore_scratchpad']=bool(c.get('restore_scratchpad',False))
        if c.get('golden') and not Path(c['golden']).exists():
            c.update(retired_golden=c['golden'],golden=None,golden_sha256=None,reviewed_environment=False)
        if not c.get('golden'):c['preparation_image']=c.get('preparation_image') or images.get(c['image']) or c['image']
        rows.append(c)
    counts=dict(collections.Counter(c['eligibility'] for c in rows))
    write(ROOT/'inventory.json',dict(created_epoch=time.time(),source=str(SOURCE),scope='Every current directory with tool_calls.json or trace.jsonl; no success-only selection',rows=rows,counts=counts,total_directories=len(rows)))
    state=dict(stage='inventory-ready',created_epoch=time.time(),coordinator_service='codex-agenttraces-v67-campaign.service',completed=[],active=[],profile_slots=1,
        cases={c['name']:dict(stage='pending-preflight' if c['eligibility']=='ready-for-replay-preflight' else 'not-replayable',reasons=c.get('reasons',[])) for c in rows})
    write(ROOT/'status.json',state)
    print('FRESH CENSUS',len(rows),counts,flush=True)

if __name__=='__main__':main()
