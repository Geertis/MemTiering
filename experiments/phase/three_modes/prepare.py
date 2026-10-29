#!/usr/bin/env python3
"""Build a new, isolated protocol; keep all earlier artifacts immutable."""
import hashlib
import json
import shutil
from pathlib import Path

W = Path(__file__).resolve().parents[2]
H = Path(__file__).resolve().parent
A = W / 'phase/artifacts/vmm-v11-three-modes'
repo = W.parents[1] / 'sosp25-demeter'
assert not A.exists(), 'refuse artifact overwrite'
shutil.copytree(W / 'phase/artifacts/vmm-v10-system-tier', A)

def change(s, before, after, count=1):
    assert s.count(before) == count, (before, s.count(before))
    return s.replace(before, after)

s = (A / 'start-vm.sh').read_text()
s = change(s, 'exec sudo "$repo/hypervisor/',
           'exec sudo -n numactl --membind="${VMM_PRIVATE_NODE:?}" "$repo/hypervisor/')
(A / 'start-vm.sh').write_text(s)

s = (A / 'controller.py').read_text()
s = change(s, '    last_cpu: int = 0', '    startup_headroom_pages: int = 0\n    last_cpu: int = 0')
s = change(s, '        if not anon:\n            return 128 * MIB // PAGE',
    '''        # Search retains the conservative historical path. Other tools get
        # an explicit startup reservation before fork/exec, capped by the ledger.
        # This uses the current command class, never future trace measurements.
        floor = self.startup_headroom_pages if self.kind != 1 else 0
        if not anon:
            return max(floor, 128 * MIB // PAGE)''')
s = change(s, '        return max(16 * MIB // PAGE, anon[',
              '        return max(floor, 16 * MIB // PAGE, anon[')
s = change(s, '    parser.add_argument("--minimum-mib", type=int, default=16)',
    '    parser.add_argument("--minimum-mib", type=int, default=16)\n    parser.add_argument("--startup-headroom-mib", type=int, default=0)')
s = change(s, '    interval = args.interval_ms / 1000',
    '    if not 0 <= args.startup_headroom_mib <= args.budget_mib:\n        parser.error("startup headroom must fit the budget")\n    interval = args.interval_ms / 1000')
s = change(s, 'weight=float(c.get("weight", 1)), sensitivity=float(c.get("sensitivity", 1)))',
    'weight=float(c.get("weight", 1)), sensitivity=float(c.get("sensitivity", 1)),\n                    startup_headroom_pages=args.startup_headroom_mib * MIB // PAGE)')
(A / 'controller.py').write_text(s)

runner = (W / 'phase/vmm_opt/run_single.sh').read_text()
runner = change(runner, "GUEST_SYSTEM_TIER='${GUEST_SYSTEM_TIER:-fast}' HOST_FAST_NUMA_NODE=",
    "VMM_PRIVATE_NODE='${VMM_PRIVATE_NODE:?}' GUEST_SYSTEM_TIER='${GUEST_SYSTEM_TIER:-fast}' HOST_FAST_NUMA_NODE=")
runner = change(runner, '"${PYTEST_COMPAT:-0}" <<\'REMOTE_SETUP\'',
    '"${PYTEST_COMPAT:-0}" "${SB_FAST_NODE:-1}" "${SB_SLOW_NODE:-0}" "${SB_FUTURE_BENEFIT:-1}" "${STARTUP_HEADROOM_MIB:-0}" <<\'REMOTE_SETUP\'')
runner = change(runner, 'pytest_compat=${21}',
    'pytest_compat=${21}\nguest_fast_node=${22}\nguest_slow_node=${23}\nfuture_benefit=${24}\nstartup_headroom=${25}')
runner = change(runner, 'extra_module_args=(sandbox_sampling_enabled=',
    'extra_module_args=(sandbox_fast_node="$guest_fast_node" sandbox_slow_node="$guest_slow_node" sandbox_future_benefit="$future_benefit" sandbox_sampling_enabled=')
runner = change(runner, '  fast_node=${nodes[0]}\n  slow_node=${nodes[1]}',
    '  fast_node=$guest_fast_node\n  slow_node=$guest_slow_node\n  test -d "/sys/devices/system/node/node$fast_node"\n  test -d "/sys/devices/system/node/node$slow_node"')
runner = change(runner, '--budget-mib "$phase_budget_mib" --fast-node',
    '--budget-mib "$phase_budget_mib" --startup-headroom-mib "$startup_headroom" --fast-node')
runner = runner.replace('phase/baselines/host_monitor.py', 'phase/three_modes/host_monitor.py')
runner = change(runner, '--expected-reporting "${FREE_PAGE_REPORTING:-off}" --expected-order "$reporting_order" --ready',
    '--expected-zones "${HOST_FAST_NUMA_NODE:?},${HOST_SLOW_NUMA_NODE:?}" --expected-reporting "${FREE_PAGE_REPORTING:-off}" --expected-order "$reporting_order" --ready')
runner = change(runner, "manifest['system_tier']=", "manifest['guest_tier_nodes']={'fast':int(os.environ.get('SB_FAST_NODE','1')),'slow':int(os.environ.get('SB_SLOW_NODE','0'))}\nmanifest['vmm_private_node']=int(os.environ['VMM_PRIVATE_NODE'])\nmanifest['future_benefit']=int(os.environ.get('SB_FUTURE_BENEFIT','1'))\nmanifest['startup_headroom_mib']=int(os.environ.get('STARTUP_HEADROOM_MIB','0'))\nmanifest['system_tier']=")
(H / 'run_single.sh').write_text(runner)

monitor = (W / 'phase/baselines/host_monitor.py').read_text()
monitor = change(monitor, 'a = p.parse_args()',
    "p.add_argument('--expected-zones', required=True)\na = p.parse_args()\nexpected_zones = [int(x) for x in a.expected_zones.split(',')]\nassert len(expected_zones) == 2")
start, end = monitor.index('def rss(pid):'), monitor.index('def balloon_actual(')
monitor = monitor[:start] + '''def rss(pid):
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

''' + monitor[end:]
monitor = change(monitor, "zones = info['config']['memory']['zones']",
    "zones = info['config']['memory']['zones']\nassert [next(z['host_numa_node'] for z in zones if z['id']==k) for k in ['dram','slow']] == expected_zones, 'actual RAM zone mapping mismatch'")
monitor = change(monitor, '            nodes = rss(pid)', '            nodes, breakdown = rss(pid)')
monitor = change(monitor, "emit('sample', rss_node_bytes=nodes, guest=guest, guest_fresh=fresh,",
    "emit('sample', rss_node_bytes=nodes, rss_breakdown=breakdown, guest=guest, guest_fresh=fresh,")
(H / 'host_monitor.py').write_text(monitor)

for name, src in [('PhaseTieing_placement.ko', repo / 'build/demeter/mm/PhaseTieing/PhaseTieing_placement.ko'),
                  ('sandbox.c', repo / 'kernel/demeter/mm/PhaseTieing/sandbox.c')]:
    shutil.copy2(src, A / name)
(A / 'SHA256SUMS.json').write_text(json.dumps({p.name: hashlib.sha256(p.read_bytes()).hexdigest()
    for p in A.iterdir() if p.is_file() and p.name != 'SHA256SUMS.json'}, indent=2) + '\n')
print(A)
