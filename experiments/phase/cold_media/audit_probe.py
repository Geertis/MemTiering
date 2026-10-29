#!/usr/bin/env python3
"""Verify all preserved bytes, real SSD round trip and WAIT physical release."""
import argparse
import hashlib
import json
from pathlib import Path
import statistics


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--root', type=Path, required=True)
    a = p.parse_args()
    analysis = json.loads((a.root / 'analysis.json').read_text())
    chunk = bytes(range(256)) * 4096
    digest = hashlib.sha256()
    for _ in range(256):
        digest.update(chunk)
    expected = json.dumps(dict(bytes=256 * 1024 * 1024, before=digest.hexdigest(),
                               after=digest.hexdigest(), valid=True), sort_keys=True).encode()
    expected_sha = hashlib.sha256(expected).hexdigest()
    rows = []
    for trial in analysis['rows']:
        if not trial['valid']:
            raise SystemExit('invalid physical trial: ' + str(trial['reasons']))
        out = a.root / trial['name']
        result = json.loads((out / 'guest-out/result.json').read_text())
        first, resume, check = result['steps']
        if result['status_vector'] != [0, 0, 0] or check['stdout_sha256'] != expected_sha:
            raise SystemExit('live state contents changed across WAIT/resume')
        records = [json.loads(line) for line in (out / 'host-capacity.jsonl').read_text().splitlines()]
        records = [r for r in records if r['event'] == 'sample']
        def ddr(r):
            return sum(r['rss_node_bytes'].get(n, 0) for n in ['N0', 'N1']) / (1 << 20)
        start = first['service_finished_monotonic_s']
        stop = resume['service_started_monotonic_s']
        early = [ddr(r) for r in records if start <= r['guest']['guest_monotonic_ns'] / 1e9 <= start + 1]
        late = [ddr(r) for r in records if stop - 2 <= r['guest']['guest_monotonic_ns'] / 1e9 <= stop - .5]
        row = dict(strategy=trial['strategy'], contents_verified=True,
                   observed_wait_ddr_drop_mib=statistics.median(early) - statistics.median(late),
                   swap_peak_mib=trial['swap_peak_mib'], pswpin=trial['pswpin'],
                   pswpout=trial['pswpout'], e2e_s=trial['e2e_s'])
        if trial['config']['media'] == 'ssd' and trial['config']['phase']:
            if trial['pswpin'] <= 0 or trial['pswpout'] <= 0 or row['observed_wait_ddr_drop_mib'] <= 0:
                raise SystemExit('SSD did not demonstrate physical release plus round trip')
        rows.append(row)
    (a.root / 'state-integrity.json').write_text(json.dumps(dict(expected_sha256=expected_sha, trials=rows), indent=2) + '\n')
    print(json.dumps(rows, indent=2))


if __name__ == '__main__':
    main()
