"""Capture all seccomp SIGSYS calls in an experiment-owned L1 without ptrace.

Uses an independent tracefs instance and a uniquely named force_sig_seccomp
kprobe. No VMM seccomp filters or machine-wide tracing controls are changed.
"""
import argparse
import json
import os
from pathlib import Path
import re
import select
import signal
import time


def parse(line):
    row = dict(epoch=time.time(), raw=line)
    for name in ['syscall', 'reason', 'fatal']:
        match = re.search(r'\b' + name + r'=(-?\d+)\b', line)
        if match:
            row[name] = int(match[1])
    match = re.search(r'(.+)-(\d+)\s+\[\d+\]', line)
    if match:
        row.update(comm=match[1].strip(), tid=int(match[2]))
        try:
            proc = Path('/proc')/match[2]/'status'
            state = dict(item.split(':',1) for item in proc.read_text().splitlines() if ':' in item)
            row['tgid'] = int(state['Tgid'].strip())
        except (FileNotFoundError, ProcessLookupError):
            pass
    return row


def save(path, value):
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, indent=2)+'\n')
    temporary.replace(path)


def collect(out, ready, stop, trace_root=Path('/sys/kernel/tracing')):
    name = 'PhaseTieing_SIGSYS_' + str(os.getpid())
    instance = trace_root/'instances'/name
    event = name + '/trap'
    stopped = False
    def terminate(signum, frame):
        nonlocal stopped
        stopped = True
    for sig in [signal.SIGTERM, signal.SIGINT]:
        signal.signal(sig, terminate)
    registered = False
    fd = None
    pending = b''
    def control(command):
        descriptor=os.open(trace_root/'kprobe_events',os.O_WRONLY|os.O_APPEND)
        try:
            os.write(descriptor,(command+'\n').encode())
        finally:
            os.close(descriptor)
    with out.open('x', buffering=1) as stream:
        def emit(data):
            stream.write(json.dumps(data)+'\n')
        def read_events():
            nonlocal pending
            while True:
                try:
                    data = os.read(fd,65536)
                except BlockingIOError:
                    break
                if not data:
                    break
                pending += data
                while b'\n' in pending:
                    line,pending = pending.split(b'\n',1)
                    if line.strip():
                        emit(parse(line.decode(errors='replace')))
        try:
            control(f'p:{event} force_sig_seccomp syscall=$arg1:s32 reason=$arg2:s32 fatal=$arg3:u8')
            registered = True
            instance.mkdir()
            # Small per-CPU buffers: only the rare SIGSYS path is traced.
            (instance/'buffer_size_kb').write_text('16')
            (instance/'events'/name/'trap/enable').write_text('1')
            (instance/'tracing_on').write_text('1')
            fd = os.open(instance/'trace_pipe',os.O_RDONLY|os.O_NONBLOCK)
            save(ready,dict(pid=os.getpid(),trace_instance=str(instance),event=event,
                function='force_sig_seccomp',buffer_kib_per_cpu=16,
                seccomp_filters_changed=False,ptrace=False))
            while not stopped and not stop.exists():
                if select.select([fd],[],[],.5)[0]:
                    read_events()
        finally:
            if instance.exists():
                (instance/'tracing_on').write_text('0')
                if fd is not None:
                    read_events()
                    os.close(fd)
                stats={p.parent.name:p.read_text() for p in instance.glob('per_cpu/cpu*/stats')}
                emit(dict(event='collector-stopped',epoch=time.time(),per_cpu_stats=stats))
                (instance/'events'/name/'trap/enable').write_text('0')
                instance.rmdir()
            if registered:
                control('-:'+event)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--ready',type=Path,required=True)
    parser.add_argument('--stop',type=Path,required=True)
    args=parser.parse_args()
    collect(args.out,args.ready,args.stop)


if __name__=='__main__':
    main()
