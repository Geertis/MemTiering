#!/usr/bin/env python3
"""Derive Linux Guest node roles from SRAT and actual VMM zone mappings."""
import argparse
import http.client
import json
import re
import socket
from pathlib import Path

p=argparse.ArgumentParser()
p.add_argument('--api',type=Path,required=True)
p.add_argument('--boot-log',type=Path,required=True)
p.add_argument('--output',type=Path,required=True)
p.add_argument('--zones',required=True)
p.add_argument('--phase',type=int,choices=[0,1],required=True)
p.add_argument('--fast-node',type=int,required=True)
p.add_argument('--slow-node',type=int,required=True)
a=p.parse_args()
class UnixHTTP(http.client.HTTPConnection):
    def connect(self):
        self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(a.api))
c=UnixHTTP('localhost',timeout=3);c.request('GET','/api/v1/vm.info')
r=c.getresponse();assert r.status==200
cfg=json.loads(r.read())['config'];c.close()
zones={z['id']:z['host_numa_node'] for z in cfg['memory']['zones']}
assert [zones[k] for k in ['dram','slow']]==[int(x) for x in a.zones.split(',')]
pxm={n['guest_numa_id']:zones[n['memory_zones'][0]] for n in cfg['numa'] if n.get('memory_zones')}
log=a.boot_log.read_text(errors='replace')
guest_host={int(n):pxm[int(p)] for n,p in re.findall(r'SRAT: Node (\d+) PXM (\d+) \[mem',log)}
assert len(guest_host)==2, 'missing actual Guest/Linux node mapping'
cpu_nodes=sorted({int(n) for n in re.findall(r'SRAT: PXM \d+ -> APIC \S+ -> Node (\d+)',log)})
assert cpu_nodes==[0], 'unexpected Linux CPU node'
result=dict(guest_to_host_numa=guest_host,guest_cpu_nodes=cpu_nodes,
            requested_fast_node=a.fast_node,requested_slow_node=a.slow_node,
            pxm_to_host_numa=pxm,zone_to_host_numa=zones,phase=bool(a.phase))
a.output.write_text(json.dumps(result,indent=2)+'\n')
if a.phase:
    expected=[int(x) for x in a.zones.split(',')]; assert expected[0]!=expected[1] and guest_host[a.fast_node]==expected[0] and guest_host[a.slow_node]==expected[1], 'Guest fast/slow labels contradict declared L1 tiers'
print(json.dumps(result))
