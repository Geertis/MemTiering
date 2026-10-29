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
p.add_argument('--expected-order', type=int, choices=[5,9], required=True)
p.add_argument('--ready', type=Path, required=True)
a = p.parse_args()

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
    values = {}
    for line in (proc / 'numa_maps').read_text().splitlines():
        fields = dict(v.split('=', 1) for v in line.split() if '=' in v)
        size = int(fields.get('kernelpagesize_kB', 4)) * 1024
        for name, count in fields.items():
            if name.startswith('N') and name[1:].isdigit():
                values[name] = values.get(name, 0) + int(count) * size
    return values

def balloon_actual(counters):
    # vm.counters maps device IDs to their per-device counters.
    for name, value in counters.items():
        if isinstance(value, dict) and 'actual' in value and 'hetero_actual' in value:
            return name, [value['actual'], value['hetero_actual']]
    raise RuntimeError('No heterogeneous balloon actual counters')

info = api('vm.info')
ping = api('vmm.ping')
assert info['config']['balloon']['free_page_reporting'] == (a.expected_reporting=='on'), 'actual VMM reporting flag mismatch'
pid = ping['pid']
command = (Path('/proc') / str(pid) / 'cmdline').read_bytes()
assert b'/HW/Demeter/sosp25-demeter/hypervisor/' in command
assert b'.scratch-' in command, 'only own experiment VMM permitted'
zones = info['config']['memory']['zones']
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
            nodes = rss(pid)
            counters = api('vm.counters')
            _, actual = balloon_actual(counters)
            state['actual'] = actual
            if not a.ready.exists():
                if guest['page_reporting_order'] != a.expected_order:
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
            emit('sample', rss_node_bytes=nodes, guest=guest, guest_fresh=fresh,
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
