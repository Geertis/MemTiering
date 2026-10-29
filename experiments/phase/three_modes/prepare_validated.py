#!/usr/bin/env python3
"""Correct Linux/PXM numbering and common legacy pytest compatibility."""
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

W=Path(__file__).resolve().parents[2]
H=Path(__file__).resolve().parent
A=W/'phase/artifacts/vmm-v13-validated'
assert not A.exists()
shutil.copytree(W/'phase/artifacts/vmm-v12-confirm',A)

def change(s,old,new):
    assert s.count(old)==1,(old,s.count(old))
    return s.replace(old,new)

s=(A/'replay_agentcgroup.py').read_text()
s=change(s,'{"dram":(2,2),"cxl":(2,1),"static":(6,3)}',
           '{"dram":(2,1),"cxl":(2,2),"static":(6,3)}')
(A/'replay_agentcgroup.py').write_text(s)
shutil.copy2(W/'phase/baselines/policy_exec.c',A/'policy_exec.c')
subprocess.run(['cc','-static','-O2','-Wall','-Wextra',str(A/'policy_exec.c'),'-o',str(A/'policy_exec')],check=True)

s=(A/'vmm_pytest_compat.py').read_text()
s=change(s,'from _pytest.nodes import NodeMeta',
    'from pathlib import Path\nimport pytest\nfrom _pytest.recwarn import WarningsRecorder\nfrom _pytest.nodes import NodeMeta')
s=change(s,'return cls._create(fspath=args[0], parent=args[1], **kwargs)',
           'return cls._create(path=Path(str(args[0])), parent=args[1], **kwargs)')
s+='''
# Legacy pytest.warns(None) records warnings without requiring one. Keep that
# meaning while leaving all real Warning-category checks and test assertions.
_original_warns = pytest.warns
def _legacy_warns(expected_warning=Warning, *args, **kwargs):
    if expected_warning is not None:
        return _original_warns(expected_warning, *args, **kwargs)
    recorder = WarningsRecorder(_ispytest=True)
    if not args:
        kwargs.pop("match", None)
        if kwargs:
            raise TypeError("unexpected legacy warns keyword arguments")
        return recorder
    if not callable(args[0]):
        raise TypeError("legacy warns callable is not callable")
    with recorder:
        return args[0](*args[1:], **kwargs)
pytest.warns = _legacy_warns
'''
(A/'vmm_pytest_compat.py').write_text(s)

s=(H/'run_confirm_matrix.py').read_text()
s=change(s,"A = W / 'phase/artifacts/vmm-v12-confirm'", "A = W / 'phase/artifacts/vmm-v13-validated'")
s=s.replace('bind_node=1','bind_node=0')
s=change(s,"benefit=int(variant != 'heat'), events=int(variant != 'reactive'))",
    "benefit=int(variant != 'heat'), events=int(variant != 'reactive'), fast_node=0, slow_node=1)")
s=change(s,"default=['dram', 'cxl', 'full2048']", "default=['dram', 'cxl', 'full4096']")
s=change(s,"['run_confirm_matrix.py', 'run_confirm_single.sh', 'host_monitor.py', 'relocate_private.py']",
    "['run_validated_matrix.py', 'run_validated_single.sh', 'host_monitor.py', 'relocate_private.py', 'verify_topology.py']")
s=change(s,"str(H / 'run_confirm_single.sh')", "str(H / 'run_validated_single.sh')")
s=change(s,'version=12','version=13')
s=change(s,'guest_cpu_memory_node=1','guest_cpu_memory_node=0, guest_cpu_acpi_proximity=2, guest_fast_node=0, guest_slow_node=1')
s=change(s,"'SB_FAST_NODE': '1', 'SB_SLOW_NODE': '0'", "'SB_FAST_NODE': '0', 'SB_SLOW_NODE': '1'")
s=change(s,"int(c['name'].startswith('xonsh'))", "int(c['name'].startswith('xonsh') or c['name']=='dask-5510')")
s=change(s,"'ours':'boot zone Guest0 CXL2, tool arena Guest1 DDR0, VMM allocation policy CXL2; phase quota and future-benefit placement'",
    "'ours':'PXM1 boot zone maps Linux Guest1/CXL2; PXM2 CPU/tool arena maps Linux Guest0/DDR0; VMM private CXL2; phase quota and future-benefit placement'")
s=change(s,"compatibility='same immutable workload images; optional Xonsh collector shim applied identically; expected original failures retained'",
    "compatibility='same immutable workload images; Xonsh pathlib collector shim and Dask legacy warning-recording shim applied to every mode; assertions and recorded commands unchanged; other original failures retained'")
(H/'run_validated_matrix.py').write_text(s)
s=(H/'run_confirm_single.sh').read_text()
s=change(s, 'if [[ "$pytest_compat" == 1 ]]; then\n  sudo install',
    'if [[ "$pytest_compat" == 1 ]]; then\n  sudo mkdir -p /mnt/workload/opt/replay-deps\n  sudo install')
s=change(s, '"${ssh[@]}" "echo \'$reporting_order\' | sudo tee',
    '''sudo -n python3 "$here/phase/three_modes/verify_topology.py" --api "$api_socket" \\
  --boot-log "$vm_log" --output "$result_dir/guest-tier-map.json" \\
  --zones "${HOST_FAST_NUMA_NODE:?},${HOST_SLOW_NUMA_NODE:?}" --phase "$phase_mode" \\
  --fast-node "${SB_FAST_NODE:?}" --slow-node "${SB_SLOW_NODE:?}"
"${ssh[@]}" "echo '$reporting_order' | sudo tee''')
s=change(s,"extras=[Path('/r0/home/geerties/HW/Demeter/experiments/agentcgroup-demeter/phase/three_modes/relocate_private.py'),",
    "extras=[Path('/r0/home/geerties/HW/Demeter/experiments/agentcgroup-demeter/phase/three_modes/verify_topology.py'), Path('/r0/home/geerties/HW/Demeter/experiments/agentcgroup-demeter/phase/three_modes/relocate_private.py'),")
(H/'run_validated_single.sh').write_text(s)
(A/'SHA256SUMS.json').write_text(json.dumps({p.name:hashlib.sha256(p.read_bytes()).hexdigest()
    for p in A.iterdir() if p.is_file() and p.name!='SHA256SUMS.json'},indent=2)+'\n')
print(A)
