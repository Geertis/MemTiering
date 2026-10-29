#!/usr/bin/env python3
"""Integrate whole-VMM DDR over actual tool-service and idle intervals."""
import argparse,bisect,csv,json
from collections import defaultdict
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);a=p.parse_args();R=a.root.resolve()
analysis=json.loads((R/'analysis.json').read_text());rows=[]
for t in analysis['trials']:
 if not t['complete']:continue
 out=R/t['name'];d=json.loads((out/'guest-out/result.json').read_text())
 samples=d['resource_samples'];start=min(x['monotonic_s'] for x in samples);end=max(x['monotonic_s'] for x in samples)
 phase_times=[x['monotonic_s'] for x in samples]
 phase_states=['service' if x['tool_index']>=0 else 'idle' for x in samples]
 points=[json.loads(x) for x in (out/'host-capacity.jsonl').read_text().splitlines()]
 points=[x for x in points if x['event']=='sample'];areas=defaultdict(float);duration=defaultdict(float);peaks=defaultdict(float)
 boundaries=phase_times
 for l,r in zip(points,points[1:]):
  lo=l['guest']['guest_monotonic_ns']/1e9;hi=r['guest']['guest_monotonic_ns']/1e9
  if hi-lo>1:continue
  lo=max(lo,start);hi=min(hi,end)
  if hi<=lo:continue
  inside=[lo,*[b for b in boundaries if lo<b<hi],hi]
  ddr=sum(v for k,v in l['rss_node_bytes'].items() if k in ['N0','N1'])/(1<<20)
  payload=l['guest'].get('cgroup',{}).get('memory_numa_stat',{})
  for b,e in zip(inside,inside[1:]):
   mid=(b+e)/2;index=max(0,bisect.bisect_right(phase_times,mid)-1);kind=phase_states[index]
   areas[kind]+=ddr*(e-b);duration[kind]+=e-b;peaks[kind]=max(peaks[kind],ddr)
 row=dict(trace=t['trace'],strategy=t['strategy'],repeat=t['repeat'],valid=t['valid'])
 for k in ['service','idle']:
  row[k+'_covered_s']=duration[k];row[k+'_ddr_mean_mib']=areas[k]/duration[k] if duration[k] else None;row[k+'_ddr_peak_mib']=peaks[k]
 rows.append(row)
result=dict(trials=rows,limits=['Service contains pre-tool notification; idle is replay scheduling idle, not tool-internal wait',
 'Service/idle boundaries are observed by the 50ms Guest sampler, not exact per-call timers',
 'Whole VMM RSS is sampled every 250ms; activity boundaries do not make samples independent'])
(R/'stages.json').write_text(json.dumps(result,indent=2)+'\n')
if rows:
 with (R/'stages.csv').open('w') as f:
  writer=csv.DictWriter(f,list(rows[0]));writer.writeheader();writer.writerows(rows)
print('stage integrals',len(rows))
