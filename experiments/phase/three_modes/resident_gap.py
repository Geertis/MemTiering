#!/usr/bin/env python3
"""Conservative diagnostic lower bound on resident but Guest-free DDR backing."""
import argparse,csv,json,statistics as st
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);a=p.parse_args();R=a.root.resolve()
plan=json.loads((R/'plan.json').read_text());data=json.loads((R/'analysis.json').read_text());rows=[];reporting=[]
for t in data['trials']:
 if not t['complete']:continue
 points=[json.loads(x) for x in (R/t['name']/'host-capacity.jsonl').read_text().splitlines()]
 resources=json.loads((R/t['name']/'guest-out/result.json').read_text())['resource_samples']
 lo=min(s['monotonic_s'] for s in resources);hi=max(s['monotonic_s'] for s in resources)
 points=[s for s in points if s['event']=='sample' and s.get('guest_fresh') and lo<=s['guest']['guest_monotonic_ns']/1e9<=hi]
 if points:
  initial=points[0]['guest']['reporting'];final=points[-1]['guest']['reporting']
  reporting.append(dict(trace=t['trace'],strategy=t['strategy'],repeat=t['repeat'],
   reporting_order=points[-1]['guest']['page_reporting_order'],
   replay_deltas={k:final[k]-initial.get(k,0) for k in final}))
 if t['config']['mode']!='ours':continue
 points=[s for s in points if s['guest']['sandbox'].get('phase')==0]
 values=[]
 for s in points:
  if not s.get('guest_fresh'):continue
  node=t['config'].get('fast_node',1)
  n=s['guest']['nodes'][str(node)]
  raw=8192*(1<<20)
  # Symmetric 2 GiB balloons in both zones avoid indexing ambiguity.
  balloon=s['balloon_actual_bytes'];assert balloon[0]==balloon[1]
  upper_nonfree=raw-balloon[0]-n['MemFree']
  guest_ddr=sum(v for k,v in s['rss_breakdown']['guest_ram'].items() if k in ['N0','N1'])
  values.append(dict(guest_ns=s['guest']['guest_monotonic_ns'],
   ddr_resident_mib=guest_ddr/(1<<20),guest_nonfree_upper_mib=upper_nonfree/(1<<20),
   resident_free_lower_mib=max(0,guest_ddr-upper_nonfree)/(1<<20),
   payload_fast_mib=s['guest']['sandbox'].get('fast_pages',0)*4096/(1<<20)))
 if values:
  rows.append(dict(trace=t['trace'],strategy=t['strategy'],repeat=t['repeat'],samples=len(values),
   wait_snapshot_ddr_median_mib=st.median(v['ddr_resident_mib'] for v in values),
   wait_snapshot_free_lower_median_mib=st.median(v['resident_free_lower_mib'] for v in values),
   final_snapshot=values[-1]))
(R/'resident-gap.json').write_text(json.dumps(dict(trials=rows,reporting_activity=reporting,limits=[
 'Diagnostic lower bound, not an exact split of kernel/system/free pages',
 'Assumes acknowledged balloon pages have dropped their Host backing; receipt and equal balloons audited',
 'Guest and Host snapshots are asynchronous; do not use this as allocatable credit',
 'MemFree excludes all nonfree/reserved pages conservatively; reporting fragmentation/delay is not causally proven',
 'WAIT snapshot median is not a time-weighted WAIT mean']),indent=2)+'\n')
print('WAIT residency-gap diagnostics',len(rows))
