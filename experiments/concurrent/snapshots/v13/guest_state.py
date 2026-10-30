#!/usr/bin/env python3
"""Stream Guest state on an existing private experiment SSH connection."""
import argparse
import json
import time
import sys
import threading
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument('--interval', type=float, default=.25)
a = p.parse_args()

def pairs(path):
    result = {}
    try:
        for line in Path(path).read_text().splitlines():
            fields = line.split()
            if len(fields) == 2:
                result[fields[0]] = int(fields[1])
    except FileNotFoundError:
        pass
    return result

def controls():
    root = Path('/sys/module/page_reporting/parameters')
    result = {}
    for name in ['page_reporting_paused', 'page_reporting_runs',
                 'page_reporting_batches', 'page_reporting_fine_batches', 'page_reporting_pause_wait_ns', 'page_reporting_barrier_wait_ns']:
        value = (root/name).read_text().strip()
        result[name] = value.lower() in ('y', '1') if name.endswith('_paused') else int(value)
    return result

def receive_pressure():
    for line in sys.stdin:
        try:
            data = json.loads(line)
            data['received_guest_s'] = time.monotonic()
            path = Path('/tmp/host-pressure.json.tmp')
            path.write_text(json.dumps(data))
            path.replace('/tmp/host-pressure.json')
        except (ValueError, OSError):
            pass
threading.Thread(target=receive_pressure, daemon=True).start()

def capacity_state():
    path = Path('/mnt/workload/out/controller.jsonl')
    if not path.exists():
        return None
    with path.open('rb') as stream:
        stream.seek(max(0, path.stat().st_size - 65536))
        lines = stream.read().splitlines()
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get('event') in ['capacity_tick', 'capacity_phase']:
            result = {k: row.get(k) for k in ['active', 'phase_id', 'target_mib', 'measurement', 'monotonic_ns', 'controller_pid', 'controller_start_ticks']}
            result['controller_age_s'] = max(0,(time.monotonic_ns()-row['monotonic_ns'])/1e9)
            pid = row.get('controller_pid')
            result['controller_alive'] = None
            if type(pid) is int:
                try:
                    fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()
                    result['controller_alive'] = fields[0] not in ['Z','X'] and int(fields[19])==row.get('controller_start_ticks')
                except (OSError,ValueError,IndexError):result['controller_alive']=False
            return result
    return None

while True:
    try:
        nodes = {}
        for node in [0, 1]:
            text = Path(f'/sys/devices/system/node/node{node}/meminfo').read_text()
            nodes[str(node)] = {line.split()[2].rstrip(':'): int(line.split()[3]) * 1024
                                for line in text.splitlines() if len(line.split()) >= 5}
        params = Path('/sys/module/demeter_balloon/parameters')
        reporting = {f.name: int(f.read_text()) for f in params.glob('reporting_*')}
        marker = Path('/tmp/capacity-stage')
        print(json.dumps({'guest_monotonic_ns': time.monotonic_ns(), 'capacity_state': capacity_state(),
                          'guest_epoch': time.time(), 'page_reporting_order': int(Path('/sys/module/page_reporting/parameters/page_reporting_order').read_text()), 'nodes': nodes, 'reporting': reporting,
                          'sandbox': pairs('/sys/kernel/mm/demeter/sandboxes/0/stats'),
                          'vmstat': pairs('/proc/vmstat'),
                          'meminfo': {line.split(':',1)[0]:int(line.split()[1])*1024
                                      for line in Path('/proc/meminfo').read_text().splitlines()
                                      if len(line.split())==3 and line.split()[2]=='kB'},
                          'memory_stat': pairs('/sys/fs/cgroup/agent-workload/memory.stat'),
                          'memory_current': int(Path('/sys/fs/cgroup/agent-workload/memory.current').read_text()),
                          'memory_numa_stat': Path('/sys/fs/cgroup/agent-workload/memory.numa_stat').read_text(),
                          'memory_events': pairs('/sys/fs/cgroup/agent-workload/memory.events'),
                          'io_stat': Path('/sys/fs/cgroup/agent-workload/io.stat').read_text() if Path('/sys/fs/cgroup/agent-workload/io.stat').exists() else None,
                          'swap_bytes': int(Path('/sys/fs/cgroup/agent-workload/memory.swap.current').read_text()),
                          'memory_high': Path('/sys/fs/cgroup/agent-workload/memory.high').read_text().strip(),
                          'memory_max': Path('/sys/fs/cgroup/agent-workload/memory.max').read_text().strip(),
                          'swap_max': Path('/sys/fs/cgroup/agent-workload/memory.swap.max').read_text().strip(),
                          'swaps': Path('/proc/swaps').read_text(),
                          'diskstats': Path('/proc/diskstats').read_text(),
                          'psi': Path('/sys/fs/cgroup/agent-workload/memory.pressure').read_text(),
                          'reporting_control': controls(),
                          # Counts by order expose free-list fragmentation,
                          # not per-PFN physical Host residency.
                          'buddyinfo': Path('/proc/buddyinfo').read_text(),
                          'stage': marker.read_text().strip() if marker.exists() else None}), flush=True)
        time.sleep(a.interval)
    except BrokenPipeError:
        break
