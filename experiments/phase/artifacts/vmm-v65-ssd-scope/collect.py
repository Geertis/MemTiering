#!/usr/bin/env python3
"""Drain opt-in Demeter diagnostic FIFOs; observer lives outside workload cgroups."""
import argparse
import json
from pathlib import Path
import time

SCHEMAS = {
 1: ['pid','initial_kind'],
 2: ['observed_lifetime_ns','regions','refs','samples'],
 3: ['hot','cold','scanned','scan_fast','scan_slow','scan_rejected','benefit_zero','benefit_le_copy','sample_unresolved','regions','age_ns','refs','samples','max_benefit_ns'],
 4: ['fast_before','quota','hot','cold','copy_before_ns','mms','promote_attempts','demote_attempts','exchange_attempts','cross_attempts','same_pair_with_cross_alternative','swap_benefit_rejected','no_cold_break','budget_exhausted','feedback_stale','swap_pairs_considered','raw_cross_pairs','profitable_cross_pairs','fast_after','copy_after_ns'],
 5: ['up','errno_u64','stage','benefit_ns','copy_ns'],
 6: ['errno_u64','stage','hot_benefit_ns','cold_benefit_ns','copy_ns'],
 7: ['regions','old_epoch','new_epoch'],
 8: ['old_gate_positive_pairs','old_gate_cross_positive_pairs','old_gate_selected_attempts','reclaim_budget','placement_paused'],
 10: ['source','addr','period','weighted','latency','heat_shift','data_src','sample_time_ns'],
 9: ['file_scanned','file_eligible','file_attempts','file_demotions_total','file_failed_total','fast_after'],
}
HEAD = ['time_ns','serial','decision','phase_seq','phase','kind','event','generation','peer_generation','comm_hex','peer_comm_hex']

def decode(line):
    values=line.strip().split(',')
    if len(values)<len(HEAD): raise ValueError('truncated diagnostic record')
    result={k:(v if k.endswith('_hex') else int(v)) for k,v in zip(HEAD,values)}
    event=result['event']
    schema=SCHEMAS[event]
    if event==2 and len(values)-len(HEAD)==6:
        schema=schema+['lifetime_kind','lifetime_kind_valid']
    if event==5 and len(values)-len(HEAD) in (6,7,8):
        schema=schema+['end_to_end_ns']
        if len(values)-len(HEAD)>=7: schema=schema+['addr']
        if len(values)-len(HEAD)==8: schema=schema+['evidence']
    if event==6 and len(values)-len(HEAD) in (6,8,10):
        schema=['errno_u64','stage','hot_benefit_ns','cold_benefit_ns','exchange_cost_ns','end_to_end_ns']
        if len(values)-len(HEAD)>=8: schema=schema+['hot_addr','cold_addr']
        if len(values)-len(HEAD)==10: schema=schema+['hot_evidence','cold_evidence']
    if event==8 and len(values)-len(HEAD)==4: schema=schema[:-1]  # relative-v1/v2
    if len(values)-len(HEAD)!=len(schema): raise ValueError('diagnostic schema mismatch')
    result.update({k:int(v) for k,v in zip(schema,values[len(HEAD):])})
    if event==8: result.setdefault('placement_paused',0)
    for prefix in ('comm','peer_comm'):
        result[prefix]=bytes.fromhex(result.pop(prefix+'_hex')).split(b'\0',1)[0].decode(errors='replace')
    if 'errno_u64' in result:
        value=result.pop('errno_u64');result['errno']=value-(1<<64) if value>=(1<<63) else value
    return result

def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--slots',type=int,default=1);p.add_argument('--stop',type=Path,required=True);a=p.parse_args()
    a.output.mkdir(parents=True,exist_ok=True)
    paths=[Path('/sys/kernel/mm/demeter/sandboxes')/str(i) for i in range(a.slots)]
    logs=[(a.output/f'sandbox-{i}.csv').open('w',buffering=1) for i in range(a.slots)]
    records=[0]*a.slots
    def drain():
        for i,path in enumerate(paths):
            while True:
                block=(path/'events').read_text()
                if not block: break
                logs[i].write(block);records[i]+=block.count('\n')
    try:
        while not a.stop.exists():
            drain();time.sleep(.025)
        drain()
        state=[]
        for path in paths:
            state.append({'diagnostics':dict((k,int(v)) for k,v in (l.split() for l in (path/'diagnostics').read_text().splitlines())),
                          'stats':dict((k,int(v)) for k,v in (l.split() for l in (path/'stats').read_text().splitlines()))})
        (a.output/'final.json').write_text(json.dumps({'records':records,'slots':state},indent=2)+'\n')
    finally:
        for log in logs: log.close()
    (a.output/'done').touch()

if __name__=='__main__':main()
