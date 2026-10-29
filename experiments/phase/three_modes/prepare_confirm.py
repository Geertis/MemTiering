#!/usr/bin/env python3
"""Freeze a separate confirmation protocol without touching an active pilot."""
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

W=Path(__file__).resolve().parents[2]
H=Path(__file__).resolve().parent
A=W/'phase/artifacts/vmm-v12-confirm'
assert not A.exists()
shutil.copytree(W/'phase/artifacts/vmm-v11-three-modes',A)

def change(s,old,new):
    assert s.count(old)==1,(old,s.count(old))
    return s.replace(old,new)

s=(A/'replay_agentcgroup.py').read_text()
s=change(s,'{"dram":(2,1),"cxl":(2,2),"static":(6,3)}',
           '{"dram":(2,2),"cxl":(2,1),"static":(6,3)}')
s=change(s, 'choices=("original", "none")', 'choices=("original", "gaps", "none")')
s=change(s, '        actual_offset = time.monotonic() - self.started_mono',
    '''        if self.args.timing == "gaps":
            previous = next((c for c in reversed(self.calls[:index]) if c["tool"] in REPLAYABLE), None)
            previous_end = parse_timestamp(previous.get("end_timestamp") or previous["timestamp"]) if previous else self.original_start
            gap = max(0, parse_timestamp(call["timestamp"]) - previous_end) / self.args.speed
            if gap:
                time.sleep(gap)
        actual_offset = time.monotonic() - self.started_mono''')
(A/'replay_agentcgroup.py').write_text(s)
policy=(W/'phase/baselines/policy_exec.c').read_text()
policy=change(policy,'"dram")) { mode = MPOL_BIND; mask = 1;',
                     '"dram")) { mode = MPOL_BIND; mask = 2;')
policy=change(policy,'"cxl")) { mode = MPOL_BIND; mask = 2;',
                     '"cxl")) { mode = MPOL_BIND; mask = 1;')
(A/'policy_exec.c').write_text(policy)
subprocess.run(['cc','-static','-O2','-Wall','-Wextra',str(A/'policy_exec.c'),'-o',str(A/'policy_exec')],check=True)

s=(H/'run_single.sh').read_text()
s=change(s,'set +e\n"${ssh[@]}" \'bash -s\' --',
    '''sudo -n python3 "$here/phase/three_modes/relocate_private.py" --api "$api_socket" \\
  --node "${VMM_PRIVATE_NODE:?}" --output "$result_dir/vmm-private-placement.json" \\
  >"$result_dir/vmm-private-placement.log" 2>"$result_dir/vmm-private-placement.err"
set +e
"${ssh[@]}" 'bash -s' --''')
s=change(s,'"$policy" <<\'REMOTE_RUN\'', '"$policy" "${REPLAY_TIMING:-original}" <<\'REMOTE_RUN\'')
s=change(s,'policy=${10}\ncgroup=', 'policy=${10}\nreplay_timing=${11}\ncgroup=')
s=change(s,'--timing original --speed "$speed"', '--timing "$replay_timing" --speed "$speed"')
# Archive the private-page setup helper in every new run as well as the matrix.
s=change(s,"extras=[Path(files[0]).parent / 'start-vm.sh',",
    "extras=[Path('/r0/home/geerties/HW/Demeter/experiments/agentcgroup-demeter/phase/three_modes/relocate_private.py'), Path(files[0]).parent / 'start-vm.sh',")
(H/'run_confirm_single.sh').write_text(s)

s=(H/'run_matrix.py').read_text()
s=change(s,"A = W / 'phase/artifacts/vmm-v11-three-modes'", "A = W / 'phase/artifacts/vmm-v12-confirm'")
s=change(s,"'dram': dict(mode='dram', policy='dram', zones=[0, 0], private=0)",
           "'dram': dict(mode='dram', policy='dram', zones=[0, 0], private=0, bind_node=1)")
s=change(s,"'cxl': dict(mode='cxl', policy='dram', zones=[2, 2], private=2)",
           "'cxl': dict(mode='cxl', policy='dram', zones=[2, 2], private=2, bind_node=1)")
s=change(s,"p.add_argument('--speed', type=float, default=10)",
    "p.add_argument('--speed', type=float, default=10)\n    p.add_argument('--timing', choices=['original','gaps'], default='original')")
s=change(s,"for c in cases:\n        for n, expected", "for c in cases:\n        assert sha(Path(c['golden'])) == c['golden_sha256'], 'workload image drift'\n        for n, expected")
s=change(s,"['run_matrix.py', 'run_single.sh', 'host_monitor.py']", "['run_confirm_matrix.py', 'run_confirm_single.sh', 'host_monitor.py', 'relocate_private.py']")
s=change(s,"protocol = dict(version=11,", "protocol = dict(version=12, replay_timing=a.timing,")
s=change(s,'guest_cpu_memory_node=0','guest_cpu_memory_node=1')
s=change(s,"'GUEST_SYSTEM_TIER': 'fast'", "'GUEST_SYSTEM_TIER': 'slow'")
s=change(s,"'BIND_TOOL_NODE': '-1',", "'REPLAY_TIMING': a.timing, 'BIND_TOOL_NODE': '-1',")
s=change(s,"str(H / 'run_single.sh')", "str(H / 'run_confirm_single.sh')")
s=change(s,"timing='original absolute start offsets divided by speed; execution unscaled; serial tools; no simulated parallel VM',",
    "timing='gaps: preserve recorded previous replayed finish to next start, divided by speed; original: absolute start offsets/speed; execution unscaled; serial tools',")
(H/'run_confirm_matrix.py').write_text(s)
(A/'SHA256SUMS.json').write_text(json.dumps({p.name:hashlib.sha256(p.read_bytes()).hexdigest()
    for p in A.iterdir() if p.is_file() and p.name!='SHA256SUMS.json'},indent=2)+'\n')
print(A)
