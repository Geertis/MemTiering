#!/usr/bin/env python3
"""Audit replay paths; keep post-hoc exclusions separate from headline metrics."""
import json
import re
import statistics
from pathlib import Path

W = Path(__file__).resolve().parents[2]
R = W / 'results/baselines-20261009'
med = statistics.median
records = []
for cohort in ['screen', 'realtime', 'whole-ram-v3']:
    root = R / cohort
    plan = json.loads((root / 'plan.json').read_text())
    for job in plan['jobs']:
        path = root / job['name'] / 'guest-out/result.json'
        receipt = root / (job['name'] + '.receipt.json')
        if not path.exists() or not receipt.exists():
            continue
        data = json.loads(path.read_text())
        steps = [s for s in data['steps'] if not s.get('skipped')]
        evidence = []
        dns_indices = []
        for s in steps:
            output = (s.get('stdout_tail') or '') + '\n' + (s.get('stderr_tail') or '')
            flags = []
            if 'Temporary failure in name resolution' in output:
                flags.append('DNS retry/failure')
                dns_indices.append(s['source_index'])
            if 'Traceback (most recent call last)' in output:
                flags.append('visible traceback')
            if re.search(r'\b[1-9]\d* (?:failed|errors?)\b', output):
                flags.append('visible test failure/error')
            if flags:
                evidence.append({
                    'source_index': s['source_index'], 'status': s['status'],
                    'execution_s': s['execution_s'], 'flags': flags,
                    'output_tail': output[-4000:],
                })
        total = sum(s['execution_s'] for s in steps)
        dns = sum(s['execution_s'] for s in steps if s['source_index'] in dns_indices)
        remaining = [s for s in steps if s['source_index'] not in dns_indices]
        records.append({
            'cohort': cohort, **job,
            'execution_s': total, 'dns_failure_call_execution_s': dns,
            'dns_failure_call_fraction': dns / total,
            'dns_failure_source_indices': dns_indices,
            'execution_excluding_dns_calls_s': sum(s['execution_s'] for s in remaining),
            'service_excluding_dns_calls_s': sum(s['elapsed_s'] for s in remaining),
            'evidence': evidence,
        })
summaries = []
for cohort, trace in dict.fromkeys((r['cohort'], r['trace']) for r in records):
    group = [r for r in records if r['cohort'] == cohort and r['trace'] == trace]
    refs = [r for r in group if r['policy'] == 'dram']
    if not refs:
        continue
    for policy in dict.fromkeys(r['policy'] for r in group):
        rows = [r for r in group if r['policy'] == policy]
        summaries.append({
            'cohort': cohort, 'trace': trace, 'policy': policy,
            'independent_vms': len(rows),
            'dns_call_fraction_median': med(r['dns_failure_call_fraction'] for r in rows),
            'dns_failure_source_indices': sorted(set(i for r in rows for i in r['dns_failure_source_indices'])),
            'remaining_execution_median_s': med(r['execution_excluding_dns_calls_s'] for r in rows),
            'remaining_execution_ratio_to_dram': med(r['execution_excluding_dns_calls_s'] for r in rows) /
                med(r['execution_excluding_dns_calls_s'] for r in refs),
            'remaining_service_ratio_to_dram': med(r['service_excluding_dns_calls_s'] for r in rows) /
                med(r['service_excluding_dns_calls_s'] for r in refs),
        })
output = {
    'limits': [
        'Post-hoc diagnostic; headline all-call metrics retain these calls',
        'DNS-call elapsed time includes startup/execution, not isolated network-wait time',
        'Excluding DNS calls does not establish remaining task success or a valid compute benchmark',
        'Visible output-tail evidence can miss errors outside retained tails; status zero may mask failure',
    ],
    'records': records, 'summaries': summaries,
}
(R / 'tool-quality-audit.json').write_text(json.dumps(output, indent=2) + '\n')
for row in summaries:
    if row['trace'] == 'github3-673':
        print(json.dumps(row))
