"""Provision this batch's private L1 data disk and frozen sources."""
import hashlib
import json
from pathlib import Path
import subprocess
from outer import ROOT, W, KEY, PORT, ssh

H = Path(__file__).resolve().parent
BASE = W.parents[1]


def transfer(paths, label):
    paths = [Path(p) for p in paths]
    for path in paths:
        if not path.exists():
            raise FileNotFoundError(path)
    tar = subprocess.Popen(['tar', '--sparse', '-C', '/',
        '--transform=s,^r0/home/geerties/HW/Demeter/,pilot/demeter/,',
        '-cf', '-', *[str(p).lstrip('/') for p in paths]], stdout=subprocess.PIPE)
    with (ROOT / (label + '.transfer.log')).open('w') as log:
        remote = subprocess.run(['ssh', '-i', str(KEY), '-p', str(PORT),
            '-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null',
            'hs@127.0.0.1', 'tar --no-same-owner -C / -xf -'],
            stdin=tar.stdout, stdout=log, stderr=log)
    tar.stdout.close()
    code = tar.wait()
    if code or remote.returncode:
        raise RuntimeError((label, code, remote.returncode))
    print('TRANSFERRED', label, flush=True)


def provision():
    # These commands operate only inside this batch's new L1 VM.
    ssh('sudo mkdir -p /pilot /pilot-swap /r0/home/geerties/HW; '
        'mountpoint -q /pilot || sudo mount /dev/vdb /pilot; sudo chown hs:hs /pilot; '
        'mkdir -p /pilot/demeter; '
        'test -e /r0/home/geerties/HW/Demeter || '
        'sudo ln -s /pilot/demeter /r0/home/geerties/HW/Demeter; '
        'sudo swapoff -a; '
        'sudo systemctl stop apt-daily.timer apt-daily-upgrade.timer '
        'snapd.service snapd.socket update-notifier-download.timer 2>/dev/null || true',
        check=True)


def main():
    provision()
    art = W / 'phase/artifacts'
    paths = [art / name for name in ['vmm-v66-ssd-compact',
        'vmm-v66-ssd-compact-readguard', 'vmm-v67-compact-cxl',
        'vmm-v67-compact-cxl-readguard']]
    paths += [W / 'phase/all_traces_v67/relocate_private.py',
        W / 'phase/three_modes/verify_topology.py',
        W / 'phase/ssd_placement_v54/guest_state.py', W / 'randomize_cgroup_once.sh',
        BASE / 'experiments/noop-random/build/randomize_pages',
        BASE / 'experiments/physical-smoke/id_ed25519',
        BASE / 'experiments/swebench/trajectory_launcher.py',
        BASE / 'sosp25-demeter/hypervisor/target/host-returns-v20-iowq-cap/cloud-hypervisor', H]
    transfer(paths, 'frozen-sources')
    ssh(f'mkdir -p {W}/validated-root-disks {BASE}/experiments/swebench/swesmith '
        f'{ROOT}/inputs {ROOT}/plans {ROOT}/cohorts {ROOT}/_input_blobs', check=True)
    image = Path('/var/tmp/geerties-acg68-20261029/root-flat.qcow2')
    with image.open('rb') as stream:
        ssh(f'cat > {W}/validated-root-disks/root-demeter.qcow2',
            stdin=stream, timeout=1200, check=True)
    cases = json.loads((ROOT / 'inputs/cases.json').read_text())
    for case in cases:
        transfer([case['golden'], case['trace']], case['name'])
    transfer([ROOT / 'inputs/cases.json', ROOT / 'protocol.json'], 'protocol')
    ssh(f'chmod 600 {BASE}/experiments/physical-smoke/id_ed25519; '
        'test -c /dev/kvm; test "$(wc -l < /proc/swaps)" = 1', check=True)
    print('DEPLOYMENT COMPLETE', flush=True)


if __name__ == '__main__':
    main()
