"""Check the private L1 capacity, swap, KVM and frozen workload inputs."""
import hashlib
import json
from pathlib import Path
import subprocess
import time
from environment import W

ROOT = W / 'results/PhaseTieing-concurrent-20261030-v6-adaptive-reserve'


def main():
    result = dict(epoch=time.time(), uname=subprocess.check_output(['uname', '-a'], text=True),
        cpu_count=len(__import__('os').sched_getaffinity(0)),
        topology=subprocess.check_output(['numactl', '--hardware'], text=True),
        meminfo=Path('/proc/meminfo').read_text(), swaps=Path('/proc/swaps').read_text(),
        filesystems=subprocess.check_output(['findmnt', '-J'], text=True), inputs=[])
    assert result['cpu_count'] == 4
    assert len(result['swaps'].splitlines()) == 1
    assert Path('/dev/kvm').exists()
    for case in json.loads((ROOT / 'inputs/cases.json').read_text()):
        with Path(case['golden']).open('rb') as stream:
            golden = hashlib.file_digest(stream, 'sha256').hexdigest()
        assert golden == case['golden_sha256'], case['name']
        for name, expected in case['input_hashes'].items():
            assert hashlib.sha256((Path(case['trace']) / name).read_bytes()).hexdigest() == expected
        result['inputs'].append(dict(name=case['name'], golden_sha256=golden, trace_sha256=case['input_hashes']))
        print('INPUT_PASS', case['name'], flush=True)
    (ROOT / 'preflight').mkdir(exist_ok=True)
    (ROOT / 'preflight/l1-inputs.json').write_text(json.dumps(result, indent=2) + '\n')
    print('L1_PREFLIGHT_PASS', flush=True)


if __name__ == '__main__':
    main()
