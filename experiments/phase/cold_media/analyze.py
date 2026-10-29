#!/usr/bin/env python3
"""Select finite-grid DRAM savings under paired trace-replay E2E constraints."""
import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import random
import re
import statistics as st

MIB = 1 << 20


def read(path):
    return json.loads(path.read_text())


def signature(data):
    steps = [s for s in data['steps'] if not s.get('skipped')]
    return dict(statuses=data['status_vector'], head=data['original_git_head'],
        diff=data['git_diff_sha256'], git_status=data['git_status_sha256'],
        outputs={str(s['source_index']): s.get('stdout_sha256') for s in steps
                 if s['tool'] in ['Read', 'Grep', 'Glob']},
        tests={str(s['source_index']): re.findall(r'\b(\d+) (passed|failed|errors?|skipped|deselected|xfailed|xpassed)\b',
               (s.get('stdout_tail') or '') + '\n' + (s.get('stderr_tail') or ''))
               for s in steps if s['tool'] == 'Bash'})


def integrate(samples, start, end):
    """Zero-order hold; reject missing boundaries and stale/large sample gaps."""
    areas = dict(ddr=0., cxl=0.)
    coverage = maxgap = 0.
    selected = []
    for left, right in zip(samples, samples[1:]):
        lo = left['guest']['guest_monotonic_ns'] / 1e9
        hi = right['guest']['guest_monotonic_ns'] / 1e9
        dt = max(0., min(end, hi) - max(start, lo))
        if not dt:
            continue
        maxgap = max(maxgap, hi - lo)
        if hi <= lo or hi - lo > 1 or not left.get('guest_fresh') or not right.get('guest_fresh'):
            continue
        coverage += dt
        selected.append(left)
        for key, nodes in [('ddr', ['N0', 'N1']), ('cxl', ['N2'])]:
            areas[key] += sum(left['rss_node_bytes'].get(n, 0) for n in nodes) / MIB * dt
    peaks = {tier: max((sum(s['rss_node_bytes'].get(n, 0) for n in nodes) / MIB
                       for s in selected), default=0.)
             for tier, nodes in [('ddr', ['N0', 'N1']), ('cxl', ['N2'])]}
    return dict(covered_s=coverage, coverage=coverage / (end - start), maxgap_s=maxgap,
                **{tier + '_mean_mib': areas[tier] / coverage if coverage else None for tier in areas},
                **{tier + '_integral_gib_s': areas[tier] / 1024 for tier in areas},
                **{tier + '_peak_mib': peaks[tier] for tier in areas})


def nvme_ancestors(tree, target):
    """Inspect the device chain, rather than treating ROTA=0 as NVMe proof."""
    for dev in tree:
        if dev['name'] == target:
            return [dev['name']]
        chain = nvme_ancestors(dev.get('children', []), target)
        if chain:
            return [dev['name'], *chain]
    return []


def trial(root, plan, job):
    row = dict(**job, complete=False, valid=False, reasons=[])
    out = root / job['name']
    receipt = root / (job['name'] + '.receipt.json')
    if not receipt.exists():
        return row
    row['runner_exit'] = read(receipt)['exit_code']
    if row['runner_exit']:
        row['reasons'].append('runner failed')
    if not (out / 'guest-out/result.json').exists():
        return row
    data = read(out / 'guest-out/result.json')
    manifest = read(out / 'manifest.json')
    cfg = job['config']
    for path, expected in manifest['files'].items():
        archived = out / 'protocol-inputs' / Path(path).name
        source = archived if archived.exists() else Path(path)
        if not source.exists() or hashlib.sha256(source.read_bytes()).hexdigest() != expected:
            row['reasons'].append('input drift: ' + path)
    case = next(c for c in plan['cases'] if c['name'] == job['trace'])
    calls = read(Path(case['trace']) / 'tool_calls.json')
    if sorted(s['source_index'] for s in data['steps']) != list(range(len(calls))):
        row['reasons'].append('partial trace')
    if data['phase_runtime_errors'] or data['phase_aware'] != cfg['phase']:
        row['reasons'].append('Runtime control mismatch/error')
    if data['timing'] != 'gaps' or data['speed'] != plan['speed']:
        row['reasons'].append('replay protocol mismatch')
    if manifest.get('cold_media') != cfg['media']:
        row['reasons'].append('medium mismatch')
    start, end = data['e2e_started_monotonic_s'], data['e2e_finished_monotonic_s']
    if not 0 < end - start or not math.isclose(data['e2e_s'], end - start):
        row['reasons'].append('invalid E2E boundary')
        return row
    steps = [s for s in data['steps'] if not s.get('skipped')]
    row.update(complete=True, e2e_s=data['e2e_s'],
               service_s=sum(s['elapsed_s'] for s in steps),
               execution_s=sum(s['execution_s'] for s in steps),
               longest_service_s=max((s['elapsed_s'] for s in steps), default=0),
               signature=signature(data), executed_calls=len(steps),
               original_outcome_mismatches=data['outcome_mismatches'])
    if steps and (steps[0]['service_started_monotonic_s'] < start or
                  steps[-1]['service_finished_monotonic_s'] > end):
        row['reasons'].append('service outside E2E')
    row['nonservice_s'] = row['e2e_s'] - row['service_s']
    records = [json.loads(line) for line in (out / 'host-capacity.jsonl').read_text().splitlines()]
    init = next((r for r in records if r['event'] == 'init'), None)
    finish = next((r for r in reversed(records) if r['event'] == 'end'), None)
    if not init or not finish or finish.get('errors'):
        row['reasons'].append('Host observer incomplete/error')
    samples = [r for r in records if r['event'] == 'sample']
    if not samples:
        row['reasons'].append('no physical samples')
        return row
    row.update(integrate(samples, start, end))
    if row['coverage'] < .95 or row['maxgap_s'] > 1:
        row['reasons'].append('E2E memory sampling coverage/gap')
    if init:
        actual = init['info']['config']
        zones = {z['id']: z['host_numa_node'] for z in actual['memory']['zones']}
        if [zones[k] for k in ['dram', 'slow']] != cfg['zones']:
            row['reasons'].append('actual zone mismatch')
        affinity = actual['cpus']['affinity']
        if actual['cpus']['boot_vcpus'] != 4 or any(x['host_cpus'] != plan['host_cpus'] for x in affinity):
            row['reasons'].append('Host CPU affinity mismatch')
        if any(s['balloon_actual_bytes'] != init['nominal_balloon'] for s in samples):
            row['reasons'].append('Guest capacity changed')
        if cfg['media'] == 'ssd':
            disks = [d for d in actual['disks'] if d.get('id') == 'ssd-swap']
            if len(disks) != 1 or not disks[0]['direct']:
                row['reasons'].append('SSD direct I/O disk absent')
            source = read(out / 'ssd-filesystem.json')['filesystems'][0]['source']
            target = Path(source).name
            chain = nvme_ancestors(read(out / 'ssd-block-topology.json')['blockdevices'], target)
            row['ssd_device_chain'] = chain
            if not any(re.fullmatch(r'nvme\d+n\d+', n) for n in chain):
                row['reasons'].append('SSD backing not verified on NVMe')
    placement = bool(re.search(r'^demeter_placement\s', (out / 'policy-preflight.log').read_text(), re.M))
    if placement != (cfg['phase'] and cfg['media'] == 'cxl'):
        row['reasons'].append('placement module mismatch')
    kernel = (out / 'dmesg.log').read_text(errors='replace')
    if re.search(r'BUG:|Oops:|Kernel panic|general protection fault|Out of memory: Killed process|oom-kill:|Memory cgroup out of memory', kernel):
        row['reasons'].append('Guest fault/OOM')
    window = [s for s in samples if start - 1 <= s['guest']['guest_monotonic_ns'] / 1e9 <= end + 1]
    for sample in samples:
        if sample['guest']['page_reporting_order'] != 5:
            row['reasons'].append('reporting order drift')
        c = sample['vmm_balloon_counters']
        if (c['reporting_observed'] != 1 or
                not 0 <= c['reporting_returned_bytes'] <= c['reporting_before_bytes'] <= c['reporting_acked_bytes'] or
                c['reporting_after_bytes']):
            row['reasons'].append('physical reporting receipt invariant')
    if window:
        row['swap_peak_mib'] = max(s['guest']['swap_bytes'] for s in window) / MIB
        for name in ['pswpin', 'pswpout', 'pgmajfault']:
            row[name] = window[-1]['guest']['vmstat'].get(name, 0) - window[0]['guest']['vmstat'].get(name, 0)
        for s in window:
            events = s['guest']['memory_events']
            if events.get('oom', 0) or events.get('oom_kill', 0):
                row['reasons'].append('Sandbox OOM')
            if cfg['media'] == 'ssd':
                swaps = s['guest']['swaps'].splitlines()[1:]
                if len(swaps) != 1 or swaps[0].split()[0] != '/dev/vdc':
                    row['reasons'].append('nonexclusive SSD swap')
                if cfg['phase'] and s['guest'].get('memory_high') != str(cfg['quota'] * MIB):
                    row['reasons'].append('SSD memory.high mismatch')
        if cfg['baseline'] and row['pswpout']:
            row['reasons'].append('DRAM baseline swapped')
    controller = out / 'guest-out/controller.jsonl'
    if controller.exists():
        events = [json.loads(line) for line in controller.read_text().splitlines()]
        if any(e['event'] == 'error' for e in events):
            row['reasons'].append('controller error')
        row['reclaim_operations'] = sum(e['event'] == 'reclaim' for e in events)
        row['reclaim_eagain'] = sum(e['event'] == 'reclaim' and bool(e.get('error')) for e in events)
    row['reasons'] = sorted(set(row['reasons']))
    row['valid'] = not row['reasons']
    return row


def paired_stats(candidates, baselines):
    base = {r['repeat']: r for r in baselines}
    rows = []
    for r in candidates:
        b = base.get(r['repeat'])
        if not b or not b['valid'] or not r['valid'] or r['signature'] != b['signature']:
            return None
        rows.append(dict(repeat=r['repeat'], e2e_ratio=r['e2e_s'] / b['e2e_s'],
                         service_ratio=r['service_s'] / b['service_s'],
                         mean_ratio=r['ddr_mean_mib'] / b['ddr_mean_mib'],
                         integral_ratio=r['ddr_integral_gib_s'] / b['ddr_integral_gib_s'],
                         peak_ratio=r['ddr_peak_mib'] / b['ddr_peak_mib']))
    if not rows:
        return None
    return dict(pairs=rows, repeats=len(rows),
        e2e_increase_pct=100 * (st.median(r['e2e_ratio'] for r in rows) - 1),
        e2e_worst_pct=100 * (max(r['e2e_ratio'] for r in rows) - 1),
        service_increase_pct=100 * (st.median(r['service_ratio'] for r in rows) - 1),
        dram_mean_save_pct=100 * (1 - st.median(r['mean_ratio'] for r in rows)),
        dram_integral_save_pct=100 * (1 - st.median(r['integral_ratio'] for r in rows)),
        dram_peak_save_pct=100 * (1 - st.median(r['peak_ratio'] for r in rows)))


def select(candidates, limits, min_repeats, criterion='max'):
    output = []
    for limit in limits:
        feasible = [c for c in candidates if c['repeats'] >= min_repeats and
                    c['dram_mean_save_pct'] >= 0 and
                    c['e2e_worst_pct' if criterion == 'max' else 'e2e_increase_pct'] <= limit * 100 + 1e-8]
        winner = max(feasible, key=lambda c: c['dram_mean_save_pct']) if feasible else None
        output.append(dict(limit_pct=limit * 100, selected=winner,
                           status='empirically feasible on measured grid' if winner else 'insufficient valid repetitions'))
    return output


def bootstrap_saving(pairs):
    if len(pairs) < 3:
        return None
    rng = random.Random(20261014)
    values = sorted(100 * (1 - st.median(rng.choice(pairs)['mean_ratio'] for _ in pairs))
                    for _ in range(4000))
    return [values[int(.025 * len(values))], values[int(.975 * len(values))]]


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--min-repeats', type=int, default=3)
    p.add_argument('--criterion', choices=['max', 'median'], default='max')
    a = p.parse_args()
    if a.min_repeats < 1:
        p.error('min repetitions must be positive')
    root = a.root.resolve()
    plan = read(root / 'plan.json')
    rows = []
    for job in plan['jobs']:
        try:
            rows.append(trial(root, plan, job))
        except (KeyError, ValueError, OSError, TypeError) as exc:
            rows.append(dict(**job, complete=False, valid=False, reasons=['analysis failed: ' + repr(exc)]))
    configurations, constraints = [], []
    for trace in sorted({r['trace'] for r in rows}):
        for medium in ['cxl', 'ssd']:
            base = [r for r in rows if r['trace'] == trace and r['strategy'] == medium + '-dram']
            available = []
            for name in sorted({r['strategy'] for r in rows if r['trace'] == trace and r['config']['media'] == medium}):
                candidate_rows = [r for r in rows if r['trace'] == trace and r['strategy'] == name]
                # A partial cohort never gets selected by silently dropping a
                # failed/slow repetition. Every planned repetition is required.
                expected = {r['repeat'] for r in candidate_rows}
                if any(not r['complete'] or not r['valid'] for r in candidate_rows) or expected != {r['repeat'] for r in base}:
                    continue
                stats = paired_stats(candidate_rows, base)
                if stats is None:
                    continue
                c = dict(trace=trace, media=medium, strategy=name, **stats)
                c['dram_mean_save_ci95'] = bootstrap_saving(c['pairs'])
                available.append(c)
                configurations.append(c)
            constraints.extend(dict(trace=trace, media=medium, **s) for s in select(
                available, plan['limits'], a.min_repeats, a.criterion))
    full_wait = plan['speed'] == 1 and plan['replay_timing'] == 'gaps'
    result = dict(protocol=plan['version'], full_wait_e2e=full_wait,
                  criterion=a.criterion, min_repeats=a.min_repeats,
                  valid_runs=sum(r['valid'] for r in rows), planned_runs=len(rows),
                  rows=rows, configurations=configurations, constraints=constraints,
                  limitations=['Trace-replay E2E is not a live Agent request-to-final-response measurement',
                               'Finite-grid selection and repeat bounds do not enforce an online SLO or establish p99',
                               'Whole-VMM RSS excludes all Host KVM/global page-cache charges',
                               'CXL targets controlled anonymous pages; SSD memory.high targets total cgroup residency',
                               'Candidate selection and descriptive intervals use the same cohort; no held-out confirmation'])
    (root / 'analysis.json').write_text(json.dumps(result, indent=2) + '\n')
    fields = ['trace', 'media', 'limit_pct', 'strategy', 'dram_mean_save_pct',
              'e2e_increase_pct', 'e2e_worst_pct', 'service_increase_pct', 'repeats', 'status']
    with (root / 'constraints.csv').open('w') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for c in constraints:
            selected = c['selected'] or {}
            writer.writerow({k: c.get(k, selected.get(k)) for k in fields})
    lines = ['# 冷层介质与 E2E 约束实验', '',
             f"有效 VM：{result['valid_runs']}/{len(rows)}；完整等待：{full_wait}；重复数门槛：{a.min_repeats}。", '',
             'E2E 为回放窗口，包含真实工具服务与记录的等待；VM 启动、采样收尾、Git 导出不计入。', '',
             'DRAM 均值先对每个 VM 的 E2E 窗口进行时间积分，再按重复块与同介质全 DRAM 配对归一化。', '',
             '默认要求每个已测重复块的 E2E 增长都不超过限制；这是有限样本筛选，不是 p99 或在线保证。', '',
             '|轨迹|冷层|E2E 上限|选择配置|DRAM 均值节省|E2E 中位增长|最差重复增长|工具服务增长|',
             '|---|---|---:|---|---:|---:|---:|---:|']
    for c in constraints:
        s = c['selected']
        if s:
            lines.append(f"|{c['trace']}|{c['media']}|{c['limit_pct']:.0f}%|{s['strategy']}|{s['dram_mean_save_pct']:.2f}%|{s['e2e_increase_pct']:.2f}%|{s['e2e_worst_pct']:.2f}%|{s['service_increase_pct']:.2f}%|")
        else:
            lines.append(f"|{c['trace']}|{c['media']}|{c['limit_pct']:.0f}%|待测/有效重复不足|—|—|—|—|")
    lines += ['', '未完成、无效、工作量不一致的配置不会被悄悄丢弃失败重复后纳入选择。', '',
              '全 DRAM 是零节省兜底；四种约束选择同一配置是允许的。测量网格之外没有最优性结论。', '',
              '原始失败/OOM 及物理核验记录保存在 analysis.json。SSD 换出量不能直接计为 Host DRAM 节省。', '']
    if not full_wait:
        lines += ['本批等待经过压缩，仅用于机制筛查，不能作为完整等待的 E2E 约束结论。', '']
    (root / 'RESULTS.md').write_text('\n'.join(lines))
    print(json.dumps(dict(valid_runs=result['valid_runs'], planned_runs=len(rows), full_wait_e2e=full_wait)))
    if (root / 'finished.json').exists() and any(not r['valid'] for r in rows):
        raise SystemExit('completed cohort contains invalid runs; see analysis.json')


if __name__ == '__main__':
    main()
