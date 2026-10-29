#!/usr/bin/env python3
"""Three physical modes; quota sweeps and causal ablations belong to ours."""
import argparse
import hashlib
import json
import os
import random
import shutil
import subprocess
import time
from pathlib import Path

W = Path(__file__).resolve().parents[2]
H = Path(__file__).resolve().parent
A = W / 'phase/artifacts/vmm-v13-validated'
TARGET = '6.10.0-demeterhost-loadfb-20261005'

def sha(p):
    h = hashlib.sha256()
    with p.open('rb') as f:
        for b in iter(lambda: f.read(8 << 20), b''):
            h.update(b)
    return h.hexdigest()

CONFIGS = {
    'dram': dict(mode='dram', policy='dram', zones=[0, 0], private=0, bind_node=0),
    'cxl': dict(mode='cxl', policy='dram', zones=[2, 2], private=2, bind_node=0),
}
for q in [256, 512, 1024, 2048, 4096]:
    for variant in ['full', 'heat', 'alloc', 'reactive', 'legacy']:
        CONFIGS[f'{variant}{q}'] = dict(mode='ours', policy='phase', zones=[2, 0], private=2,
            quota=q, startup=0 if variant == 'legacy' else q,
            allocation=1, sampling=int(variant != 'alloc'), placement=int(variant != 'alloc'),
            benefit=int(variant != 'heat'), events=int(variant != 'reactive'), fast_node=0, slow_node=1)

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--traces', nargs='+', default=['numba-9636', 'dask-5510', 'pydicom-2065'])
    p.add_argument('--configs', nargs='+', default=['dram', 'cxl', 'full4096'])
    p.add_argument('--repeats', type=int, default=1)
    p.add_argument('--speed', type=float, default=10)
    p.add_argument('--timing', choices=['original','gaps'], default='original')
    p.add_argument('--plan-only', action='store_true')
    a = p.parse_args()
    assert a.repeats > 0 and a.speed > 0 and set(a.configs) <= CONFIGS.keys()
    assert os.uname().release == TARGET, 'wrong Host kernel'
    cases = json.loads((W / 'results/capacity-v2-20261009/catalog.json').read_text())['primary']
    cases = [c for c in cases if c['name'] in a.traces]
    assert set(a.traces) == {c['name'] for c in cases}
    for c in cases:
        assert sha(Path(c['golden'])) == c['golden_sha256'], 'workload image drift'
        for n, expected in c['input_hashes'].items():
            assert sha(Path(c['trace']) / n) == expected
    jobs = []
    rng = random.Random(20261011)
    for repeat in range(1, a.repeats + 1):
        order = cases[:]
        rng.shuffle(order)
        for c in order:
            configs = a.configs[:]
            shift = (repeat - 1) % len(configs)
            configs = configs[shift:] + configs[:shift]
            for cfg in configs:
                jobs.append(dict(trace=c['name'], strategy=cfg, config=CONFIGS[cfg], repeat=repeat,
                    speed=a.speed, name=f"{c['name']}-{cfg}-r{repeat}"))
    files = [H / n for n in ['run_validated_matrix.py', 'run_validated_single.sh', 'host_monitor.py', 'relocate_private.py', 'verify_topology.py']]
    files += [W / 'phase/baselines' / n for n in ['guest_state.py', 'startup_monitor.py']]
    artifact_files = {x.name: sha(x) for x in A.iterdir() if x.is_file() and x.name != 'SHA256SUMS.json'}
    protocol = dict(version=13, replay_timing=a.timing, host_kernel=TARGET, cases=cases, jobs=jobs, artifact=str(A),
        files={str(x): sha(x) for x in files}, artifact_files=artifact_files,
        serial=True, host_cpus=[0, 1, 2, 3], guest_cpu_memory_node=0, guest_cpu_acpi_proximity=2, guest_fast_node=0, guest_slow_node=1,
        raw_zones_mib=[8192, 8192], usable_zones_mib=[6144, 6144],
        free_page_reporting=True, reporting_order=9, speed=a.speed,
        scope='whole VMM RSS peak and mean plus tool service; every Guest RAM zone audited; no density claim',
        latency_screen_target=.05,
        mode_semantics={'dram':'all Guest RAM DDR0, VMM allocation policy DDR0, no placement',
            'cxl':'all Guest RAM CXL2, VMM allocation policy CXL2, no placement; shared Host executable pages reported separately',
            'ours':'PXM1 boot zone maps Linux Guest1/CXL2; PXM2 CPU/tool arena maps Linux Guest0/DDR0; VMM private CXL2; phase quota and future-benefit placement'},
        timing='gaps: preserve recorded previous replayed finish to next start, divided by speed; original: absolute start offsets/speed; execution unscaled; serial tools',
        compatibility='same immutable workload images; Xonsh pathlib collector shim and Dask legacy warning-recording shim applied to every mode; assertions and recorded commands unchanged; other original failures retained')
    R = a.root.resolve()
    R.mkdir(parents=True, exist_ok=True)
    saved = R / 'plan.json'
    if saved.exists():
        assert json.loads(saved.read_text()) == protocol, 'frozen protocol drift'
    else:
        saved.write_text(json.dumps(protocol, indent=2) + '\n')
        archive = R / 'protocol'
        archive.mkdir()
        for x in files:
            shutil.copy2(x, archive / x.name)
    print('PLANNED', len(jobs), R, flush=True)
    if a.plan_only:
        return
    for job in jobs:
        assert os.uname().release == TARGET, 'Host rebooted to a different kernel'
        receipt = R / (job['name'] + '.receipt.json')
        if receipt.exists():
            assert json.loads(receipt.read_text())['exit_code'] == 0, 'retained failed run'
            continue
        assert not (R / job['name']).exists(), 'retained incomplete run'
        for name, expected in protocol['files'].items():
            assert sha(Path(name)) == expected, name
        for name, expected in artifact_files.items():
            assert sha(A / name) == expected, name
        c = next(c for c in cases if c['name'] == job['trace'])
        cfg = job['config']
        phase = int(cfg['mode'] == 'ours')
        env = {**os.environ, 'VM_LANE': '0', 'ROOT_DISK': str(W / 'validated-root-disks/root-demeter.qcow2'),
            'PHASE_ARTIFACT_DIR': str(A), 'FREE_PAGE_REPORTING': 'on', 'PAGE_REPORTING_ORDER': '9',
            'FAST_RAW_MIB': '8192', 'SLOW_RAW_MIB': '8192', 'CAPACITY_POLICY': 'static',
            'REPLAY_TIMEOUT_S': '1800', 'REPLAY_SEARCH_BACKEND': 'ripgrep', 'PHASE_COUNTERS': '0',
            'REPLAY_TIMING': a.timing, 'BIND_TOOL_NODE': '-1', 'GUEST_SYSTEM_TIER': 'slow',
            'HOST_FAST_NUMA_NODE': str(cfg['zones'][0]), 'HOST_SLOW_NUMA_NODE': str(cfg['zones'][1]),
            'VMM_PRIVATE_NODE': str(cfg['private']), 'BASELINE_POLICY': cfg['policy'],
            'PHASE_BUDGET_MIB': str(cfg.get('quota', 256)), 'STARTUP_HEADROOM_MIB': str(cfg.get('startup', 0)),
            'SB_FAST_NODE': '0', 'SB_SLOW_NODE': '1', 'SB_FUTURE_BENEFIT': str(cfg.get('benefit', 1)),
            'PHASE_NO_EVENTS': str(1-cfg.get('events', 1)),
            'PYTEST_COMPAT': str(int(c['name'].startswith('xonsh') or c['name']=='dask-5510')), 'SB_SAMPLE_WAKE_US': '2000',
            'SB_ALLOCATION': str(cfg.get('allocation', phase)), 'SB_SAMPLING': str(cfg.get('sampling', phase)),
            'SB_PLACEMENT': str(cfg.get('placement', phase)), 'SB_SAMPLE_BATCH': str(phase),
            'SB_SAMPLE_WAKEUP': str(cfg.get('sampling', phase)), 'SB_LIFECYCLE_WAKEUP': str(cfg.get('sampling', phase))}
        cmd = ['bash', str(H / 'run_validated_single.sh'), 'phase', c['golden'], c['trace'], str(R / job['name']),
            '6144', '6144', '0', '0', str(a.speed)]
        print('START', job['name'], flush=True)
        start = time.time()
        with (R / (job['name'] + '.launch.log')).open('x') as log:
            run = subprocess.run(cmd, cwd=W, env=env, stdout=log, stderr=subprocess.STDOUT)
        receipt.write_text(json.dumps(dict(exit_code=run.returncode, job=job,
            started_epoch=start, finished_epoch=time.time(), wall_s=time.time()-start), indent=2) + '\n')
        print('END', job['name'], run.returncode, round(time.time()-start, 1), flush=True)
        if run.returncode:
            raise SystemExit(run.returncode)
    (R / 'finished.json').write_text(json.dumps(dict(planned=len(jobs), epoch=time.time())) + '\n')

if __name__ == '__main__':
    main()
