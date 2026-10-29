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
                          'stage': marker.read_text().strip() if marker.exists() else None}), flush=True)
        time.sleep(a.interval)
    except BrokenPipeError:
        break
