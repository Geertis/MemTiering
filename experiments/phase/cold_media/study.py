#!/usr/bin/env python3
"""Run and retain the probe then the full-wait quota matrix and its report."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

H = Path(__file__).resolve().parent
W = H.parents[1]


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--lane', type=int, default=1)
    p.add_argument('--repeats', type=int, default=3)
    a = p.parse_args()
    root = a.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    started = time.time()
    state = dict(started_epoch=started, pid=os.getpid(), root=str(root),
                 lane=a.lane, repeats=a.repeats, stage='starting')
    def status(**values):
        state.update(values, updated_epoch=time.time())
        tmp = root / 'status.json.tmp'
        tmp.write_text(json.dumps(state, indent=2) + '\n')
        tmp.replace(root / 'status.json')
    def run(script, *args):
        status(stage=script, command=[str(H / script), *map(str, args)])
        subprocess.run([sys.executable, str(H / script), *map(str, args)], cwd=W, check=True)
    try:
        inputs = root / 'state-input'
        if not inputs.exists():
            run('make_probe.py', '--output', inputs)
        probe = root / 'state-probe'
        run('run_matrix.py', '--root', probe, '--catalog', inputs / 'catalog.json',
            '--traces', 'state-probe', '--configs', 'cxl-dram', 'ssd-dram', 'cxl-512', 'ssd-512',
            '--repeats', 1, '--lane', a.lane)
        run('analyze.py', '--root', probe, '--min-repeats', 1)
        run('audit_probe.py', '--root', probe)
        formal = root / 'formal'
        run('run_matrix.py', '--root', formal, '--repeats', a.repeats, '--lane', a.lane)
        if not (formal / 'finished.json').exists():
            status(stage='stopped-after-current')
            return
        run('analyze.py', '--root', formal)
        run('plot.py', '--root', formal)
        status(stage='finished', elapsed_s=time.time() - started)
    except Exception as error:
        status(stage='failed', error=repr(error), elapsed_s=time.time() - started)
        raise


if __name__ == '__main__':
    main()
