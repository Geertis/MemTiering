"""No-oversubscription profiles and concurrency baseline; halt on any anomaly."""
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import random
import subprocess
import time

from outer import ROOT, W, start, stop, ssh, save
from deploy import transfer, provision
from campaign import update, host_audit, run, source_guard

H = Path(__file__).resolve().parent
HOST = 'v3-wide-96g'


def read(path):
    return json.loads(Path(path).read_text())


def profile_bound(label):
    folder = ROOT / 'cohorts' / label
    peak = 0
    with gzip.open(folder / 'l1-resources.jsonl.gz', 'rt') as stream:
        for line in stream:
            point = json.loads(line)
            peak = max(peak, point['memory']['MemTotal'] - point['memory']['MemAvailable'])
    # This upper bound includes shared L1 overhead on every single-VM run.
    # Summing it repeats shared overhead and is deliberately conservative.
    return dict(label=label, whole_l1_peak_unavailable_bytes=peak,
                upper_bound_gib=peak / (1 << 30))


def make_plans():
    cases = read(ROOT / 'inputs/cases.json')
    ordered = sorted(cases, key=lambda c: next(i for i, name in enumerate(
        ['numba__', 'dask__', 'pydicom__', 'streamlink__']) if name in c['name']))
    matrix = []
    for repetition in range(1, 4):
        seed = 20261029 + repetition - 1
        for case in ordered:
            short = next(name for name in ['numba', 'dask', 'pydicom', 'streamlink']
                         if '__' + name in case['name'])
            matrix.append(dict(label=f'v3-s1-{short}-r{repetition}', category='S1', strategy='REF',
                medium='dram', dram_gib=96, cxl_gib=0, reference=None, n=1,
                repetition=repetition, seed=seed, slots=[dict(lane=0, case=case['name'],
                    offset_s=0, deadline_s=None)]))
        for n in [4, 8]:
            rng = random.Random(seed + n * 1000)
            chosen = [cases[i % 4] for i in range(n)]
            rng.shuffle(chosen)
            matrix.append(dict(label=f'v3-c0-n{n}-r{repetition}', category='C0', strategy='REF',
                medium='dram', dram_gib=96, cxl_gib=0, reference=None, n=n,
                repetition=repetition, seed=seed, slots=[dict(lane=i, case=case['name'],
                    offset_s=round((i + rng.random()) * 10 / n, 6), deadline_s=None)
                    for i, case in enumerate(chosen)]))
    save(ROOT / 'matrix.json', matrix)
    return matrix


def deploy_wide():
    source_guard()
    start(HOST, 96, 0)
    host_audit(HOST)
    provision()
    transfer([H, ROOT / 'inputs/cases.json', ROOT / 'protocol.json'], 'v3-sources-and-protocol')
    cases = read(ROOT / 'inputs/cases.json')
    checks = [(case['golden'], case['golden_sha256']) for case in cases]
    for case in cases:
        checks += [
            (str(Path(case['trace']) / name), digest) for name, digest in case['input_hashes'].items()]
    code = f'''from pathlib import Path
import hashlib,json,os
assert os.cpu_count()==36
assert len(Path('/proc/swaps').read_text().splitlines())==1
for name,expected in {checks!r}:
 with Path(name).open('rb') as stream: actual=hashlib.file_digest(stream,'sha256').hexdigest()
 assert actual==expected,(name,actual,expected)
assert Path('{W}/validated-root-disks/root-demeter.qcow2').is_file()
for path in Path('{W}').glob('.scratch-*'):
 if path.is_file() and path.suffix in ['.ext4','.qcow2']:path.unlink()
print(json.dumps(dict(vcpus=os.cpu_count(),swap=False,verified_inputs={len(checks)})))
'''
    result = ssh("sudo python3 - <<'VERIFY'\n" + code + '\nVERIFY\n', timeout=180,
                 capture_output=True, text=True, check=True)
    save(ROOT / 'preflight.json', dict(epoch=time.time(), stdout=result.stdout, stderr=result.stderr))


def main():
    with (ROOT / 'baseline.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        save(ROOT / 'baseline-process.json', dict(pid=os.getpid(), started_epoch=time.time()))
        matrix = make_plans()
        completed = []
        failed = []
        profiles = {}
        update('booting-wide-baseline', active=HOST, completed=completed, failed=failed)
        try:
            deploy_wide()
            for spec in matrix:
                source_guard()
                if spec['n'] > 1:
                    required = sum(p['whole_l1_peak_unavailable_bytes'] for p in profiles.values())
                    required *= spec['n'] // 4
                    required += 8 * (1 << 30)
                    gate = dict(n=spec['n'], required_upper_bound_gib=required / (1 << 30),
                        shared_dram_gib=96, margin_gib=8,
                        rule='sum of single-instance whole-L1 peak unavailable (shared overhead repeated) + 8GiB margin')
                    save(ROOT / ('capacity-gate-' + spec['label'] + '.json'), gate)
                    if len(profiles) != 4 or required > 96 * (1 << 30):
                        update('capacity-calibration-blocked', active=None, capacity_gate=gate,
                               completed=completed, failed=failed)
                        return
                update('preparing-cohort', active=spec['label'], completed=completed, failed=failed)
                result = run(spec, existing_outer=True)
                save(ROOT / 'last-audit.json', result)
                if not result['valid']:
                    failed.append(dict(label=spec['label'], classification=result['classification'],
                                       l1_oom=result['l1_oom']))
                    save(ROOT / 'failure-alert.json', dict(epoch=time.time(), label=spec['label'], audit=result))
                    update('failure-needs-immediate-diagnosis', active=None, failed=failed, completed=completed,
                           diagnostic_host=HOST)
                    return  # Retain private L1 and exact evidence for the active agent.
                completed.append(spec['label'])
                if spec['category'] == 'S1' and spec['repetition'] == 1:
                    name = spec['slots'][0]['case']
                    profiles[name] = profile_bound(spec['label'])
                    save(ROOT / 'single-instance-profiles.json', profiles)
                update('cohort-passed', active=None, completed=completed, failed=failed)
            update('baseline-complete-ready-for-density-sweep', active=None, completed=completed, failed=failed)
            stop(HOST)
        except BaseException as error:
            save(ROOT / 'infrastructure-alert.json', dict(epoch=time.time(), error=repr(error)))
            update('infrastructure-needs-immediate-diagnosis', active=None, error=repr(error),
                   completed=completed, failed=failed, diagnostic_host=HOST)
            raise


if __name__ == '__main__':
    main()
