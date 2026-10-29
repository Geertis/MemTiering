#!/usr/bin/env python3
"""Diagnose finite allocation credit exhaustion against actual active occupancy."""
import argparse,json
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);a=p.parse_args();R=a.root.resolve()
d=json.loads((R/'analysis.json').read_text());rows=[]
for t in d['trials']:
 if not t['complete'] or t['config']['mode']!='ours' or not t['config'].get('allocation'):continue
 seen=set();active=[]
 for line in (R/t['name']/'host-capacity.jsonl').read_text().splitlines():
  s=json.loads(line)
  if s['event']!='sample' or not s.get('guest_fresh'):continue
  g=s['guest'];ns=g['guest_monotonic_ns']
  if ns in seen:continue
  seen.add(ns);sb=g['sandbox']
  if sb.get('phase')==1 and sb.get('quota_pages',0)>0:active.append(sb)
 rows.append(dict(trace=t['trace'],strategy=t['strategy'],repeat=t['repeat'],active_snapshots=len(active),
  zero_credit_snapshots=sum(s.get('allocation_remaining')==0 for s in active),
  zero_credit_at_least20pct_quota_free_snapshots=sum(s.get('allocation_remaining')==0 and s['fast_pages']<=.8*s['quota_pages'] for s in active),
  allocation_fast_total=t['stats'].get('allocation_fast'),allocation_slow_total=t['stats'].get('allocation_slow')))
(R/'credits.json').write_text(json.dumps(dict(trials=rows,limits=[
 'Snapshot fractions are descriptive, not independent repetitions or proven allocatable credit',
 'Cumulative allocation counts differ from live occupancy; feedback/inflight/migration races remain',
 'Disabled allocation hooks have no credit rule: their zero remaining counter is omitted, not interpreted as starvation',
 'Do not replenish quota solely from these snapshots; require verifiable real frees and conservation']),indent=2)+'\n')
print('allocation-credit diagnostics',len(rows))
