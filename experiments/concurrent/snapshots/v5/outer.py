#!/usr/bin/env python3
"""Owned outer VM for the approved PhaseTieing formal batch; exact PID/QMP cleanup."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import time

W = Path('/r0/home/geerties/HW/Demeter/experiments/agentcgroup-demeter')
ROOT = W / 'results/PhaseTieing-concurrent-20261030-v5-capacity-scan'
BASE = W / 'results/concurrent-v68-pilot-20261029/outer-preflight/root.qcow2'
KEY = Path('/r0/home/geerties/HW/HugeScope/hugescope-atc24-ae/experiments/vm/id_ed25519')
KERNEL = Path('/boot/vmlinuz-6.10.0-demeterhost-loadfb-20261005')
INITRD = Path('/boot/initrd.img-6.10.0-demeterhost-loadfb-20261005')
PORT = 22944
VCPU_CORES = list(range(32, 64)) + list(range(24, 28))
HELPER_CORES = [28, 29]
STORAGE = Path('/var/tmp/geerties-PhaseTieing-concurrent-20261029')


def save(path, value):
    path = Path(path)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    tmp.replace(path)


def ssh(command, timeout=60, **kwargs):
    return subprocess.run(['ssh', '-i', str(KEY), '-p', str(PORT),
        '-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null',
        '-o', 'ConnectTimeout=3', '-o', 'ServerAliveInterval=10',
        'hs@127.0.0.1', command], timeout=timeout, **kwargs)


def qmp(path, command, arguments=None):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(10)
        sock.connect(str(path))
        f = sock.makefile('rwb', buffering=0)
        f.readline()
        def request(cmd, args=None):
            payload = {'execute': cmd}
            if args is not None:
                payload['arguments'] = args
            f.write(json.dumps(payload).encode() + b'\n')
            while True:
                row = json.loads(f.readline())
                if 'error' in row:
                    raise RuntimeError(row)
                if 'return' in row:
                    return row['return']
        request('qmp_capabilities')
        return request(command, arguments)


def start(label, dram, cold=0):
    if not label.replace('-', '').isalnum():
        raise ValueError('simple owned label required')
    folder = ROOT / label
    folder.mkdir(exist_ok=True)
    meta = folder / 'outer.json'
    if meta.exists():
        previous = json.loads(meta.read_text())
        if previous.get('pid') and Path(f"/proc/{previous['pid']}").exists():
            raise RuntimeError('owned outer VM is already running')
    root = folder / 'root.qcow2'
    if not root.exists():
        subprocess.run(['qemu-img', 'create', '-q', '-f', 'qcow2', '-F', 'qcow2',
            '-b', str(BASE), str(root)], check=True)
    STORAGE.mkdir(mode=0o700, exist_ok=True)
    data = STORAGE / 'formal-data.raw'
    if not data.exists():
        with data.open('xb') as f:
            f.truncate(256 * (1 << 30))
        subprocess.run(['mkfs.ext4', '-q', '-F', '-L', 'PhaseTieing-data', str(data)], check=True)
    qpath = Path('/tmp') / ('pt-formal-' + hashlib.sha256(str(folder).encode()).hexdigest()[:12] + '.qmp')
    if qpath.exists():
        raise RuntimeError('owned QMP socket exists; inspect before reuse')
    cmd = ['sudo', '-n', 'prlimit', '--memlock=unlimited', '--', 'taskset', '-c',
        ','.join(map(str, VCPU_CORES + HELPER_CORES)), 'qemu-system-x86_64', '-name', 'pt-formal-' + label,
        '-machine', 'q35,accel=kvm,mem-merge=off', '-cpu', 'host', '-smp', str(len(VCPU_CORES)),
        '-m', f'{dram+cold}G', '-object',
        f'memory-backend-ram,id=dram,size={dram}G,host-nodes=1,policy=bind,prealloc=on,merge=off',
        '-numa', f'node,nodeid=0,cpus=0-{len(VCPU_CORES)-1},memdev=dram']
    if cold:
        cmd += ['-object', f'memory-backend-ram,id=cold,size={cold}G,host-nodes=2,policy=bind,prealloc=on,merge=off',
            '-numa', 'node,nodeid=1,memdev=cold']
    cold_disk = W / 'results/PhaseTieing-concurrent-20261029-formal/nvme-cold.raw'
    if not cold_disk.exists():
        with cold_disk.open('xb') as f:
            f.truncate(72 * (1 << 30))
        subprocess.run(['mkfs.ext4', '-q', '-F', '-L', 'PhaseTieing-cold', str(cold_disk)], check=True)
    cmd += ['-overcommit', 'mem-lock=on', '-kernel', str(KERNEL), '-initrd', str(INITRD),
        '-append', 'root=/dev/vda1 rw console=ttyS0 transparent_hugepage=never cgroup_no_v1=all',
        '-drive', f'file={root},if=virtio,format=qcow2,cache=none,discard=unmap',
        '-drive', f'file={data},if=virtio,format=raw,cache=none,discard=unmap',
        '-drive', f'file={cold_disk},if=virtio,format=raw,cache=none,discard=unmap',
        '-netdev', f'user,id=net0,hostfwd=tcp:127.0.0.1:{PORT}-:22',
        '-device', 'virtio-net-pci,netdev=net0', '-device', 'virtio-rng-pci',
        '-display', 'none', '-serial', f'file:{folder}/serial.log',
        '-qmp', f'unix:{qpath},server=on,wait=off',
        '-pidfile', str(folder / 'qemu.pid'), '-daemonize']
    state = dict(label=label, dram_gib=dram, cxl_gib=cold, vcpus=len(VCPU_CORES),
        vcpu_cores=VCPU_CORES, helper_cores=HELPER_CORES,
        qmp=str(qpath), command=cmd, started_epoch=time.time(), stage='launching')
    save(meta, state)
    p = subprocess.run(cmd, capture_output=True, text=True)
    (folder / 'launch.stdout').write_text(p.stdout)
    (folder / 'launch.stderr').write_text(p.stderr)
    state['launch_rc'] = p.returncode
    if p.returncode:
        state['stage'] = 'launch-failed'
        save(meta, state)
        raise RuntimeError(p.stderr)
    return finish_start(folder, state)


def finish_start(folder, state):
    label = state['label']
    qpath = Path(state['qmp'])
    state['pid'] = int(subprocess.check_output(['sudo', '-n', 'cat', str(folder / 'qemu.pid')], text=True))
    for path in [folder / 'qemu.pid', folder / 'serial.log', qpath]:
        subprocess.run(['sudo', '-n', 'chown', f'{os.getuid()}:{os.getgid()}', str(path)], check=True)
    meta = folder / 'outer.json'
    vcpus = qmp(qpath, 'query-cpus-fast')
    vcpu_tids = {p['thread-id'] for p in vcpus}
    for task in Path(f"/proc/{state['pid']}/task").iterdir():
        if int(task.name) not in vcpu_tids:
            subprocess.run(['sudo', '-n', 'taskset', '-pc', ','.join(map(str, HELPER_CORES)), task.name],
                check=True, stdout=subprocess.DEVNULL)
    for cpu in vcpus:
        subprocess.run(['sudo', '-n', 'taskset', '-pc', str(VCPU_CORES[cpu['cpu-index']]),
            str(cpu['thread-id'])], check=True, stdout=subprocess.DEVNULL)
    state['vcpu_threads'] = vcpus
    state['stage'] = 'waiting-ssh'
    save(meta, state)
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        p = ssh('true', timeout=8, capture_output=True)
        if p.returncode == 0:
            state.update(stage='ready', ready_epoch=time.time())
            save(meta, state)
            print(json.dumps({'label': label, 'stage': 'ready', 'pid': state['pid']}), flush=True)
            return state
        time.sleep(2)
    state['stage'] = 'ssh-timeout'
    save(meta, state)
    raise RuntimeError('outer SSH timeout; serial log retained')


def stop(label):
    folder = ROOT / label
    state = json.loads((folder / 'outer.json').read_text())
    pid = state.get('pid')
    if pid and Path(f'/proc/{pid}').exists():
        command = Path(f'/proc/{pid}/cmdline').read_bytes()
        if ('pt-formal-' + label).encode() not in command or str(folder / 'root.qcow2').encode() not in command:
            raise RuntimeError('owned VM identity mismatch')
        qmp(state['qmp'], 'quit')
    state.update(stage='stopped', stopped_epoch=time.time())
    save(folder / 'outer.json', state)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=['start', 'attach', 'stop', 'ssh'])
    parser.add_argument('--label', default='outer-preflight')
    parser.add_argument('--dram', type=int, default=8)
    parser.add_argument('--cold', type=int, default=0)
    parser.add_argument('--command', default='true')
    args = parser.parse_args()
    if args.operation == 'start':
        start(args.label, args.dram, args.cold)
    elif args.operation == 'attach':
        folder = ROOT / args.label
        finish_start(folder, json.loads((folder / 'outer.json').read_text()))
    elif args.operation == 'stop':
        stop(args.label)
    else:
        raise SystemExit(ssh(args.command, timeout=120).returncode)
