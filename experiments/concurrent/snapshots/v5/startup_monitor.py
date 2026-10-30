#!/usr/bin/env python3
"""Observe own VMM from process discovery through teardown, independently of SSH."""
import argparse
import json
import os
import time
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument('--disk', required=True)
p.add_argument('--output', type=Path, required=True)
p.add_argument('--stop', type=Path, required=True)
a = p.parse_args()
pid = None

def rss(proc):
    nodes = {}
    for line in (proc / 'numa_maps').read_text().splitlines():
        fields = dict(v.split('=', 1) for v in line.split() if '=' in v)
        size = int(fields.get('kernelpagesize_kB', 4)) * 1024
        for name, count in fields.items():
            if name.startswith('N') and name[1:].isdigit():
                nodes[name] = nodes.get(name, 0) + int(count) * size
    return nodes

with a.output.open('x', buffering=1) as log:
    while not a.stop.exists():
        if pid is None:
            for proc in Path('/proc').iterdir():
                if not proc.name.isdigit(): continue
                try:
                    cmd = (proc/'cmdline').read_bytes().split(b'\0')
                    if cmd and cmd[0].endswith(b'/cloud-hypervisor') and any(a.disk.encode() in x for x in cmd):
                        pid = int(proc.name)
                        break
                except (FileNotFoundError, PermissionError, ProcessLookupError): pass
        if pid is not None:
            try:
                proc = Path('/proc')/str(pid)
                rollup = {}
                for line in (proc/'smaps_rollup').read_text().splitlines():
                    v = line.split()
                    if len(v) == 3 and v[2] == 'kB': rollup[v[0].rstrip(':')] = int(v[1])*1024
                node_free = {}
                for n in sorted(int(x.name[4:]) for x in Path('/sys/devices/system/node').glob('node[0-9]*')):
                    for line in Path(f'/sys/devices/system/node/node{n}/meminfo').read_text().splitlines():
                        v=line.split()
                        if len(v)>=5 and v[2] in ['MemFree:','MemUsed:']: node_free[f'N{n}_{v[2][:-1]}'] = int(v[3])*1024
                log.write(json.dumps({'host_monotonic_ns':time.monotonic_ns(), 'epoch':time.time(),
                    'pid':pid, 'rss_node_bytes':rss(proc), 'rollup_bytes':rollup,
                    'host_node_memory_bytes':node_free,
                    'host_memory_psi':Path('/proc/pressure/memory').read_text().strip()})+'\n')
            except (FileNotFoundError, ProcessLookupError):
                log.write(json.dumps({'event':'process_exit','pid':pid,'epoch':time.time()})+'\n')
                break
        time.sleep(.1)
