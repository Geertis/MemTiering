#!/usr/bin/env python3
"""Persist a live anonymous state across WAIT and verify demand swap-in."""
import argparse
import json
from pathlib import Path
import shlex
from run_matrix import sha, W

CODE = '''import hashlib, json, pathlib, time
root = pathlib.Path('/tmp')
data = bytearray(256 * 1024 * 1024)
chunk = bytes(range(256)) * 4096
for offset in range(0, len(data), len(chunk)):
    data[offset:offset + len(chunk)] = chunk
before = hashlib.sha256(data).hexdigest()
(root / 'state-ready').write_text('ready')
while not (root / 'state-resume').exists():
    time.sleep(.05)
after = hashlib.sha256(data).hexdigest()
(root / 'state-result.json').write_text(json.dumps(dict(bytes=len(data), before=before, after=after, valid=before == after), sort_keys=True))
'''


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    a.output.mkdir(parents=True, exist_ok=False)
    command = 'python -c ' + shlex.quote(CODE) + ' >/tmp/state-worker.log 2>&1 &\n'
    command += 'for i in $(seq 1 200); do test -f /tmp/state-ready && exit 0; sleep .05; done; exit 1'
    calls = [dict(tool='Bash', id='state-start', input={'command': command},
                  timestamp='2026-01-01T00:00:00Z', end_timestamp='2026-01-01T00:00:01Z'),
             dict(tool='Bash', id='state-resume', input={'command':
                  'touch /tmp/state-resume; for i in $(seq 1 400); do test -f /tmp/state-result.json && exit 0; sleep .05; done; exit 1'},
                  timestamp='2026-01-01T00:00:13Z', end_timestamp='2026-01-01T00:00:14Z'),
             dict(tool='Read', id='state-check', input={'file_path': '/tmp/state-result.json'},
                  timestamp='2026-01-01T00:00:14Z', end_timestamp='2026-01-01T00:00:14.1Z')]
    (a.output / 'tool_calls.json').write_text(json.dumps(calls, indent=2) + '\n')
    original = next(c for c in json.loads((W / 'results/capacity-v2-20261009/catalog.json').read_text())['primary'] if c['name'] == 'dask-5510')
    case = dict(name='state-probe', trace=str(a.output.resolve()), golden=original['golden'],
                golden_sha256=original['golden_sha256'],
                input_hashes={'tool_calls.json': sha(a.output / 'tool_calls.json')})
    (a.output / 'catalog.json').write_text(json.dumps({'primary': [case]}, indent=2) + '\n')


if __name__ == '__main__':
    main()
