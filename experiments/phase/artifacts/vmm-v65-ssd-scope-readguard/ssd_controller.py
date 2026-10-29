#!/usr/bin/env python3
"""Guest-delegated SSD tier: ACTIVE memory.high and bounded WAIT reclaim.

Anonymous pages go to an exclusive virtio swap disk; clean file cache can be
dropped. Linux restores swapped pages on demand. This is a cgroup resident
target, not a whole-VMM hard limit or a PEBS page-benefit implementation.
The pinned Linux 6.10 accepts only a byte count in memory.reclaim.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import errno
import json
from pathlib import Path
import selectors
import socket
import time

MIB = 1 << 20


def pairs(path):
    return {k: int(v) for k, v in (line.split() for line in path.read_text().splitlines())}


def reclaim_once(group, amount):
    started = time.monotonic_ns()
    before = int((group / 'memory.current').read_text())
    swap_before = int((group / 'memory.swap.current').read_text())
    error = None
    try:
        (group / 'memory.reclaim').write_text(str(amount))
    except OSError as exc:
        # EAGAIN can accompany partial reclamation. Preserve both the error
        # and observations; requested bytes never become release credit.
        error = {'errno': exc.errno, 'message': str(exc)}
    return dict(event='reclaim', requested_bytes=amount, before_bytes=before,
                after_bytes=int((group / 'memory.current').read_text()),
                swap_before_bytes=swap_before,
                swap_after_bytes=int((group / 'memory.swap.current').read_text()),
                elapsed_ns=time.monotonic_ns() - started, error=error)


class SSDController:
    def __init__(self, group, active, wait, delay_ms, chunk, fast_node, log):
        self.group, self.active, self.wait = group, active, wait
        self.delay_s, self.chunk = delay_ms / 1000, chunk
        self.fast_node, self.log = fast_node, log
        self.phase, self.busy, self.wait_since = 0, 0, time.monotonic()
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.future = None
        self.seq = 0
        for name in ['memory.current', 'memory.high', 'memory.max',
                     'memory.swap.current', 'memory.swap.max', 'memory.reclaim']:
            if not (group / name).exists():
                raise RuntimeError('required cgroup interface missing: ' + name)
        # Set the execution quota before the workload joins. Keep it constant
        # across WAIT: 6.10 high writes synchronously reclaim, so lowering high
        # on the event socket could delay ACKs by seconds. WAIT uses a worker.
        (group / 'memory.high').write_text(str(active))
        (group / 'memory.max').write_text('max')
        (group / 'memory.swap.max').write_text(str(16 * 1024 * MIB))
        self.emit(event='init', active_bytes=active, wait_target_bytes=wait,
                  wait_delay_ms=delay_ms, reclaim_chunk_bytes=chunk,
                  memory_max='max', backend='guest-ssd-swap')

    def emit(self, **data):
        self.seq += 1
        self.log.write(json.dumps(dict(seq=self.seq, monotonic_ns=time.monotonic_ns(),
                                      controller_cpu_ns=time.process_time_ns(), **data)) + '\n')
        self.log.flush()

    def finish_reclaim(self, block=False):
        if self.future and (block or self.future.done()):
            # Before start ACK, finish the single existing chunk. No new WAIT
            # job is submitted until the last concurrent tool has ended.
            result = self.future.result(timeout=1.5 if block else None)
            self.future = None
            self.emit(**result)
            error = result['error']
            if error and error['errno'] != errno.EAGAIN:
                raise RuntimeError('SSD reclaim failed: ' + str(error))

    def event(self, message):
        if str(message.get('sandbox')) != '0':
            raise ValueError('this experiment controller manages Sandbox 0')
        action = message.get('event')
        if action not in ['start', 'end', 'fallback']:
            raise ValueError('unknown Runtime event')
        if action == 'start':
            self.busy += 1
            self.phase = 1
            self.finish_reclaim(block=True)
        elif action == 'end':
            self.busy = max(0, self.busy - 1)
            self.phase = int(self.busy > 0)
            if not self.phase:
                self.wait_since = time.monotonic()
        self.emit(event='phase', phase=self.phase, busy=self.busy,
                  action=action, kind=message.get('kind', 0),
                  resident_bytes=int((self.group / 'memory.current').read_text()),
                  swap_bytes=int((self.group / 'memory.swap.current').read_text()))
        return dict(preferred_node=self.fast_node, phase=self.phase,
                    quota_bytes=self.active if self.phase else self.wait,
                    backend='ssd', recovery='demand-fault')

    def tick(self):
        self.finish_reclaim()
        current = int((self.group / 'memory.current').read_text())
        self.emit(event='sample', phase=self.phase, resident_bytes=current,
                  swap_bytes=int((self.group / 'memory.swap.current').read_text()),
                  memory_events=pairs(self.group / 'memory.events'),
                  memory_stat=pairs(self.group / 'memory.stat'),
                  psi=(self.group / 'memory.pressure').read_text().strip())
        if (not self.phase and self.future is None and
                time.monotonic() - self.wait_since >= self.delay_s and
                current > self.wait):
            amount = min(self.chunk, current - self.wait)
            self.future = self.pool.submit(reclaim_once, self.group, amount)

    def close(self):
        self.pool.shutdown(wait=True)
        self.finish_reclaim()
        (self.group / 'memory.high').write_text('max')


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--config', type=Path, required=True)
    p.add_argument('--socket', type=Path, required=True)
    p.add_argument('--log', type=Path, required=True)
    p.add_argument('--budget-mib', type=int, required=True)
    p.add_argument('--wait-mib', type=int, default=16)
    p.add_argument('--wait-delay-ms', type=int, default=500)
    p.add_argument('--chunk-mib', type=int, default=8)
    p.add_argument('--fast-node', type=int, default=0)
    # Common launch flags; SSD does not allocate a slow NUMA node or headroom.
    p.add_argument('--slow-node', type=int, default=1)
    p.add_argument('--startup-headroom-mib', type=int, default=0)
    a = p.parse_args()
    if not (0 < a.wait_mib <= a.budget_mib and 0 < a.chunk_mib <= 64
            and a.wait_delay_ms >= 0 and 0 <= a.fast_node < 64):
        p.error('invalid SSD resident target or reclaim settings')
    entries = json.loads(a.config.read_text())
    if len(entries) != 1 or str(entries[0]['id']) != '0':
        p.error('single Sandbox experiment expected')
    if a.socket.exists():
        p.error('refusing to replace an existing controller socket')
    with a.log.open('x') as log:
        controller = SSDController(Path(entries[0]['cgroup']), a.budget_mib * MIB,
                                   a.wait_mib * MIB, a.wait_delay_ms,
                                   a.chunk_mib * MIB, a.fast_node, log)
        try:
            with socket.socket(socket.AF_UNIX) as server, selectors.DefaultSelector() as sel:
                server.bind(str(a.socket))
                server.listen(16)
                sel.register(server, selectors.EVENT_READ)
                deadline = time.monotonic()
                while True:
                    for _, _ in sel.select(max(0, deadline - time.monotonic())):
                        with server.accept()[0] as conn:
                            conn.settimeout(1)
                            data = conn.makefile('rb').readline(8192)
                            try:
                                response = controller.event(json.loads(data))
                            except Exception as exc:
                                controller.emit(event='error', error=repr(exc))
                                response = {'error': repr(exc)}
                            conn.sendall(json.dumps(response).encode() + b'\n')
                    if time.monotonic() >= deadline:
                        controller.tick()
                        deadline = time.monotonic() + .1
        finally:
            controller.close()
            a.socket.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
