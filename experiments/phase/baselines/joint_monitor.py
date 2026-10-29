#!/usr/bin/env python3
"""Simultaneous RSS accounting of the two known experiment VMMs."""
import argparse
import json
import time
from pathlib import Path

p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);a=p.parse_args()
identities={}
with (a.root/'joint-host.jsonl').open('x',buffering=1) as out:
    while not (a.root/'joint-stop').exists():
        states=[]
        for lane in [0,1]:
            ready=a.root/f'lane{lane}/capacity-ready.json'
            if lane not in identities and ready.exists():
                pid=json.loads(ready.read_text())['pid'];proc=Path('/proc')/str(pid)
                cmd=(proc/'cmdline').read_bytes()
                assert b'/HW/Demeter/sosp25-demeter/hypervisor/' in cmd and b'.scratch-' in cmd
                identities[lane]=(pid,(proc/'stat').read_text().split(') ',1)[1].split()[19])
            if lane not in identities:
                states.append({'lane':lane,'observed':False});continue
            pid,start=identities[lane];proc=Path('/proc')/str(pid)
            try:
                assert (proc/'stat').read_text().split(') ',1)[1].split()[19]==start
                nodes={}
                for line in (proc/'numa_maps').read_text().splitlines():
                    fields=dict(v.split('=',1) for v in line.split() if '=' in v)
                    size=int(fields.get('kernelpagesize_kB',4))*1024
                    for k,v in fields.items():
                        if k.startswith('N') and k[1:].isdigit():nodes[k]=nodes.get(k,0)+int(v)*size
                rollup={}
                for line in (proc/'smaps_rollup').read_text().splitlines():
                    fields=line.split()
                    if len(fields)==3 and fields[2]=='kB':rollup[fields[0][:-1]]=int(fields[1])*1024
                states.append({'lane':lane,'pid':pid,'observed':True,'exited':False,'rss_node_bytes':nodes,'rollup_bytes':rollup})
            except (FileNotFoundError,ProcessLookupError):
                states.append({'lane':lane,'pid':pid,'observed':True,'exited':True,'rss_node_bytes':{},'rollup_bytes':{}})
        out.write(json.dumps({'host_monotonic_ns':time.monotonic_ns(),'epoch':time.time(),'vms':states,
            'ddr_rss_bytes':sum(sum(s.get('rss_node_bytes',{}).get(k,0) for k in ['N0','N1']) for s in states),
            'cxl_rss_bytes':sum(s.get('rss_node_bytes',{}).get('N2',0) for s in states),
            'pss_bytes':sum(s.get('rollup_bytes',{}).get('Pss',0) for s in states)})+'\n')
        time.sleep(.1)
