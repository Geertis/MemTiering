#!/usr/bin/env python3
"""Read-only progress of the long serial experiment."""
import argparse
import json
from pathlib import Path
import time

p = argparse.ArgumentParser()
p.add_argument('--root', type=Path, required=True)
a = p.parse_args()
state = json.loads((a.root / 'status.json').read_text())
print(json.dumps(state, indent=2))
for cohort in ['state-probe', 'formal']:
    root = a.root / cohort
    if not (root / 'plan.json').exists():
        continue
    plan = json.loads((root / 'plan.json').read_text())
    receipts = [json.loads(p.read_text()) for p in root.glob('*.receipt.json')]
    completed = {r['job']['name'] for r in receipts}
    active = [j['name'] for j in plan['jobs'] if j['name'] not in completed and (root / j['name']).exists()]
    print(cohort, 'completed', len(receipts), '/', len(plan['jobs']),
          'failed', sum(r['exit_code'] != 0 for r in receipts), 'active', active)
    durations = [r['wall_s'] for r in receipts if r['exit_code'] == 0]
    if durations:
        print('remaining hours estimate', round((len(plan['jobs']) - len(receipts)) * sum(durations) / len(durations) / 3600, 1))
