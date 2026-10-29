#!/usr/bin/env python3
"""Observe whole-VMM residency and ACK capacity changes before reuse.

The controller never treats a reported free page as a reserved Host credit.
Experimental dynamic mode holds the other tier fixed, restores when ACTIVE is observed,
and limits WAIT shrinking to a small amount of actually free Guest memory.
"""
import argparse
import http.client
import json
import os
import selectors
import socket
import subprocess
import time
from pathlib import Path

MIB = 1 << 20
p = argparse.ArgumentParser()
p.add_argument('--api', type=Path, required=True)
p.add_argument('--output', type=Path, required=True)
p.add_argument('--guest', required=True)
p.add_argument('--key', type=Path, required=True)
p.add_argument('--stop', type=Path, required=True)
p.add_argument('--policy', choices=['static', 'reporting', 'dynamic'], default='static')
p.add_argument('--fast-mib', type=int, default=1280)
p.add_argument('--slow-mib', type=int, default=4096)
p.add_argument('--reserve-mib', type=int, default=192)
p.add_argument('--shrink-mib', type=int, default=128)
p.add_argument('--expected-reporting', choices=['on','off'], required=True)
p.add_argument('--expected-receipts', type=int, choices=[0,1], default=1)
p.add_argument('--expected-order', type=int, choices=range(10), required=True)
p.add_argument('--wait-order', type=int, default=None)
p.add_argument('--ready', type=Path, required=True)
p.add_argument('--expected-zones', required=True)
a = p.parse_args()
expected_zones = [int(x) for x in a.expected_zones.split(',')]
assert len(expected_zones) == 2

class UnixHTTP(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(a.api))

def api(path, data=None):
    c = UnixHTTP('localhost', timeout=3)
    try:
        body = None if data is None else json.dumps(data)
        c.request('GET' if data is None else 'PUT', '/api/v1/' + path, body,
                  {'Content-Type': 'application/json'})
        result = c.getresponse()
        raw = result.read()
        if result.status not in [200, 204]:
            raise RuntimeError((path, result.status, raw.decode()))
        return json.loads(raw) if raw else None
    finally:
        c.close()

def rss(pid):
    proc = Path('/proc') / str(pid)
    ram_ranges = set()
    ram_span = 0
    for line in (proc / 'maps').read_text().splitlines():
        fields = line.split()
        lo, hi = [int(x, 16) for x in fields[0].split('-')]
        if hi-lo >= (1 << 30) and 'memfd:' in line:
            ram_ranges.add(lo)
            ram_span += hi-lo
    # A memory zone can have several mappings around the PCI address hole.
    if len(ram_ranges) < 2 or ram_span != raw_fast + raw_slow:
        raise RuntimeError(f'Guest RAM mapping span mismatch: {ram_span}, ranges={len(ram_ranges)}')
    categories = {k: {} for k in ['guest_ram', 'private_anon', 'other_file']}
    total = {}
    for line in (proc / 'numa_maps').read_text().splitlines():
        fields = dict(v.split('=', 1) for v in line.split() if '=' in v)
        size = int(fields.get('kernelpagesize_kB', 4)) * 1024
        kind = 'guest_ram' if int(line.split()[0], 16) in ram_ranges else ('private_anon' if 'anon' in fields else 'other_file')
        for name, count in fields.items():
            if name.startswith('N') and name[1:].isdigit():
                value = int(count) * size
                total[name] = total.get(name, 0) + value
                categories[kind][name] = categories[kind].get(name, 0) + value
    # Endpoint validity is checked against resident physical pages, not labels.
    if expected_zones[0] == expected_zones[1]:
        allowed = 'N' + str(expected_zones[0])
        if any(n != allowed and b for n, b in categories['guest_ram'].items()):
            raise RuntimeError('Guest RAM escaped the declared physical endpoint')
    return total, categories

def balloon_actual(counters):
    # vm.counters maps device IDs to their per-device counters.
    for name, value in counters.items():
        if isinstance(value, dict) and 'actual' in value and 'hetero_actual' in value:
            return name, [value['actual'], value['hetero_actual']]
    raise RuntimeError('No heterogeneous balloon actual counters')

def scheduling(pid):
    """Raw per-thread counters; vanished threads are missing coverage."""
    threads = {}
    for task in (Path('/proc') / str(pid) / 'task').iterdir():
        try:
            values = [int(x) for x in (task/'schedstat').read_text().split()]
            threads[task.name] = dict(name=(task/'comm').read_text().strip(),
                                     runtime_ns=values[0], runqueue_ns=values[1],
                                     timeslices=values[2])
        except FileNotFoundError:
            continue
    cpus = {}
    actual_cpus = sorted({c for x in info['config']['cpus']['affinity'] for c in x['host_cpus']})
    for cpu in actual_cpus:
        base = Path(f'/sys/devices/system/cpu/cpu{cpu}')
        values = {}
        for name in ['cpufreq/scaling_cur_freq', 'cpufreq/scaling_governor',
                     'thermal_throttle/core_throttle_count', 'thermal_throttle/package_throttle_count']:
            path = base/name
            if path.exists():
                values[name] = path.read_text().strip()
        cpus[str(cpu)] = values
    cpu_ticks = {s[0]: [int(x) for x in s[1:]] for line in Path('/proc/stat').read_text().splitlines()
                 if (s := line.split()) and s[0] in ['cpu'+str(c) for c in actual_cpus]}
    return dict(threads=threads, host_cpus=cpus, host_cpu_ticks=cpu_ticks)

info = api('vm.info')
ping = api('vmm.ping')
assert info['config']['balloon']['free_page_reporting'] == (a.expected_reporting=='on'), 'actual VMM reporting flag mismatch'
pid = ping['pid']
command = (Path('/proc') / str(pid) / 'cmdline').read_bytes()
assert b'/HW/Demeter/sosp25-demeter/hypervisor/' in command
assert b'.scratch-' in command, 'only own experiment VMM permitted'
zones = info['config']['memory']['zones']
assert [next(z['host_numa_node'] for z in zones if z['id']==k) for k in ['dram','slow']] == expected_zones, 'actual RAM zone mapping mismatch'
raw_fast = next(z['size'] for z in zones if z['id'] == 'dram')
raw_slow = next(z['size'] for z in zones if z['id'] == 'slow')
nominal = [raw_fast - a.fast_mib * MIB, raw_slow - a.slow_mib * MIB]
assert info['config']['balloon']['deflate_on_oom'] is False
assert info['config']['balloon']['heterogeneous_memory'] is True
state = {'desired': nominal[:], 'actual': None, 'wait_since': None,
         'operation': None, 'capacity_returned_bytes': 0,
         'host_drop_since_request_bytes': 0, 'receipt': False}
ssh = subprocess.Popen(['ssh', '-i', str(a.key), '-o', 'StrictHostKeyChecking=no',
                        '-o', 'ConnectTimeout=5', 'clear@' + a.guest,
                        'sudo python3 -u /tmp/capacity_guest.py'],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
selector = selectors.DefaultSelector()
selector.register(ssh.stdout, selectors.EVENT_READ)
guest = None
last_guest = None
started = time.monotonic()
errors = 0
with a.output.open('x', buffering=1) as log:
    def emit(event, **fields):
        log.write(json.dumps({'event': event, 'host_monotonic_ns': time.monotonic_ns(),
                              'epoch': time.time(), **fields}) + '\n')
    emit('init', pid=pid, policy=a.policy, info=info, nominal_balloon=nominal,
         scope='one VMM; RSS includes shared pages; baselines; no hard per-node Host limit')
    try:
        while not a.stop.exists():
            for _, _ in selector.select(.25):
                line = ssh.stdout.readline()
                if not line:
                    raise RuntimeError('Guest state stream ended')
                guest = json.loads(line)
                last_guest = time.monotonic()
            if guest is None:
                if time.monotonic() - started > 15:
                    raise RuntimeError('Guest observation unavailable')
                continue
            nodes, breakdown = rss(pid)
            counters = api('vm.counters')
            balloon_id, actual = balloon_actual(counters)
            device = counters[balloon_id]
            if device.get('reporting_observed') != a.expected_receipts:
                raise RuntimeError('Host receipt observation switch mismatch')
            stat = (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()
            cpu_s = (int(stat[11]) + int(stat[12])) / os.sysconf('SC_CLK_TCK')
            state['actual'] = actual
            if not a.ready.exists():
                if guest['page_reporting_order'] not in {a.expected_order, a.wait_order}:
                    raise RuntimeError('Guest reporting order mismatch')
                if actual != nominal:
                    raise RuntimeError('initial balloon actual not at target')
                if a.expected_reporting == 'on' and not guest['reporting'].get('reporting_pages',0):
                    raise RuntimeError('reporting enabled but no acknowledged pages')
                a.ready.write_text(json.dumps({'actual_balloon':actual,'reporting':info['config']['balloon']['free_page_reporting'],'order':guest['page_reporting_order'],'pid':pid})+'\n')
            fresh = time.monotonic() - last_guest <= 1
            phase = guest['sandbox'].get('phase')
            waiting = fresh and (guest['stage'] == 'freed' if guest['stage'] is not None else phase == 0)
            if waiting:
                if state['wait_since'] is None:
                    state['wait_since'] = time.monotonic()
            else:
                state['wait_since'] = None
            if a.policy == 'dynamic':
                # Restore before leaving WAIT; reserve remains owned by this VM.
                # No release credit is issued solely from a pending request.
                if not waiting and state['desired'] != nominal:
                    api('vm.resize', {'desired_balloon': nominal})
                    emit('restore_requested', actual_before=actual, target=nominal)
                    state.update(desired=nominal[:], operation=None, receipt=False,
                                 capacity_returned_bytes=0, host_drop_since_request_bytes=0)
                elif waiting and state['desired'] == nominal and state['wait_since'] is not None and time.monotonic() - state['wait_since'] >= 2:
                    free = guest['nodes']['0']['MemFree']
                    amount = min(a.shrink_mib * MIB, max(0, free - a.reserve_mib * MIB))
                    amount = amount // (16 * MIB) * (16 * MIB)
                    if amount and actual == nominal:
                        desired = [nominal[0] + amount, nominal[1]]
                        api('vm.resize', {'desired_balloon': desired})
                        state.update(desired=desired, operation={'started': time.monotonic(),
                                     'rss_before': nodes.get('N0', 0), 'amount': amount})
                        emit('shrink_requested', target=desired, free_bytes=free,
                             reserve_bytes=a.reserve_mib*MIB, actual_before=actual,
                             host_ddr_before=nodes.get('N0', 0))
                if state['operation']:
                    op = state['operation']
                    state['capacity_returned_bytes'] = max(0, actual[0] - nominal[0])
                    state['host_drop_since_request_bytes'] = max(0, op['rss_before'] - nodes.get('N0', 0))
                    if not state['receipt'] and actual == state['desired'] and state['host_drop_since_request_bytes'] >= op['amount']:
                        state['receipt'] = True
                        emit('release_receipt', amount_bytes=op['amount'], actual=actual,
                             observed_ddr_drop_bytes=state['host_drop_since_request_bytes'],
                             elapsed_s=time.monotonic()-op['started'])
                    if time.monotonic() - op['started'] > 5 and actual != state['desired']:
                        api('vm.resize', {'desired_balloon': nominal})
                        emit('shrink_timeout_rollback', actual=actual)
                        state.update(desired=nominal[:], operation=None, receipt=False)
            if guest['page_reporting_order'] not in {a.expected_order, a.wait_order}:
                raise RuntimeError('unexpected runtime reporting order')
            emit('sample', vmm_cpu_s=cpu_s, vmm_minor_faults=int(stat[7]), vmm_major_faults=int(stat[9]),
                 scheduling=scheduling(pid), vmm_balloon_counters=device, rss_node_bytes=nodes, rss_breakdown=breakdown, guest=guest, guest_fresh=fresh,
                 balloon_actual_bytes=actual, balloon_desired_bytes=state['desired'],
                 actual_fast_available_bytes=raw_fast-actual[0],
                 payload_quota_debt_pages=max(0, guest['sandbox'].get('fast_pages', 0)-guest['sandbox'].get('quota_pages', 0)),
                 capacity_state=state.copy())
    except (OSError, RuntimeError, ValueError) as err:
        errors += 1
        emit('monitor_error', error=str(err))
    finally:
        try:
            if state['desired'] != nominal:
                api('vm.resize', {'desired_balloon': nominal})
                emit('final_restore_requested', target=nominal)
        except Exception as err:
            errors += 1
            emit('final_restore_error', error=str(err))
        ssh.terminate()
        try:
            _, stderr = ssh.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            ssh.kill(); _, stderr = ssh.communicate()
        emit('end', errors=errors, guest_stream_stderr=stderr)
if errors:
    raise SystemExit(1)
