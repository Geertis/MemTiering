#!/usr/bin/env python3
"""Describe observed per-call initial placement decisions; no response-time oracle."""
import argparse,json
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);a=p.parse_args();R=a.root.resolve()
d=json.loads((R/'analysis.json').read_text());rows=[]
for t in d['trials']:
 if not t['complete'] or t['config']['mode']!='ours':continue
 result=json.loads((R/t['name']/'guest-out/result.json').read_text());steps=[s for s in result['steps'] if not s.get('skipped')]
 hinted=[s for s in steps if isinstance(s.get('placement_hint'),dict) and 'preferred_node' in s['placement_hint']]
 fast=t['config']['fast_node'];selected=next(s for s in steps if s['source_index']==t['selected_index'])
 rows.append(dict(trace=t['trace'],strategy=t['strategy'],repeat=t['repeat'],allocation_hook_enabled=bool(t['config'].get('allocation')),executed_calls=len(steps),
  calls_with_hint=len(hinted),calls_initially_prefer_fast=sum(s['placement_hint']['preferred_node']==fast for s in hinted),
  fast_preference_fraction=sum(s['placement_hint']['preferred_node']==fast for s in hinted)/len(hinted) if hinted else None,
  selected_index=t['selected_index'],selected_hint=selected.get('placement_hint'),
  initial_fast_allocation_pages=t['stats'].get('allocation_fast'),initial_slow_allocation_pages=t['stats'].get('allocation_slow')))
(R/'phase-signal.json').write_text(json.dumps(dict(trials=rows,limits=[
 'Preference is a start-time decision; actual placement can fall back and is separately audited',
 'Allocation counts are hook counters: zero when the hook is disabled does not mean zero physical DRAM allocation',
 'Counts are calls within independent VMs; calls are not independent repetitions',
 'Reactive comparison removes event kind and boundary information together; it is the current implementation, not optimal SOTA',
 'No inference-delay estimate is claimed from these hints']),indent=2)+'\n')
print('phase-signal diagnostics',len(rows))
