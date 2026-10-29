#!/usr/bin/env python3
"""Serial physical experiments with switchable CXL/SSD and matched DRAM controls."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

H = Path(__file__).resolve().parent
W = H.parents[1]
A = W / 'phase/artifacts/vmm-v25-cold-media-e2e'
TARGET = '6.10.0-demeterhost-loadfb-20261005'


def sha(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(8 << 20), b''):
            result.update(block)
    return result.hexdigest()


def config(name):
    media, amount = name.split('-', 1)
    if media not in ['cxl', 'ssd']:
        raise ValueError('unknown medium')
    if amount == 'dram':
        return dict(media=media, policy='dram', quota=6144, zones=[0, 0], private=0,
                    baseline=True, phase=False)
    if amount == 'all' and media == 'cxl':
        return dict(media=media, policy='dram', quota=0, zones=[2, 2], private=2,
                    baseline=False, phase=False)
    quota = int(amount)
    if not 16 <= quota <= 6144:
        raise ValueError('quota outside 16..6144 MiB')
    return dict(media=media, policy='phase', quota=quota,
                zones=[2, 0] if media == 'cxl' else [0, 0],
                private=2 if media == 'cxl' else 0, baseline=False, phase=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--traces', nargs='+', default=['dask-5510', 'numba-9636', 'pydicom-2065'])
    p.add_argument('--configs', nargs='+', default=['cxl-dram', 'ssd-dram', 'cxl-4096',
        'ssd-4096', 'cxl-2048', 'ssd-2048', 'cxl-1024', 'ssd-1024',
        'cxl-512', 'ssd-512', 'cxl-all'])
    p.add_argument('--repeats', type=int, default=3)
    p.add_argument('--lane', type=int, default=1)
    p.add_argument('--speed', type=float, default=1)
    p.add_argument('--catalog', type=Path, default=W / 'results/capacity-v2-20261009/catalog.json')
    p.add_argument('--plan-only', action='store_true')
    a = p.parse_args()
    if not (a.repeats > 0 and a.speed > 0 and 0 <= a.lane <= 7):
        p.error('invalid repeat, speed or lane')
    if os.uname().release != TARGET:
        p.error('wrong Host kernel')
    root = a.root.resolve()
    if not re.fullmatch(r'[A-Za-z0-9_./-]+', str(root)):
        p.error('runner paths require simple shell-safe characters')
    cases = json.loads(a.catalog.read_text())['primary']
    cases = [c for c in cases if c['name'] in a.traces]
    if set(a.traces) != {c['name'] for c in cases}:
        p.error('trace absent from catalog')
    configs = {name: config(name) for name in a.configs}
    for c in cases:
        if sha(c['golden']) != c['golden_sha256']:
            raise RuntimeError('workload image drift: ' + c['name'])
        for name, expected in c['input_hashes'].items():
            if sha(Path(c['trace']) / name) != expected:
                raise RuntimeError('trace input drift: ' + name)
    jobs = []
    for repeat in range(1, a.repeats + 1):
        for c in cases:
            order = a.configs[(repeat - 1) % len(a.configs):] + a.configs[:(repeat - 1) % len(a.configs)]
            for name in order:
                jobs.append(dict(name=f"{c['name']}-{name}-r{repeat}", trace=c['name'],
                                 strategy=name, config=configs[name], repeat=repeat))
    files = [x for x in H.iterdir() if x.suffix in ['.py', '.sh']]
    files += [W / 'phase/three_modes' / name for name in ['verify_topology.py', 'relocate_private.py']]
    files += [W / 'phase/baselines/startup_monitor.py', W / 'randomize_cgroup_once.sh',
              W.parents[1] / 'sosp25-demeter/hypervisor/target/host-returns-v19/cloud-hypervisor',
              W / 'validated-root-disks/root-demeter.qcow2']
    hashes = {str(x): sha(x) for x in files}
    artifacts = {str(x): sha(x) for x in A.iterdir() if x.is_file()}
    protocol = dict(version=25, host_kernel=TARGET, cases=cases, jobs=jobs,
        files=hashes, artifact_files=artifacts, artifact=str(A), lane=a.lane,
        speed=a.speed, replay_timing='gaps', serial=True,
        host_cpus=list(range(4 * a.lane, 4 * a.lane + 4)),
        raw_zones_mib=[8192, 8192], usable_zones_mib=[6144, 6144], reporting_order=5,
        constraint_metric='e2e_s', limits=[.05, .10, .15, .20],
        selection='maximum observed whole-VMM DRAM mean saving among measured configurations satisfying paired E2E bounds; baseline is always a zero-saving fallback',
        scope='Guest-delegated trace-replay E2E; no LLM rerun, full user request/response boundary, shared Host budget, density or p99 guarantee',
        quota_semantics={'cxl':'controlled anonymous placement target; boot/private CXL',
                         'ssd':'cgroup total resident memory.high, WAIT proactive reclaim; boot/private DRAM; demand swap-in'},
        host_numa_balancing=int(Path('/proc/sys/kernel/numa_balancing').read_text()))
    root.mkdir(parents=True, exist_ok=True)
    # Lock protects this plan's receipts. The runner separately owns the VM lane.
    with (root / 'study.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        saved = root / 'plan.json'
        if saved.exists():
            if json.loads(saved.read_text()) != protocol:
                raise RuntimeError('frozen protocol drift; use a new result directory')
        else:
            saved.write_text(json.dumps(protocol, indent=2) + '\n')
            archive = root / 'protocol'
            archive.mkdir()
            for path in files:
                # Relative archive names keep duplicate basenames distinct.
                target = archive / path.relative_to(W.parents[1])
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
        print('PLANNED', len(jobs), root, flush=True)
        if a.plan_only:
            return
        for job in jobs:
            if (root / 'stop-after-current').exists():
                (root / 'stopped.json').write_text(json.dumps(dict(epoch=time.time(), reason='stop-after-current')) + '\n')
                return
            receipt = root / (job['name'] + '.receipt.json')
            if receipt.exists():
                if json.loads(receipt.read_text())['exit_code'] != 0:
                    raise RuntimeError('retained failed receipt; diagnose rather than overwrite')
                continue
            if (root / job['name']).exists():
                raise RuntimeError('retained incomplete trial; use a fresh trial root')
            for name, expected in {**hashes, **artifacts}.items():
                if sha(name) != expected:
                    raise RuntimeError('frozen input drift: ' + name)
            c = next(c for c in cases if c['name'] == job['trace'])
            cfg = job['config']
            env = dict(os.environ, VM_LANE=str(a.lane),
                PHASE_ARTIFACT_DIR=str(A), ROOT_DISK=str(W / 'validated-root-disks/root-demeter.qcow2'),
                COLD_MEDIA=cfg['media'], BASELINE_POLICY=cfg['policy'],
                HOST_FAST_NUMA_NODE=str(cfg['zones'][0]), HOST_SLOW_NUMA_NODE=str(cfg['zones'][1]),
                VMM_PRIVATE_NODE=str(cfg['private']), GUEST_SYSTEM_TIER='slow',
                FAST_RAW_MIB='8192', SLOW_RAW_MIB='8192', FREE_PAGE_REPORTING='on',
                PAGE_REPORTING_ORDER='5', WAIT_REPORTING_ORDER='-1',
                CAPACITY_POLICY='static', DEMETER_REPORTING_RECEIPTS='1',
                PHASE_BUDGET_MIB=str(cfg['quota'] or 256), STARTUP_HEADROOM_MIB=str(cfg['quota'] or 0),
                SB_FAST_NODE='0', SB_SLOW_NODE='1', SB_ALLOCATION='1', SB_PLACEMENT='1',
                SB_SAMPLING='0', SB_SAMPLE_WAKEUP='0', SB_LIFECYCLE_WAKEUP='0',
                SB_SAMPLE_BATCH='1', SB_RECYCLE_CREDIT='1', SB_FUTURE_BENEFIT='1',
                PHASE_NO_EVENTS='0', REPLAY_TIMING='gaps', REPLAY_TIMEOUT_S='2400',
                REPLAY_SEARCH_BACKEND='ripgrep', PHASE_COUNTERS='0', BIND_TOOL_NODE='-1',
                PYTEST_COMPAT=str(int(c['name'].startswith('dask'))))
            cmd = ['bash', str(H / 'run_single.sh'), 'phase', c['golden'], c['trace'],
                   str(root / job['name']), '6144', '6144', '0', '0', str(a.speed)]
            print('START', job['name'], flush=True)
            started = time.time()
            with (root / (job['name'] + '.launch.log')).open('x') as log:
                run = subprocess.run(cmd, cwd=W, env=env, stdout=log, stderr=subprocess.STDOUT)
            receipt.write_text(json.dumps(dict(job=job, exit_code=run.returncode,
                started_epoch=started, finished_epoch=time.time(), wall_s=time.time() - started), indent=2) + '\n')
            print('END', job['name'], run.returncode, round(time.time() - started, 1), flush=True)
            if run.returncode:
                raise SystemExit(run.returncode)
        (root / 'finished.json').write_text(json.dumps(dict(epoch=time.time(), planned=len(jobs))) + '\n')


if __name__ == '__main__':
    main()
