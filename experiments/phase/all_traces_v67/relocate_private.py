#!/usr/bin/env python3
"""Place this experiment's resident non-Guest-RAM VMM pages before replay.

Only pages actually mapped by the verified VMM are queried. Guest RAM is never
moved here: its two memory-zone policies are validated by the main observer.
Unmovable and nonresident pages remain explicit, auditable outcomes.
"""
import argparse
import os
import ctypes
import http.client
import json
import socket
import time
from pathlib import Path

p=argparse.ArgumentParser()
p.add_argument('--api',type=Path,required=True)
p.add_argument('--node',type=int,required=True)
p.add_argument('--output',type=Path,required=True)
a=p.parse_args()

class UnixHTTP(http.client.HTTPConnection):
    def connect(self):
        self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(a.api))

c=UnixHTTP('localhost',timeout=3)
c.request('GET','/api/v1/vmm.ping')
r=c.getresponse();assert r.status==200
pid=json.loads(r.read())['pid'];c.close()
proc=Path('/proc')/str(pid)
cmd=(proc/'cmdline').read_bytes()
assert cmd.startswith(str(Path(os.environ['PHASE_TIEING_ROOT'])/'backend/bin/cloud-hypervisor').encode()) and b'.scratch-' in cmd
addresses=[]
ram_bytes=0
for line in (proc/'maps').read_text().splitlines():
    fields=line.split();lo,hi=[int(v,16) for v in fields[0].split('-')]
    if hi-lo >= (1<<30) and 'memfd:' in line:
        ram_bytes+=hi-lo
        continue
    # Device mappings and kernel-provided helper pages are reported separately.
    if fields[1][0]!='r' or any(x in line for x in ['[vvar','[vsyscall]','/dev/kvm']):continue
    addresses.extend(range(lo,hi,4096))
c=UnixHTTP('localhost',timeout=3)
c.request('GET','/api/v1/vm.info')
r=c.getresponse();assert r.status==200
config=json.loads(r.read())['config'];c.close()
expected_ram_bytes=sum(zone['size'] for zone in config['memory']['zones'])
assert expected_ram_bytes in (12*(1<<30),16*(1<<30)), 'unsupported experiment raw geometry'
assert ram_bytes==expected_ram_bytes, 'Guest RAM mapping differs from the actual VMM configuration'
libc=ctypes.CDLL(None,use_errno=True)
move=libc.syscall
move.restype=ctypes.c_long
started=time.monotonic()
result=dict(pid=pid,target_node=a.node,epoch=time.time(),queried_pages=len(addresses),
    resident_before_nodes={},move_statuses={},query_statuses={},requested_moves=0,
    guest_ram_excluded_bytes=ram_bytes,flags='MPOL_MF_MOVE; shared pages may be unmovable')
for first in range(0,len(addresses),4096):
    chunk=addresses[first:first+4096]
    addrs=(ctypes.c_void_p*len(chunk))(*chunk)
    status=(ctypes.c_int*len(chunk))()
    rc=move(279,ctypes.c_int(pid),ctypes.c_ulong(len(chunk)),addrs,None,status,ctypes.c_int(0))
    if rc<0:raise OSError(ctypes.get_errno(),'move_pages query')
    selected=[]
    for addr,n in zip(chunk,status):
        key=str(n);result['query_statuses'][key]=result['query_statuses'].get(key,0)+1
        if n>=0:
            result['resident_before_nodes'][key]=result['resident_before_nodes'].get(key,0)+1
            if n!=a.node:selected.append(addr)
    if not selected:continue
    addrs=(ctypes.c_void_p*len(selected))(*selected)
    target=(ctypes.c_int*len(selected))(*([a.node]*len(selected)))
    status=(ctypes.c_int*len(selected))()
    rc=move(279,ctypes.c_int(pid),ctypes.c_ulong(len(selected)),addrs,target,status,ctypes.c_int(2))
    if rc<0:raise OSError(ctypes.get_errno(),'move_pages placement')
    result['requested_moves']+=len(selected)
    for n in status:
        key=str(n);result['move_statuses'][key]=result['move_statuses'].get(key,0)+1
result['elapsed_s']=time.monotonic()-started
result['numa_maps_after']=(proc/'numa_maps').read_text()
a.output.write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps({k:v for k,v in result.items() if k!='numa_maps_after'}))
