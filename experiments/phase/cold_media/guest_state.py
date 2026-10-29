#!/usr/bin/env python3
"""Stream Guest state on an existing private experiment SSH connection."""
import argparse
import json
import time
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

while True:
    try:
        nodes = {}
        for node in [0, 1]:
            text = Path(f'/sys/devices/system/node/node{node}/meminfo').read_text()
            nodes[str(node)] = {line.split()[2].rstrip(':'): int(line.split()[3]) * 1024
                                for line in text.splitlines() if len(line.split()) >= 5}
        params = Path('/sys/module/PhaseTieing_balloon/parameters')
        reporting = {f.name: int(f.read_text()) for f in params.glob('reporting_*')}
        marker = Path('/tmp/capacity-stage')
        print(json.dumps({'guest_monotonic_ns': time.monotonic_ns(),
                          'guest_epoch': time.time(), 'page_reporting_order': int(Path('/sys/module/page_reporting/parameters/page_reporting_order').read_text()), 'nodes': nodes, 'reporting': reporting,
                          'sandbox': pairs('/sys/kernel/mm/demeter/sandboxes/0/stats'),
                          'vmstat': pairs('/proc/vmstat'),
                          'memory_stat': pairs('/sys/fs/cgroup/agent-workload/memory.stat'),
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
