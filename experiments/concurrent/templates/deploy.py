"""Transfer only staged paths into this experiment's private L1 data disk."""
from pathlib import Path
import subprocess
from outer import ROOT, W, KEY, PORT, ssh

BASE = W.parents[1]
H = Path(__file__).resolve().parent


def transfer(paths, label):
    paths = [Path(p) for p in paths]
    members = [str(p.relative_to(BASE)) for p in paths]
    for p in paths:
        if not p.exists():
            raise FileNotFoundError(p)
    tar = subprocess.Popen(['tar', '--sparse', '-C', str(BASE), '-cf', '-', *members], stdout=subprocess.PIPE)
    with (ROOT / (label + '.transfer.log')).open('w') as log:
        remote = ssh('tar --no-same-owner -C /pilot/PhaseTieing -xf -', timeout=None,
                     stdin=tar.stdout, stdout=log, stderr=log)
    tar.stdout.close()
    code = tar.wait()
    if code or remote.returncode:
        raise RuntimeError(('transfer', label, code, remote.returncode))


def provision():
    import json
    user = json.loads((H / 'site.json').read_text())['l0']['outer_user']
    ssh(f'sudo mkdir -p /pilot /pilot-swap {BASE.parent}; '
        'mountpoint -q /pilot || sudo mount /dev/vdb /pilot; '
        f'sudo chown {user}:{user} /pilot; mkdir -p /pilot/PhaseTieing; '
        f'test -e {BASE} || sudo ln -s /pilot/PhaseTieing {BASE}; '
        'sudo swapoff -a', check=True)


def main():
    provision()
    transfer([W / 'phase', W / 'inputs', W / 'validated-root-disks',
              W / 'randomize_cgroup_once.sh', BASE / 'experiments/noop-random',
              BASE / 'experiments/physical-smoke', BASE / 'experiments/swebench',
              BASE / 'sosp25-PhaseTieing', ROOT / 'protocol.json', ROOT / 'inputs'], 'bootstrap-assets')
    ssh(f'mkdir -p {ROOT}/plans {ROOT}/cohorts {ROOT}/_input_blobs; '
        f'chmod 600 {BASE}/experiments/physical-smoke/id_ed25519; '
        'test -c /dev/kvm; test "$(wc -l < /proc/swaps)" = 1', check=True)
