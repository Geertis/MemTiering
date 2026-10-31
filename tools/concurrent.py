#!/usr/bin/env python3
"""Plan and stage the measured v16 experiment in an owned, site-configured lab.

Staging never starts a VM. bootstrap/run are explicit operations on that lab.
Large assets and SSH credentials stay outside the source release.
"""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / 'experiments/concurrent'
OLD = '/r0/home/geerties/HW/Demeter'
OLD_RUN = 'PhaseTieing-concurrent-20261031-v16-iowq-cap'
VARIANTS = ['vmm-v66-ssd-compact', 'vmm-v66-ssd-compact-readguard',
            'vmm-v67-compact-cxl', 'vmm-v67-compact-cxl-readguard']


def read(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def config(path):
    c = read(path)
    lab = Path(c['lab_root'])
    if not lab.is_absolute() or not re.fullmatch(r'/[A-Za-z0-9_./-]+', str(lab)):
        raise ValueError('lab_root must be an absolute path without shell metacharacters/spaces')
    if '..' in lab.parts or lab == Path('/') or len(lab.parts) < 4:
        raise ValueError('choose a dedicated, owned lab directory')
    if not re.fullmatch(r'[A-Za-z0-9-]+', c['run_name']):
        raise ValueError('run_name must contain letters, numbers and hyphens only')
    if c['n'] != 8 or not c['dram_gib'] or any(type(d) is not int or d < 1 for d in c['dram_gib']):
        raise ValueError('this measured geometry supports N=8 and positive integer DRAM GiB')
    if not c['repetitions'] or not set(c['repetitions']) <= {1, 2, 3}:
        raise ValueError('published paired arrivals support repetitions 1..3')
    for name, choices in [('media', {'cxl', 'ssd'}), ('strategies', {'P', 'F'})]:
        if not c[name] or not set(c[name]) <= choices or len(set(c[name])) != len(c[name]):
            raise ValueError('invalid or duplicated ' + name)
    if 'F' in c['strategies'] and ('P' not in c['strategies'] or c['strategies'].index('P') > c['strategies'].index('F')):
        raise ValueError('F requires a preceding valid P in the same repetition')
    for name in ['dram_gib', 'repetitions']:
        if len(set(c[name])) != len(c[name]):
            raise ValueError('duplicated ' + name)
    if c['module_abi'] not in ['published', 'measured-legacy']:
        raise ValueError('module_abi must identify the internal kernel module name')
    if c.get('reserve_policy','fixed') not in ['fixed','scaled']:
        raise ValueError('reserve_policy must be fixed or scaled')
    l0 = c['l0']
    cpus, helpers = l0['vcpu_cores'], l0['helper_cores']
    if len(cpus) != 36 or len(set(cpus)) != 36 or not helpers or set(cpus) & set(helpers):
        raise ValueError('need 36 unique vCPU cores and disjoint helper cores')
    if any(type(x) is not int or x < 0 for x in cpus + helpers):
        raise ValueError('CPU IDs must be nonnegative integers')
    if not re.fullmatch(r'[a-z_][a-z0-9_-]*', l0['outer_user']):
        raise ValueError('invalid outer user')
    if not 1024 <= l0['ssh_port'] <= 65535 or l0['dram_node'] == l0['cxl_node']:
        raise ValueError('invalid SSH port or overlapping NUMA nodes')
    c['lab_root'] = str(lab)
    return c


def matrix(c):
    refs = read(SOURCE / 'arrivals.json')
    rows = []
    for dram in c['dram_gib']:
        for repetition in c['repetitions']:
            ref = next(x for x in refs if x['n'] == 8 and x['repetition'] == repetition)
            for medium in c['media']:
                for strategy in c['strategies']:
                    rows.append(dict(label=f'repro-{medium}-{strategy}-d{dram}-n8-r{repetition}',
                        category='CAPACITY-EXPLORATION', strategy=strategy, medium=medium,
                        dram_gib=dram, cxl_gib=64 if medium == 'cxl' else 0, n=8,
                        repetition=repetition, seed=ref['seed'], reference=None,
                        reserve_policy=c.get('reserve_policy','fixed'), require_controller_health=True,
                        exploratory=True, quota_mib=6144 if strategy == 'F' else None,
                        slots=ref['slots']))
    return rows


def layout(c):
    base = Path(c['lab_root'])
    w = base / 'experiments/agentcgroup-PhaseTieing'
    h = w / 'phase/PhaseTieing_concurrent_v16'
    root = w / 'results' / c['run_name']
    return base, w, h, root


def render(text, c):
    text = text.replace(OLD, c['lab_root']).replace(OLD_RUN, c['run_name'])
    for a, b in [('agentcgroup-demeter', 'agentcgroup-PhaseTieing'),
                 ('sosp25-demeter', 'sosp25-PhaseTieing'), ('root-demeter', 'root-PhaseTieing'),
                 ('/pilot/demeter', '/pilot/PhaseTieing'),
                 ('demeter_placement.ko', 'PhaseTieing_placement.ko'),
                 ('demeter_balloon.ko', 'PhaseTieing_balloon.ko')]:
        text = text.replace(a, b)
    if c['module_abi'] == 'published':
        for name in ['placement', 'balloon']:
            text = text.replace('/sys/module/demeter_' + name, '/sys/module/PhaseTieing_' + name)
    else:
        for name in ['placement', 'balloon']:
            text = text.replace('/sys/module/PhaseTieing_' + name, '/sys/module/demeter_' + name)
    return (text.replace('hs@127.0.0.1', c['l0']['outer_user'] + '@127.0.0.1')
            .replace('hs:hs', c['l0']['outer_user'] + ':' + c['l0']['outer_user']))


def text_tree(source, target, c):
    for p in source.rglob('*'):
        if p.suffix not in ['.py', '.sh', '.c', '.h', '.json', '.md'] or '__pycache__' in p.parts:
            continue
        dest = target / p.relative_to(source)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(render(p.read_text(), c))
        dest.chmod(p.stat().st_mode & 0o777)


def replace_assignments(path, values):
    text = path.read_text()
    for name, expression in values.items():
        text, count = re.subn(r'^' + name + r' = .*$', name + ' = ' + expression,
                              text, flags=re.MULTILINE)
        if count != 1:
            raise RuntimeError('unexpected snapshot assignment: ' + name)
    path.write_text(text)


def prepare(c):
    base, w, h, root = layout(c)
    if base.exists():
        raise FileExistsError('refusing to overwrite a lab; choose a new lab_root: ' + str(base))
    base.mkdir(parents=True, mode=0o700)
    save(base / 'site.json', c)
    text_tree(SOURCE / 'snapshots/v16', h, c)
    for name in ['all_traces_v67', 'ssd_placement_v54', 'cold_media', 'three_modes']:
        text_tree(REPO / 'experiments/phase' / name, w / 'phase' / name, c)
    for name in VARIANTS:
        target = h / 'artifacts' / name
        text_tree(REPO / 'experiments/phase/artifacts' / name, target, c)
        (target / 'start-vm.sh').write_text(render((SOURCE / 'snapshots/v16' / ('start-' + name + '.sh')).read_text(), c))
    replace_assignments(h / 'outer.py', {
        'BASE': 'Path(' + repr(c['assets']['l1_base']) + ')',
        'KEY': 'Path(' + repr(c['assets']['outer_ssh_key']) + ')',
        'KERNEL': 'Path(' + repr(c['assets']['l1_kernel']) + ')',
        'INITRD': 'Path(' + repr(c['assets']['l1_initrd']) + ')',
        'PORT': repr(c['l0']['ssh_port']), 'VCPU_CORES': repr(c['l0']['vcpu_cores']),
        'HELPER_CORES': repr(c['l0']['helper_cores']), 'STORAGE': 'Path(' + repr(str(base / 'storage')) + ')'})
    p = h / 'outer.py'
    text = p.read_text().replace('host-nodes=1,', 'host-nodes=DRAM_NODE,')
    text = text.replace('host-nodes=2,', f"host-nodes={c['l0']['cxl_node']},")
    text = text.replace('host-nodes=DRAM_NODE,', f"host-nodes={c['l0']['dram_node']},")
    text = text.replace("W / 'results/PhaseTieing-concurrent-20261029-formal/nvme-cold.raw'", "ROOT / 'nvme-cold.raw'")
    p.write_text(text)
    p = h / 'campaign.py'
    text = p.read_text().replace('wanted = {1, 2} if state[\'cxl_gib\'] else {1}',
        f"wanted = {{{c['l0']['dram_node']}, {c['l0']['cxl_node']}}} if state['cxl_gib'] else {{{c['l0']['dram_node']}}}")
    p.write_text(text)
    # Keep site adaptation separate from the unchanged measured policy code.
    for name in ['deploy.py', 'portable_plan.py']:
        (h / name).write_text(render((SOURCE / 'templates' / name).read_text(), c))
    p = h / 'pressure.py'
    text = p.read_text()
    begin, end = text.index('def plans():'), text.index('\ndef prepare(spec):')
    text = text[:begin] + "def plans():\n    from portable_plan import plans as configured_plans\n    return configured_plans()\n\n" + text[end:]
    begin, end = text.index('def update('), text.index('\ncampaign.update = update')
    text = text[:begin] + '''def update(stage, **values):
    state = json.loads((ROOT / 'status.json').read_text())
    state.update(stage=stage, updated_epoch=time.time(), **values)
    save(ROOT / 'status.json', state)
    (ROOT / 'progress.md').write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\\n')
    print(time.strftime('%Y-%m-%d %H:%M:%S'), stage, values.get('active'), flush=True)

''' + text[end:]
    p.write_text(text)
    # Status reports belong to this lab, never the original author's report dir.
    for name in ['pressure.py', 'campaign.py']:
        p = h / name
        p.write_text(re.sub(r"Path\('/r0/home/geerties/HW/report/[^']+'\)",
                            "(ROOT / 'progress.md')", p.read_text()))
    save(h / 'site.json', c)
    save(h / 'configured-matrix.json', matrix(c))
    cases = []
    for case in read(SOURCE / 'inputs.json')['cases']:
        cases.append(dict(case, golden=str(w / 'inputs/golden' / case['golden_filename']),
                          trace=str(w / 'inputs/traces' / case['name'])))
    save(root / 'inputs/cases.json', cases)
    save(root / 'protocol.json', dict(read(REPO / 'published/diagnostics-20261031-sigsys/v16-protocol.json'),
         version='v16-configured-reproduction', site=c, scan_dram_gib=c['dram_gib'], scan_repetitions=len(c['repetitions']), scope='Configured paired P/F matrix; measured v16 begins with a single P6 regression.', note='Configured port of measured v16; validate on destination hardware.'))
    for name in ['plans', 'cohorts', '_input_blobs']:
        (root / name).mkdir()
    (base / 'experiments/swebench/swesmith').mkdir(parents=True)
    save(root / 'status.json', {'stage': 'prepared-not-started', 'completed': [], 'failed': []})
    for src, dst in [(REPO / 'experiments/randomize_cgroup_once.sh', w / 'randomize_cgroup_once.sh'),
                     (REPO / 'experiments/trajectory_launcher.py', base / 'experiments/swebench/trajectory_launcher.py')]:
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(render(src.read_text(), c))
    # Validate generated Python before any asset copies or VM launch.
    for p in base.rglob('*.py'):
        ast.parse(p.read_text(), filename=str(p))
    print('PREPARED (no VMs started):', h)


def copy_asset(source, target):
    source, target = Path(source), Path(target)
    if not source.is_file():
        raise FileNotFoundError(source)
    if target.exists():
        raise FileExistsError('refusing to overwrite ' + str(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['cp', '--reflink=auto', '--sparse=always', '--', str(source), str(target)], check=True)


def stage_assets(c):
    base, w, h, root = layout(c)
    if read(base / 'site.json') != c:
        raise ValueError('configuration changed after prepare')
    a = c['assets']
    # Verify trace and golden inputs before copying any large files.
    cases = read(SOURCE / 'inputs.json')['cases']
    for case in cases:
        if digest(a['golden'][case['name']]) != case['golden_sha256']:
            raise ValueError('golden image hash mismatch: ' + case['name'])
        for name, expected in case['input_hashes'].items():
            if digest(Path(a['traces']) / case['name'] / name) != expected:
                raise ValueError('trace hash mismatch: ' + case['name'] + '/' + name)
    pairs = [(a['guest_base'], w / 'validated-root-disks/root-PhaseTieing.qcow2'),
             (a['cloud_hypervisor'], base / 'sosp25-PhaseTieing/hypervisor/target/host-returns-v20-iowq-cap/cloud-hypervisor'),
             (a['guest_ssh_key'], base / 'experiments/physical-smoke/id_ed25519'),
             (a['randomize_pages'], base / 'experiments/noop-random/build/randomize_pages')]
    for name in VARIANTS:
        for file in ['vmlinux.bin', 'PhaseTieing_placement.ko', 'PhaseTieing_balloon.ko', 'policy_exec', 'rg']:
            pairs.append((Path(a['guest_artifacts']) / name / file, h / 'artifacts' / name / file))
    for case in cases:
        pairs.append((a['golden'][case['name']], w / 'inputs/golden' / case['golden_filename']))
        for name in case['input_hashes']:
            pairs.append((Path(a['traces']) / case['name'] / name, w / 'inputs/traces' / case['name'] / name))
    for source, target in pairs:
        copy_asset(source, target)
    (base / 'experiments/physical-smoke/id_ed25519').chmod(0o600)
    for source, target in pairs:
        if target.name in ['cloud-hypervisor', 'policy_exec', 'rg', 'randomize_pages']:
            target.chmod(target.stat().st_mode | 0o100)
    # Never hash or publish private keys. Record hashes of executable/image assets.
    receipts = {str(t.relative_to(base)): digest(t) for _, t in pairs if t.name != 'id_ed25519'}
    save(root / 'staged-assets.json', receipts)
    paths = [p for p in w.rglob('*') if p.suffix in ['.py', '.sh'] and '__pycache__' not in p.parts]
    save(root / 'source-manifest.json', {str(p): digest(p) for p in paths})
    print('ASSETS STAGED; VM launch remains explicit')


def operate(c, operation):
    base, w, h, root = layout(c)
    if read(base / 'site.json') != c or not (root / 'staged-assets.json').exists():
        raise RuntimeError('run prepare and stage-assets with the same configuration first')
    if operation == 'bootstrap':
        program = ('from outer import start,stop; from deploy import main; '
                   'from outer import ssh; start("bootstrap",16,0); main(); '
                   f'ssh("taskset -c 32-35 python3 {h}/preflight_l1.py",timeout=None,check=True); '
                   'stop("bootstrap")')
        subprocess.run([sys.executable, '-c', program], cwd=h, check=True)
        save(root / 'bootstrap-complete.json', {'complete': True})
    else:
        if not (root / 'bootstrap-complete.json').exists():
            raise RuntimeError('bootstrap is required before run')
        subprocess.run([sys.executable, str(h / 'pressure.py')], cwd=h, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['plan', 'prepare', 'stage-assets', 'bootstrap', 'run'])
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    c = config(args.config)
    if args.operation == 'plan':
        print(json.dumps(matrix(c), ensure_ascii=False, indent=2))
    elif args.operation == 'prepare':
        prepare(c)
    elif args.operation == 'stage-assets':
        stage_assets(c)
    else:
        operate(c, args.operation)


if __name__ == '__main__':
    main()
