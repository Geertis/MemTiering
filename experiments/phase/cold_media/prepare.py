#!/usr/bin/env python3
"""Create new artifacts and harness; never edit a prior experiment snapshot."""
import hashlib
import json
from pathlib import Path
import shutil

H = Path(__file__).resolve().parent
W = H.parents[1]
FINAL = W / 'phase/artifacts/vmm-v25-cold-media-e2e'
A = W / 'phase/artifacts/.preparing-v25-cold-media-e2e'
OLD = W / 'phase/artifacts/vmm-v24-reporting-barrier'


def replace(text, old, new, count=1):
    if text.count(old) != count:
        raise ValueError(f'patch anchor changed ({text.count(old)} != {count}): {old[:90]}')
    return text.replace(old, new)


def main():
    if A.exists() or FINAL.exists():
        raise SystemExit('artifact already exists; retain it and use a new revision for changes')
    shutil.copytree(OLD, A, ignore=shutil.ignore_patterns('__pycache__', 'SHA256SUMS.json'))
    shutil.copy2(A / 'controller.py', A / 'cxl_controller.py')
    for name in ['controller.py', 'ssd_controller.py']:
        shutil.copy2(H / name, A / name)
    p = A / 'replay_agentcgroup.py'
    s = p.read_text()
    s = replace(s, '        finally:\n            if self.phase_runtime:',
                '            self.e2e_finished_mono = time.monotonic()\n'
                '        finally:\n            if self.phase_runtime:')
    s = replace(s, '            "elapsed_s": time.monotonic() - self.started_mono,',
                '            "elapsed_s": time.monotonic() - self.started_mono,\n'
                '            "e2e_started_monotonic_s": self.started_mono,\n'
                '            "e2e_finished_monotonic_s": self.e2e_finished_mono,\n'
                '            "e2e_s": self.e2e_finished_mono - self.started_mono,\n'
                '            "e2e_scope": "sandbox trace replay: initial recorded offset, serial tool service and inter-tool waits; excludes VM boot, model execution and response generation",\n'
                '            "e2e_wait_protocol": "completion-to-next-start gaps" if self.args.timing == "gaps" else "absolute starts",')
    p.write_text(s)

    p = A / 'start-vm.sh'
    s = p.read_text()
    s = replace(s, 'extra_workload_disks=()',
                'extra_workload_disks=()\n'
                'if [[ -n ${SSD_SWAP_DISK:-} ]]; then\n'
                '  test -f "$SSD_SWAP_DISK"\n'
                '  extra_workload_disks+=("path=$SSD_SWAP_DISK,direct=on,id=ssd-swap")\n'
                'fi')
    p.write_text(s)

    # The mature V24 runner is retained as the common boot/reporting path.
    # This new copy adds SSD setup without altering its CXL control policy.
    s = (W / 'phase/reporting_barrier/run_single.sh').read_text()
    s = s.replace('set -euo pipefail\n',
                'set -euo pipefail\ncold_media=${COLD_MEDIA:-cxl}\n'
                '[[ "$cold_media" =~ ^(cxl|ssd)$ ]] || exit 2\n'
                '[[ "${WAIT_REPORTING_ORDER:--1}" == -1 ]] || { echo "this media comparison uses fixed reporting order" >&2; exit 2; }\n', 1)
    s = s.replace('/phase/reporting_barrier/', '/phase/cold_media/')
    s = s.replace('phase/artifacts/lookahead-v7', 'phase/artifacts/vmm-v25-cold-media-e2e')
    s = replace(s, "manifest['baseline_policy']=os.environ['BASELINE_POLICY']",
                "manifest['cold_media']=os.environ.get('COLD_MEDIA','cxl')\n"
                "manifest['ssd_wait_mib']=int(os.environ.get('SSD_WAIT_MIB','16'))\n"
                "manifest['ssd_chunk_mib']=int(os.environ.get('SSD_CHUNK_MIB','8'))\n"
                "manifest['replay_timing']=os.environ.get('REPLAY_TIMING','gaps')\n"
                "manifest['baseline_policy']=os.environ['BASELINE_POLICY']")
    s = replace(s, "for path in extras:\n", "extras += [Path(files[0]).parent / n for n in ['cxl_controller.py','ssd_controller.py']]\nfor path in extras:\n")
    s = replace(s, 'root_scratch="$here/.scratch-root-$(basename "$result_dir")-$$.qcow2"',
                'root_scratch="$here/.scratch-root-$(basename "$result_dir")-$$.qcow2"\n'
                'swap_scratch=\n'
                'if [[ "$cold_media" == ssd ]]; then\n'
                '  swap_scratch="$here/.scratch-swap-$(basename "$result_dir")-$$.raw"\n'
                '  fallocate -l 16G "$swap_scratch"\n'
                '  chmod 600 "$swap_scratch"\n'
                '  mkswap -L agent-ssd "$swap_scratch" >"$result_dir/swap-format.log"\n'
                '  findmnt -T "$swap_scratch" -J >"$result_dir/ssd-filesystem.json"\n'
                '  lsblk -J -o NAME,TYPE,SIZE,ROTA,MOUNTPOINTS >"$result_dir/ssd-block-topology.json"\n'
                'fi')
    s = replace(s, '  rm -f -- "$root_scratch"',
                '  rm -f -- "$root_scratch"\n  if [[ -n "$swap_scratch" ]]; then rm -f -- "$swap_scratch"; fi')
    s = replace(s, '"VMM_PRIVATE_NODE=', '"SSD_SWAP_DISK=\'$swap_scratch\' VMM_PRIVATE_NODE=')
    s = replace(s, '--zones "${HOST_FAST_NUMA_NODE:?},${HOST_SLOW_NUMA_NODE:?}" --phase "$phase_mode"',
                '--zones "${HOST_FAST_NUMA_NODE:?},${HOST_SLOW_NUMA_NODE:?}" --phase "$((phase_mode && cold_media != "ssd"))"')
    # Bash arithmetic cannot compare medium strings. Compute the flag explicitly.
    s = replace(s, 'sudo -n python3 "$here/phase/three_modes/verify_topology.py" --api',
                'topology_phase=$phase_mode\n[[ "$cold_media" == ssd ]] && topology_phase=0\n'
                'sudo -n python3 "$here/phase/three_modes/verify_topology.py" --api')
    s = replace(s, '"$((phase_mode && cold_media != "ssd"))"', '"$topology_phase"')
    s = replace(s, '    "$placement_module" "$controller_driver" "$artifact_dir/collect.py"',
                '    "$placement_module" "$controller_driver" "$artifact_dir/cxl_controller.py" "$artifact_dir/ssd_controller.py" "$artifact_dir/collect.py"')
    s = replace(s, '"${BARRIER_REPORTING_START:-0}" <<\'REMOTE_SETUP\'',
                '"${BARRIER_REPORTING_START:-0}" "$cold_media" "${SSD_WAIT_MIB:-16}" "${SSD_CHUNK_MIB:-8}" <<\'REMOTE_SETUP\'')
    s = replace(s, 'barrier_reporting_start=${32}',
                'barrier_reporting_start=${32}\ncold_media=${33}\nssd_wait_mib=${34}\nssd_chunk_mib=${35}')
    s = replace(s, 'sudo swapoff -a',
                'sudo swapoff -a\n'
                'if [[ "$cold_media" == ssd ]]; then\n'
                '  test "$(sudo blkid -s LABEL -o value /dev/vdc)" = agent-ssd\n'
                '  if [[ -e /sys/module/zswap/parameters/enabled ]]; then echo N | sudo tee /sys/module/zswap/parameters/enabled; fi\n'
                '  sudo swapon /dev/vdc\n'
                '  sudo sysctl -w vm.swappiness=100 vm.page-cluster=0\n'
                '  cat /proc/swaps\n'
                'fi')
    s = replace(s, 'if [[ "$method" == demeter ]]; then\n  extra_module_args=',
                'if [[ "$method" == demeter && "$cold_media" == cxl ]]; then\n  extra_module_args=')
    s = replace(s, 'if [[ "$method" == demeter ]]; then\n  test -d /sys/module/PhaseTieing_placement',
                'if [[ "$method" == demeter && "$cold_media" == cxl ]]; then\n  test -d /sys/module/PhaseTieing_placement')
    s = replace(s, "  sudo sh -c 'echo 0 > /sys/kernel/mm/demeter/targets/nr_targets'",
                "  if [[ \"$cold_media\" == cxl ]]; then sudo sh -c 'echo 0 > /sys/kernel/mm/demeter/targets/nr_targets'; fi")
    s = replace(s, '  sudo python3 /tmp/controller.py --config',
                '  media_flags=(--cold-media "$cold_media")\n'
                '  if [[ "$cold_media" == ssd ]]; then media_flags+=(--wait-mib "$ssd_wait_mib" --chunk-mib "$ssd_chunk_mib"); fi\n'
                '  sudo python3 /tmp/controller.py "${media_flags[@]}" --config')
    s = replace(s, '  sudo python3 /tmp/collect.py --output',
                '  if [[ "$cold_media" == cxl ]]; then\n  sudo python3 /tmp/collect.py --output')
    s = replace(s, '  printf \'%s\\n\' "$slow_node" | sudo tee /mnt/workload/tmp/phase-slow-node >/dev/null',
                '  fi\n  fallback_node=$slow_node\n  [[ "$cold_media" == ssd ]] && fallback_node=$fast_node\n'
                '  printf \'%s\\n\' "$fallback_node" | sudo tee /mnt/workload/tmp/phase-slow-node >/dev/null')
    s = replace(s, 'if [[ "$method" == demeter ]]; then\n  grep -H .',
                'if [[ "$method" == demeter && "$cold_media" == cxl ]]; then\n  grep -H .')
    s = replace(s, 'if [[ "$policy" == phase ]]; then\nsudo touch /tmp/diag-stop',
                'if [[ "$policy" == phase && "${COLD_MEDIA:-cxl}" == cxl ]]; then\nsudo touch /tmp/diag-stop')
    # Pass medium into the run heredoc as an argument, not a remote inherited env.
    s = replace(s, '"${REPLAY_TIMING:-original}" <<\'REMOTE_RUN\'',
                '"${REPLAY_TIMING:-original}" "$cold_media" <<\'REMOTE_RUN\'')
    s = replace(s, 'replay_timing=${11}', 'replay_timing=${11}\nCOLD_MEDIA=${12}')
    (H / 'run_single.sh').write_text(s)
    (H / 'run_single.sh').chmod(0o755)

    s = (W / 'phase/reporting_barrier/guest_state.py').read_text()
    s = replace(s, "'vmstat': pairs('/proc/vmstat'),",
                "'vmstat': pairs('/proc/vmstat'),\n"
                "                          'memory_stat': pairs('/sys/fs/cgroup/agent-workload/memory.stat'),\n"
                "                          'memory_events': pairs('/sys/fs/cgroup/agent-workload/memory.events'),\n"
                "                          'io_stat': Path('/sys/fs/cgroup/agent-workload/io.stat').read_text() if Path('/sys/fs/cgroup/agent-workload/io.stat').exists() else None,\n"
                "                          'swap_bytes': int(Path('/sys/fs/cgroup/agent-workload/memory.swap.current').read_text()),\n"
                "                          'memory_high': Path('/sys/fs/cgroup/agent-workload/memory.high').read_text().strip(),\n"
                "                          'memory_max': Path('/sys/fs/cgroup/agent-workload/memory.max').read_text().strip(),\n"
                "                          'swap_max': Path('/sys/fs/cgroup/agent-workload/memory.swap.max').read_text().strip(),\n"
                "                          'swaps': Path('/proc/swaps').read_text(),\n"
                "                          'diskstats': Path('/proc/diskstats').read_text(),\n"
                "                          'psi': Path('/sys/fs/cgroup/agent-workload/memory.pressure').read_text(),")
    (H / 'guest_state.py').write_text(s)
    s = (W / 'phase/reporting_barrier/host_monitor.py').read_text()
    s = replace(s, '    for cpu in range(4):',
                "    actual_cpus = sorted({c for x in info['config']['cpus']['affinity'] for c in x['host_cpus']})\n    for cpu in actual_cpus:")
    s = replace(s, "s[0] in ['cpu0','cpu1','cpu2','cpu3']", "s[0] in ['cpu'+str(c) for c in actual_cpus]")
    (H / 'host_monitor.py').write_text(s)
    manifest = {x.name: hashlib.sha256(x.read_bytes()).hexdigest()
                for x in A.iterdir() if x.is_file()}
    (A / 'SHA256SUMS.json').write_text(json.dumps(manifest, indent=2) + '\n')
    A.rename(FINAL)
    print(FINAL)


if __name__ == '__main__':
    main()
