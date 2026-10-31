"""Execute one immutable formal cohort entirely within the private L1 VM."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from environment import environment, W
from control_timeout import expired

ROOT = W / 'results/PhaseTieing-concurrent-20261031-v16-iowq-cap'
H = Path(__file__).resolve().parent
GROUP = Path('/sys/fs/cgroup/PhaseTieing-formal')


def save(path, value):
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def memory(path):
    result = {}
    for line in path.read_text().splitlines():
        fields = line.split()
        if len(fields) >= 5 and fields[0] == 'Node':
            result[fields[2].rstrip(':')] = int(fields[3]) * 1024
        elif len(fields) >= 2:
            result[fields[0].rstrip(':')] = int(fields[1]) * 1024
    return result


def snapshot():
    domains = {}
    for group in [GROUP, *sorted(GROUP.glob('slot-*'))]:
        if not group.exists():
            continue
        row = {name: (group / name).read_text().strip() for name in [
            'memory.current', 'memory.peak', 'memory.events', 'memory.stat',
            'memory.numa_stat', 'memory.swap.current', 'cpu.stat',
            'memory.pressure', 'cpu.pressure', 'io.pressure'] if (group / name).exists()}
        row['vmms'] = []
        for member in (group / 'cgroup.procs').read_text().split():
            proc = Path('/proc') / member
            try:
                if (proc / 'comm').read_text().startswith('cloud-hypervis'):
                    row['vmms'].append(dict(pid=int(member),
                        status=(proc / 'status').read_text()))
            except (FileNotFoundError, ProcessLookupError):
                continue
        domains[group.name] = row
    return dict(epoch=time.time(), monotonic_ns=time.monotonic_ns(),
        cohort_observer_cpu_ns=time.process_time_ns(),
        memory=memory(Path('/proc/meminfo')),
        nodes={p.parent.name: memory(p) for p in
            Path('/sys/devices/system/node').glob('node[0-9]*/meminfo')},
        psi={name: Path('/proc/pressure', name).read_text().strip()
             for name in ['memory', 'cpu', 'io']}, domains=domains,
        diskstats=Path('/proc/diskstats').read_text(),
        oom_kill=int(dict(line.split() for line in Path('/proc/vmstat').read_text().splitlines()).get('oom_kill', 0)))


def slot_environment(case, spec, lane, settings, barrier):
    phase = spec['strategy'] in ['P', 'F']
    medium = spec['medium'] if phase or spec['strategy'] == 'L' else 'cxl'
    env = environment(case, medium, lane, spec['n'], settings, barrier)
    env['ATTACH_COLD_DISK'] = '1' if medium == 'ssd' else '0'
    per_slot, remainder = divmod(64 * 1024, spec['n'])
    env['SSD_SWAP_MIB'] = str(per_slot + int(lane < remainder))
    env['REPLAY_TIMEOUT_S'] = '0'
    if not phase:
        env.update(BASELINE_POLICY='cxl' if spec['strategy'] == 'L' else 'dram',
            HOST_SLOW_NUMA_NODE='1' if spec['strategy'] == 'L' else '0',
            WAIT_REPORTING_ORDER='-1', BARRIER_REPORTING_START='0',
            ADAPTIVE_MODE='0', ATTACH_COLD_DISK='0')
    if spec.get('setup_only'):
        env['SETUP_ONLY'] = '1'
    return env


def last_sample(path):
    if not path.exists():
        return None
    with path.open('rb') as stream:
        stream.seek(max(0, path.stat().st_size - 262144))
        rows = stream.read().splitlines()
    for line in reversed(rows):
        try:
            row = json.loads(line)
            if row.get('event') == 'sample' and row.get('guest_fresh'):
                return row
        except ValueError:
            continue
    return None


def main():
    os.sched_setaffinity(0, {32, 33, 34, 35})
    spec = json.loads(Path(sys.argv[1]).read_text())
    folder = ROOT / 'cohorts' / spec['label']
    folder.mkdir(parents=True, exist_ok=False)
    barrier = folder / 'barrier'
    barrier.mkdir()
    cases = {c['name']: c for c in json.loads((ROOT / 'inputs/cases.json').read_text())}
    GROUP.mkdir(exist_ok=False)
    available = set((GROUP / 'cgroup.controllers').read_text().split())
    if not {'memory', 'cpu'} <= available:
        raise RuntimeError('missing L1 resource controllers')
    (GROUP / 'cgroup.subtree_control').write_text(' '.join('+' + name
        for name in ['memory', 'cpu', 'io'] if name in available))
    (GROUP / 'memory.swap.max').write_text('0')
    jobs, processes = [], []
    state = dict(**spec, started_epoch=time.time(), released=False,
                 result='preparing', release_events=[])
    initial_oom_kill = snapshot()['oom_kill']
    from global_pool import GlobalPool
    global_pool = GlobalPool(folder, spec) if spec['strategy'] in ['P', 'F'] else None
    save(folder / 'status.json', state)
    out = (folder / 'l1-resources.jsonl').open('x', buffering=1)
    last_observe = 0
    aborted = False
    probe_process = None
    probe_log = None
    timeout_policy = spec.get('control_timeout')
    if timeout_policy and spec['strategy'] != 'F':
        raise ValueError('control timeout attached to non-control cohort')
    try:
        probe_log=(folder/'sigsys-probe.stderr').open('w')
        probe_process=subprocess.Popen([sys.executable,str(H/'kernel_sigsys_probe.py'),
            '--out',str(folder/'sigsys-events.jsonl'),'--ready',str(folder/'sigsys-probe-ready.json'),
            '--stop',str(folder/'sigsys-probe-stop')],stdout=probe_log,stderr=probe_log)
        until=time.monotonic()+5
        while not (folder/'sigsys-probe-ready.json').exists():
            if probe_process.poll() is not None or time.monotonic()>until:
                raise RuntimeError('SIGSYS probe failed to initialize')
            time.sleep(.05)
        for slot in spec['slots']:
            lane = slot['lane']
            case = cases[slot['case']]
            group = GROUP / f'slot-{lane}'
            group.mkdir()
            (group / 'memory.swap.max').write_text('0')
            quota = spec.get('quota_mib') if spec['strategy'] == 'F' else None
            settings = folder / f'slot-{lane}-settings.json'
            save(settings, dict(capacity=dict(active_mib=quota if quota is not None else 6144,
                wait_mib=quota if quota is not None else 16), placement=dict(
                mode='cold-cache' if spec['medium'] == 'ssd' else 'native',
                fast_wait_cache=spec['medium'] == 'ssd')))
            target = folder / f'slot-{lane}'
            cmd = ['bash', str(H / 'run_single.sh'), 'phase', case['golden'],
                   case['trace'], str(target), '6144', '6144', '0', '0', '1']
            jobs.append(dict(lane=lane, case=case, target=str(target), command=cmd,
                strategy=spec['strategy'], settings=str(settings), deadline_s=slot['deadline_s']))
            log = (folder / f'slot-{lane}.log').open('x')
            def join(path=group):
                (path / 'cgroup.procs').write_text(str(os.getpid()))
            process = subprocess.Popen(cmd, env=slot_environment(case, spec, lane,
                settings, barrier), stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True, preexec_fn=join)
            processes.append((process, log))
        save(folder / 'jobs.json', jobs)
        ready_deadline = time.monotonic() + 1800
        ready_at = None
        ready_samples = []
        while any(p.poll() is None for p, _ in processes):
            now = time.monotonic()
            if probe_process.poll() is not None:
                state.update(result='sigsys-probe-exited',first_failure_epoch=time.time())
                aborted=True
            if now - last_observe >= .1:
                point = snapshot()
                out.write(json.dumps(point) + '\n')
                last_observe = now
                parent = point['domains'].get(GROUP.name, {})
                if global_pool is not None:
                    global_pool.tick(point, [last_sample(folder / f'slot-{i}/host-capacity.jsonl')
                                             for i in range(spec['n'])],
                                     allow_initial_commit=ready_at is not None and now-ready_at >= 2)
                if spec.get('require_controller_health'):
                    for lane in range(spec['n']):
                        sample = last_sample(folder / f'slot-{lane}/host-capacity.jsonl')
                        if not sample or not sample.get('guest_fresh'):continue
                        cap = sample['guest'].get('capacity_state') or {}
                        if cap.get('controller_alive') is False or cap.get('controller_age_s',0)>30:
                            state.update(result='controller-exited' if cap.get('controller_alive') is False else 'controller-heartbeat-stalled',
                                failure_lane=lane,first_failure_epoch=time.time(),controller_health=cap)
                            aborted=True
                events = dict(line.split() for line in parent.get('memory.events', '').splitlines())
                if point['oom_kill'] > initial_oom_kill or any(int(events.get(k, 0)) for k in ['oom', 'oom_kill']):
                    state.update(result='l1-oom', first_failure_epoch=time.time(), failure='L1 global/cgroup OOM')
                    aborted = True
                if state['released'] and 'all_done_epoch' not in state:
                    live = sum(len(v.get('vmms', [])) for k, v in point['domains'].items() if k.startswith('slot-'))
                    if live != spec['n']:
                        if not aborted:
                            state.update(result='vmm-loss', first_failure_epoch=time.time())
                        state['live_vmms']=live
                        aborted = True
                    for lane in range(spec['n']):
                        sample = last_sample(folder / f'slot-{lane}/host-capacity.jsonl')
                        if sample:
                            guest_events = sample['guest'].get('memory_events', {})
                            if any(guest_events.get(k, 0) for k in ['oom', 'oom_kill']):
                                state.update(result='l2-oom', first_failure_epoch=time.time(), failure_lane=lane)
                                aborted = True
                        log_path = folder / f'slot-{lane}/replay.log'
                        if log_path.exists():
                            with log_path.open('rb') as progress:
                                progress.seek(max(0, log_path.stat().st_size - 65536))
                                lines = progress.read().splitlines()
                            for line in lines:
                                if line.startswith(b'PHASETIEING_STEP='):
                                    try:
                                        event = json.loads(line.split(b'=', 1)[1])
                                    except ValueError:
                                        continue
                                    if not aborted and event.get('expected_error') is False and event.get('outcome_match') is False:
                                        state.update(result='unexpected-tool-outcome', first_failure_epoch=time.time(),
                                            failure_lane=lane, failure_event=event)
                                        aborted = True
            codes = [p.poll() for p, _ in processes]
            if (os.statvfs(ROOT).f_bavail * os.statvfs(ROOT).f_frsize) < 8*(1<<30):
                state.update(result='censored-resource-protection', censor_reason='private data filesystem below 8 GiB free')
                aborted = True
            if any(code not in [None, 0] for code in codes):
                if not aborted:
                    state['result'] = 'runner-failed'
                aborted = True
            all_ready = all((barrier / f'lane{i}.ready').exists() for i in range(spec['n']))
            if spec.get('setup_only'):
                if now > ready_deadline:
                    state['result'] = 'setup-censored-infrastructure'
                    aborted = True
            elif not state['released'] and all_ready:
                if ready_at is None:
                    ready_at = now
                if len(ready_samples) < 10:
                    ready_samples.append(dict(l1=snapshot(), slots=[last_sample(
                        folder / f'slot-{i}/host-capacity.jsonl') for i in range(spec['n'])]))
                    save(folder / 'ready-observations.json', ready_samples)
                if now - ready_at >= 2:
                    state['ready_barrier_complete'] = True
                    if global_pool is not None and global_pool.budget is None:
                        state['result'] = 'waiting-initial-physical-budget'
                        if now > ready_deadline:
                            state['result'] = 'startup-resource-admission-blocked'
                            aborted = True
                    elif spec.get('ready_only'):
                        state['result'] = 'ready-calibration-completed'
                        aborted = True
                    else:
                        state.update(released=True, release_epoch=time.time(),
                            release_monotonic_ns=time.monotonic_ns(), result='running')
                        # Only F has the paired cohort deadline; tools and P remain unlimited.
                        print('RELEASED', spec['label'], spec['n'], flush=True)
            elif not state['released'] and not spec.get('setup_only'):
                if any(code is not None for code in codes) or now > ready_deadline:
                    state['result'] = 'incomplete-ready-cohort'
                    aborted = True
            if state['released']:
                elapsed = (time.monotonic_ns() - state['release_monotonic_ns']) / 1e9
                for slot in spec['slots']:
                    go = barrier / f"lane{slot['lane']}.go"
                    if elapsed >= slot['offset_s'] and not go.exists():
                        go.touch()
                        state['release_events'].append(dict(lane=slot['lane'],
                            actual_offset_s=elapsed, planned_offset_s=slot['offset_s']))
                if all((barrier / f'lane{i}.done').exists() for i in range(spec['n'])):
                    if 'all_done_epoch' not in state:
                        state.update(all_done_epoch=time.time(), all_done_monotonic_ns=time.monotonic_ns(), result='exporting')
                        (barrier / 'export.go').touch()
                if not aborted and expired(timeout_policy,elapsed,all_done='all_done_epoch' in state):
                    cutoff=snapshot()
                    health={str(lane):(last_sample(folder / f'slot-{lane}/host-capacity.jsonl') or {})
                            for lane in range(spec['n'])}
                    decision=dict(timeout_policy,stop_epoch=time.time(),observed_duration_s=elapsed,
                        stop_monotonic_ns=time.monotonic_ns(),adopted_after_start=False,
                        completed_lanes=[lane for lane in range(spec['n'])
                            if (barrier / f'lane{lane}.done').exists()])
                    # Kernel and resource evidence is taken at cutoff, before cleanup.
                    with (folder/'cutoff-l1-dmesg.log').open('w') as dst:
                        subprocess.run(['dmesg'],stdout=dst,check=False)
                    save(folder/'control-timeout.json',decision)
                    save(folder/'cutoff-controller-health.json',health)
                    save(folder/'cutoff-resources.json',cutoff)
                    state.update(result='control-relative-timeout',control_timeout=decision,
                        observed_duration_s=elapsed,cutoff_resources=cutoff)
                    aborted=True
            save(folder / 'status.json', state)
            if aborted:
                # Capture before signalling runners, so Guests remain available.
                break
            time.sleep(.05)
        if not aborted:
            state['result'] = 'setup-completed' if spec.get('setup_only') else (
                'completed' if state['released'] and all(p.returncode == 0
                    for p, _ in processes) else 'failed')
    except BaseException as error:
        state.update(result='driver-error', error=repr(error))
        raise
    finally:
        state['final_resources'] = snapshot()
        if aborted:
            from concurrent.futures import ThreadPoolExecutor
            from timeout_evidence import capture
            key = W.parents[1] / 'experiments/physical-smoke/id_ed25519'
            with ThreadPoolExecutor(max_workers=2) as pool:
                receipts=list(pool.map(lambda lane:capture(folder,lane,key), (lane for lane in range(spec['n']) if (folder/f'slot-{lane}').is_dir())))
            save(folder/'timeout-evidence-receipts.json',receipts)
        (barrier / 'abort').touch()
        if not aborted:
            (barrier / 'export.go').touch()
        # Normal runners finish their own exact-VM cleanup before group cleanup.
        if aborted and state['result'] != 'control-relative-timeout':
            until = time.monotonic() + 30
            while any(p.poll() is None for p, _ in processes) and time.monotonic() < until:
                time.sleep(.25)
        for group in GROUP.glob('slot-*'):
            if (group / 'cgroup.events').exists() and 'populated 1' in (group / 'cgroup.events').read_text():
                (group / 'cgroup.kill').write_text('1')
        for process, log in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            log.close()
        out.close()
        if global_pool is not None:
            global_pool.close()
        if probe_process is not None:
            (folder/'sigsys-probe-stop').touch()
            try:probe_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                probe_process.terminate();probe_process.wait(timeout=5)
            state['sigsys_probe_returncode']=probe_process.returncode
        if probe_log is not None:probe_log.close()
        state.update(finished_epoch=time.time(), returncodes=[p.returncode for p, _ in processes])
        save(folder / 'status.json', state)
        with (folder / 'l1-dmesg.log').open('w') as stream:
            subprocess.run(['dmesg'], stdout=stream, check=False)
        # A persistent private L1 may execute the next independent cohort.
        for group in GROUP.glob('slot-*'):
            try:
                group.rmdir()
            except OSError:
                pass
        try:
            GROUP.rmdir()
        except OSError:
            pass
        print('COHORT', spec['label'], state['result'], state['returncodes'], flush=True)


if __name__ == '__main__':
    main()
