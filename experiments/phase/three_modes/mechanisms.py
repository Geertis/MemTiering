#!/usr/bin/env python3
"""Describe actual mechanism activity; sampled reuse is evidence, not accuracy."""
import argparse,csv,importlib.util,json,statistics as st
from collections import Counter,defaultdict
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);a=p.parse_args();R=a.root.resolve()
plan=json.loads((R/'plan.json').read_text());analysis=json.loads((R/'analysis.json').read_text())
spec=importlib.util.spec_from_file_location('frozen_decoder',Path(plan['artifact'])/'collect.py')
mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
rows=[]
for t in analysis['trials']:
 if not t['complete'] or t['config']['mode']!='ours':continue
 out=R/t['name'];path=out/'guest-out/diagnostics/sandbox-0.csv'
 records=[mod.decode(line) for line in path.read_text().splitlines()]
 events=Counter(r['event'] for r in records)
 decisions=[r for r in records if r['event']==4];moves=[r for r in records if r['event']==5];swaps=[r for r in records if r['event']==6]
 candidate_checks=[r for r in records if r['event']==3]
 drops={r['generation']:r for r in records if r['event']==2 and r.get('refs')==0}
 file_by_phase=Counter();prev_file=0
 for r in records:
  if r['event']==9:
   file_by_phase['active' if r['phase'] else 'wait']+=max(0,r['file_demotions_total']-prev_file)
   prev_file=r['file_demotions_total']
 # Index actual post-placement samples by exact mm generation and page address.
 samples=defaultdict(list)
 for r in records:
  if r['event']==10:samples[r['generation'],r['addr']>>12].append(r)
 hotops=[(r,r['generation'],r['addr']) for r in moves if r['up'] and r['errno']==0]
 hotops += [(r,r['generation'],r['hot_addr']) for r in swaps if r['errno']==0]
 evidence=[]
 creates={r['generation']:r for r in records if r['event']==1}
 operations=defaultdict(list)
 for r,generation,addr in hotops:
  operations[generation].append(r)
  ended=drops.get(generation,{}).get('time_ns')
  later=[s for s in samples[generation,addr>>12] if s['sample_time_ns']>r['time_ns'] and s['phase_seq']==r['phase_seq'] and (ended is None or s['sample_time_ns']<ended)]
  evidence.append(dict(time_ns=r['time_ns'],generation=generation,addr=addr,phase_seq=r['phase_seq'],
   predicted_hot_benefit_ns=r.get('benefit_ns',r.get('hot_benefit_ns')),cost_ns=r['end_to_end_ns'],
   postmove_sample_count=len(later),postmove_sample_weight=sum(s['weighted'] for s in later),
   observed_remaining_lifetime_ms=(ended-r['time_ns'])/1e6 if ended else None))
 remaining=[x['observed_remaining_lifetime_ms'] for x in evidence if x['observed_remaining_lifetime_ms'] is not None]
 costs=[x['cost_ns'] for x in evidence]
 predicted=[x['predicted_hot_benefit_ns'] for x in evidence if x['predicted_hot_benefit_ns'] is not None]
 s=t['stats'];row=dict(trace=t['trace'],strategy=t['strategy'],repeat=t['repeat'],valid=t['valid'],
  decisions=len(decisions),active_decisions=sum(r['phase']!=0 for r in decisions),
  decisions_with_hot=sum(r['hot']>0 for r in decisions),
  active_decisions_with_hot=sum(r['phase']!=0 and r['hot']>0 for r in decisions),
  decisions_with_pairs=sum(r['hot']>0 and r['cold']>0 for r in decisions),
  cost_gate_rejections=s.get('rejected_benefit',0),promotions=s.get('promotions',0),
  candidate_benefit_zero_checks=sum(r['benefit_zero'] for r in candidate_checks),
  candidate_benefit_le_cost_checks=sum(r['benefit_le_copy'] for r in candidate_checks),
  demotions=s.get('demotions',0),file_demotions=s.get('file_demotions',0),
  exchanges=s.get('exchanges',0),cross_mm_exchanges=s.get('cross_mm_exchanges',0),
  file_demotions_active=file_by_phase['active'],file_demotions_wait=file_by_phase['wait'],
  completed_mms=len(drops),completed_mm_lifetime_median_ms=st.median(r['observed_lifetime_ns']/1e6 for r in drops.values()) if drops else None,
  alloc_fast_pages=s.get('allocation_fast',0),alloc_slow_pages=s.get('allocation_slow',0),
  samples_received=s.get('samples_received',0),load_overflows=s.get('load_overflows',0),
  worker_cpu_s=s.get('worker_cpu_ns',0)/1e9,migration_s=s.get('migration_ns',0)/1e9,
  placement_s=s.get('placement_ns',0)/1e9,
  successful_hot_operations=len(hotops),sample_reuse_diagnostics_available=bool(events[10]),
  hot_remaining_lifetime_median_ms=st.median(remaining) if remaining else None,
  hot_operation_cost_median_us=st.median(costs)/1000 if costs else None,
  predicted_hot_benefit_median_us=st.median(predicted)/1000 if predicted else None,
  hot_operations_with_later_sample=sum(x['postmove_sample_count']>0 for x in evidence) if events[10] else None,
  diagnostic_records=len(records),events=dict(events),reuse_evidence=evidence if events[10] else None,
  hot_mm_contexts=[dict(generation=g,pid=creates.get(g,{}).get('pid'),comm=creates.get(g,{}).get('comm'),
   successful_operations=len(ops),observed_lifetime_ms=drops[g]['observed_lifetime_ns']/1e6 if g in drops else None,
   first_operation_age_ms=(min(o['time_ns'] for o in ops)-creates[g]['time_ns'])/1e6 if g in creates else None)
   for g,ops in operations.items()])
 rows.append(row)
result=dict(trials=rows,limits=['Sample-event diagnostics are normally disabled: missing event 10 means reuse cannot be evaluated',
 'Postmove PEBS is sparse and mode-dependent: absence does not imply no reuse',
 'Counters are operations/pages, not saved latency; lifetime is discovered mm age, not perfect process lifetime',
 'Post-operation lifetime is retrospective process survival, not observed useful page reuse or saved latency',
 'Predicted benefit and actual operation cost have different scopes for exchanges and are not realized net profit',
 'Heat-only removes the benefit model as a bundle, not an independent ablation of each feature',
 'No shared multivm pool/density or predicted-benefit accuracy is inferred from these counters'])
(R/'mechanisms.json').write_text(json.dumps(result,indent=2)+'\n')
if rows:
 columns=[k for k in rows[0] if k not in ['events','reuse_evidence','hot_mm_contexts']]
 with (R/'mechanisms.csv').open('w') as f:
  w=csv.DictWriter(f,columns,extrasaction='ignore');w.writeheader();w.writerows(rows)
 print(json.dumps([{k:r[k] for k in ['trace','strategy','repeat','active_decisions','active_decisions_with_hot','promotions','exchanges','demotions','file_demotions','cost_gate_rejections']} for r in rows]))
