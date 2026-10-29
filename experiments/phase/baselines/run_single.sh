#!/usr/bin/env bash
set -euo pipefail
policy=${BASELINE_POLICY:?set BASELINE_POLICY to dram, cxl, static, or phase}
[[ "$policy" =~ ^(dram|cxl|static|phase)$ ]] || exit 2
static_fast_weight=${STATIC_FAST_WEIGHT:-1}
static_slow_weight=${STATIC_SLOW_WEIGHT:-4}
[[ "$static_fast_weight" =~ ^[1-9][0-9]*$ && "$static_slow_weight" =~ ^[1-9][0-9]*$ ]] || exit 2
reporting_order=${PAGE_REPORTING_ORDER:-9}
[[ "$reporting_order" =~ ^(5|9)$ ]] || exit 2

usage() {
  echo "usage: $0 METHOD GOLDEN_EXT4 TRACE_DIR RESULT_DIR FAST_MIB SLOW_MIB [SEED] [TRIGGER_BYTES] [SPEED]" >&2
  echo "METHOD: phase (diagnostics require the new Sandbox module)" >&2
  exit 2
}

method=${1:-}
golden=${2:-}
trace_dir=${3:-}
result_dir=${4:-}
fast_mib=${5:-}
slow_mib=${6:-}
seed=${7:-0}
trigger_bytes=${8:-0}
speed=${9:-1}
guest_cpu_with_fast=${GUEST_CPU_WITH_FAST:-1}
root_disk=${ROOT_DISK:-}
replay_timeout_s=${REPLAY_TIMEOUT_S:-0}
demeter_agent_mode=${DEMETER_AGENT_MODE:-1}
phase_budget_mib=${PHASE_BUDGET_MIB:-256}
phase_no_events=${PHASE_NO_EVENTS:-0}
sb_alloc=${SB_ALLOCATION:-1}
sb_batch=${SB_SAMPLE_BATCH:-1}
sb_wakeup=${SB_SAMPLE_WAKEUP:-1}
sb_placement=${SB_PLACEMENT:-1}
sb_lifecycle=${SB_LIFECYCLE_WAKEUP:-0}
sb_wake_us=${SB_SAMPLE_WAKE_US:-2000}
sb_load_period=${SB_LOAD_PERIOD:-65535}
[[ "$sb_alloc" =~ ^[01]$ && "$sb_batch" =~ ^[01]$ ]] || exit 2
sb_cross_mm=${SB_CROSS_MM:-1}
sb_file_demotion=${SB_FILE_DEMOTION:-1}
[[ "$sb_cross_mm" =~ ^[01]$ && "$sb_file_demotion" =~ ^[01]$ ]] || exit 2
phase_mode=0
[[ "$policy" == phase ]] && phase_mode=1
demeter_exch_thresh=${DEMETER_EXCH_THRESH:-33554432}
demeter_initial_range_mb=${DEMETER_INITIAL_RANGE_MB:-0}
lane=${VM_LANE:?set VM_LANE to an unused lane from 0 to 3}
[[ "$lane" =~ ^[0-7]$ ]] || { echo "VM_LANE must be from 0 to 3" >&2; exit 2; }
if [[ -e "/tmp/PhaseTieing-agent-lane${lane}.lock" ]]; then exec 9<>"/tmp/PhaseTieing-agent-lane${lane}.lock"; else exec 9>"/tmp/PhaseTieing-agent-lane${lane}.lock"; fi
flock -n 9 || { echo "VM lane $lane is busy" >&2; exit 1; }
subnet=$((92 + lane))
guest_ip="192.168.$subnet.100"
host_ip="192.168.$subnet.1/24"
tap="demeter$lane"
mac="2e:89:a8:e4:92:0$lane"
host_cpu_start=$((4 * lane))
[[ "$method" == phase ]] || usage
# "noop" is the static placement control.  Keep "random" as its existing
# matrix name, while giving direct noop invocations the same semantics.
requested_method=$method
method=baseline
[[ "$policy" == phase ]] && method=demeter
if [[ "$method" == noop ]]; then
  method=random
  [[ "$trigger_bytes" != 0 ]] || trigger_bytes=1
fi
[[ -f "$golden" && -f "$trace_dir/tool_calls.json"  ]] || usage
[[ "$fast_mib" =~ ^[1-9][0-9]*$ && "$slow_mib" =~ ^[1-9][0-9]*$ ]] || usage
[[ "$seed" =~ ^[0-9]+$ && "$trigger_bytes" =~ ^[0-9]+$ ]] || usage
result_dir=$(realpath -m -- "$result_dir")
[[ ! -e "$result_dir" ]] || { echo "refusing to overwrite: $result_dir" >&2; exit 1; }
if [[ "$method" == random && "$trigger_bytes" == 0 ]]; then
  echo "random requires a non-zero TRIGGER_BYTES" >&2
  exit 2
fi

base=/r0/home/geerties/HW/Demeter
repo=$base/sosp25-demeter
here=$base/experiments/agentcgroup-demeter
vm_scripts=$base/experiments/swebench/swesmith
key=$base/experiments/physical-smoke/id_ed25519
ssh=(ssh -i "$key" -o StrictHostKeyChecking=no -o ConnectTimeout=5 "clear@$guest_ip")
scp=(scp -i "$key" -o StrictHostKeyChecking=no)
vm_method=demeter
artifact_dir=${PHASE_ARTIFACT_DIR:-$here/phase/artifacts/lookahead-v7}
placement_module="$artifact_dir/PhaseTieing_placement.ko"
replay_driver="$artifact_dir/replay_agentcgroup.py"
controller_driver="$artifact_dir/controller.py"
kernel_image="$artifact_dir/vmlinux.bin"
if [[ "$requested_method" == native ]]; then
  placement_module="$base/baselines/20261003-current/bin/PhaseTieing_placement.ko"
  kernel_image="$base/baselines/20261003-current/bin/vmlinux.bin"
fi
[[ "$method" == random ]] && vm_method=noop
if [[ -n "$root_disk" ]]; then
  [[ -f "$root_disk" ]] || { echo "missing ROOT_DISK: $root_disk" >&2; exit 2; }
  root_disk=$(realpath -- "$root_disk")
fi
kernel=demeter
[[ "$method" == tpp ]] && kernel=tpp

mkdir -p "$(dirname "$result_dir")"
mkdir "$result_dir"
printf '%s\n' "$requested_method" >"$result_dir/requested-method"
python3 - "$result_dir" "$requested_method" "$trace_dir" "$fast_mib" "$slow_mib" "$phase_budget_mib" "$phase_no_events" "$placement_module" "$kernel_image" "$replay_driver" "$controller_driver" "$artifact_dir/collect.py" "$0" "$artifact_dir/PhaseTieing_balloon.ko" "$artifact_dir/rg" <<'MANIFEST'
import hashlib,json,sys,os,subprocess
from pathlib import Path
out,method,trace,fast,slow,budget,no_events,*files=sys.argv[1:]
manifest={'method':method,'host_kernel':subprocess.check_output(['uname','-r'],text=True).strip(),'trace':trace,'fast_mib':int(fast),'slow_mib':int(slow),
          'requested_cross_mm':int(os.environ.get('SB_CROSS_MM','1')), 'requested_file_demotion':int(os.environ.get('SB_FILE_DEMOTION','1')),
          'artifact':Path(files[0]).parent.name, 'load_event':33232,'load_period':int(os.environ.get('SB_LOAD_PERIOD','65535')),'store_period':65535, 'allocation_enabled':int(os.environ.get('SB_ALLOCATION','1')), 'sample_batch':int(os.environ.get('SB_SAMPLE_BATCH','1')), 'sample_wakeup':int(os.environ.get('SB_SAMPLE_WAKEUP','1')), 'placement_enabled':int(os.environ.get('SB_PLACEMENT','1')), 'lifecycle_wakeup':int(os.environ.get('SB_LIFECYCLE_WAKEUP','0')), 'sample_wake_us':int(os.environ.get('SB_SAMPLE_WAKE_US','2000')), 'search_backend':os.environ.get('REPLAY_SEARCH_BACKEND','ripgrep'), 'phase_counters':int(os.environ.get('PHASE_COUNTERS','0')), 'bind_tool_node':os.environ.get('BIND_TOOL_NODE'),
          'budget_mib':int(budget),'without_runtime_events':no_events=='1',
          'files':{str(p):hashlib.sha256(Path(p).read_bytes()).hexdigest()
                   for p in files+[str(Path(trace)/'tool_calls.json'),*([str(Path(trace)/'trace.jsonl')] if (Path(trace)/'trace.jsonl').exists() else [])]}}
archive=Path(out)/'protocol-inputs';archive.mkdir()
import shutil
for name in files:
 shutil.copy2(name,archive/Path(name).name)

extras=[Path(files[0]).parent / 'start-vm.sh', Path('/r0/home/geerties/HW/Demeter/experiments/agentcgroup-demeter/phase/baselines/host_monitor.py'), Path('/r0/home/geerties/HW/Demeter/experiments/agentcgroup-demeter/phase/baselines/guest_state.py')]
for path in extras:
 manifest['files'][str(path)]=hashlib.sha256(path.read_bytes()).hexdigest()
 shutil.copy2(path,archive/path.name)
p=Path('/r0/home/geerties/HW/Demeter/experiments/agentcgroup-demeter/phase/baselines/startup_monitor.py')
manifest['files'][str(p)]=hashlib.sha256(p.read_bytes()).hexdigest()
shutil.copy2(p,archive/p.name)
p=Path(files[0]).parent/'policy_exec'
manifest['files'][str(p)]=hashlib.sha256(p.read_bytes()).hexdigest()
shutil.copy2(p,archive/p.name)
manifest['baseline_policy']=os.environ['BASELINE_POLICY']
manifest['static_weights']=[int(os.environ.get('STATIC_FAST_WEIGHT','1')),int(os.environ.get('STATIC_SLOW_WEIGHT','4'))]
manifest['page_reporting_order']=int(__import__('os').environ.get('PAGE_REPORTING_ORDER','9'))
manifest['capacity_policy']=__import__('os').environ.get('CAPACITY_POLICY','static')
manifest['free_page_reporting']=__import__('os').environ.get('FREE_PAGE_REPORTING','off')
manifest['raw_zones_mib']=[int(__import__('os').environ.get(k,'16384')) for k in ['FAST_RAW_MIB','SLOW_RAW_MIB']]
(Path(out)/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
MANIFEST
scratch="$here/.scratch-$(basename "$result_dir")-$$.ext4"
root_scratch="$here/.scratch-root-$(basename "$result_dir")-$$.qcow2"
session="agentcg-${method}-$$"
api_socket="$vm_scripts/cloud-hypervisor-$vm_method-lane$lane.sock"
vm_log="$result_dir/vm.log"
vm_started=0
dhcp_started=0
monitor_pid=
startup_monitor_pid=

cleanup() {
  if [[ -n "$startup_monitor_pid" ]]; then
    touch "$result_dir/startup-stop"
    wait "$startup_monitor_pid" || true
  fi
  if [[ -n "$monitor_pid" ]]; then
    touch "$result_dir/capacity-stop"
    wait "$monitor_pid" || true
  fi
  if (( vm_started == 1 )); then
    "${ssh[@]}" 'sudo poweroff' >/dev/null 2>&1 || true
    for _ in $(seq 1 30); do
      pgrep -f "^$repo/hypervisor/.*/cloud-hypervisor .*${scratch}" >/dev/null || break
      sleep 1
    done
    for pid in $(pgrep -f "^$repo/hypervisor/.*/cloud-hypervisor .*${scratch}" || true); do
      sudo -n kill "$pid" || true
    done
  fi
  tmux kill-session -t "$session" 2>/dev/null || true
  if (( dhcp_started == 1 )) && [[ -f "$result_dir/dnsmasq.pid" ]]; then
    sudo -n kill "$(<"$result_dir/dnsmasq.pid")" 2>/dev/null || true
  fi
  rm -f -- "$scratch"
  rm -f -- "$root_scratch"
}
trap cleanup EXIT

cp --reflink=auto --sparse=always "$golden" "$scratch"
if [[ -n "$root_disk" ]]; then
  # Keep the validated system image immutable across runs. The guest writes to
  # this disposable overlay; the workload disk is copied separately above.
  qemu-img create -q -f qcow2 -F qcow2 -b "$root_disk" "$root_scratch"
  root_disk="$root_scratch"
fi
sudo -n ip link set "$tap" down 2>/dev/null || true
sudo -n ip tuntap del dev "$tap" mode tap 2>/dev/null || true
sudo -n ip tuntap add dev "$tap" mode tap user "$(id -un)"
sudo -n ip addr add "$host_ip" dev "$tap"
sudo -n ip link set "$tap" up
# The guest root image obtains its fixed experimental address via DHCP. Keep
# this per-run service scoped to the VM, instead of relying on host state.
sudo -n dnsmasq --port=0 --interface="$tap" --except-interface=lo --bind-interfaces \
  --dhcp-range="$guest_ip,$guest_ip,12h" \
  --dhcp-host="$mac,$guest_ip" \
  --pid-file="$result_dir/dnsmasq.pid" \
  --log-facility="$result_dir/dnsmasq.log"
dhcp_started=1
sudo -n unlink "$api_socket" 2>/dev/null || true
tmux new-session -d -s "$session" 9>&- \
  "FREE_PAGE_REPORTING='${FREE_PAGE_REPORTING:-off}' FAST_RAW_MIB='${FAST_RAW_MIB:-16384}' SLOW_RAW_MIB='${SLOW_RAW_MIB:-16384}' DEMETER_KERNEL_IMAGE='$kernel_image' FAST_MIB='$fast_mib' SLOW_MIB='$slow_mib' GUEST_CPU_WITH_FAST='$guest_cpu_with_fast' HOST_CPU_START='$host_cpu_start' VM_TAP='$tap' VM_MAC='$mac' VM_API_SOCKET='$api_socket' ROOT_DISK='$root_disk' WORKLOAD_DISK='$scratch' '$artifact_dir/start-vm.sh' '$vm_method' > '$vm_log' 2>&1"
vm_started=1
sudo -n python3 "$here/phase/baselines/startup_monitor.py" --disk "$scratch" --output "$result_dir/host-lifecycle.jsonl" --stop "$result_dir/startup-stop" >"$result_dir/startup-monitor.log" 2>"$result_dir/startup-monitor.err" &
startup_monitor_pid=$!

ready=0
for _ in $(seq 1 60); do
  if "${ssh[@]}" 'uname -r' >"$result_dir/uname.txt" 2>/dev/null; then ready=1; break; fi
  sleep 2
done
[[ $ready == 1 ]] || { echo "guest SSH timeout" >&2; exit 1; }
"${ssh[@]}" "echo '$reporting_order' | sudo tee /sys/module/page_reporting/parameters/page_reporting_order" >"$result_dir/page-reporting-order.log"

"${scp[@]}" \
  "$artifact_dir/rg" "$replay_driver" "$here/randomize_cgroup_once.sh" \
  "$base/experiments/noop-random/build/randomize_pages" \
  "$trace_dir/tool_calls.json" \
  "$artifact_dir/PhaseTieing_balloon.ko" "$artifact_dir/policy_exec" \
  clear@${guest_ip}:/tmp/
if [[ -f "$trace_dir/trace.jsonl" ]]; then "${scp[@]}" "$trace_dir/trace.jsonl" clear@${guest_ip}:/tmp/; fi
if [[ "$method" == random ]]; then
  cc -static -O2 -Wall -Wextra "$here/weighted_exec.c" -o "$here/.weighted_exec"
  "${scp[@]}" "$here/.weighted_exec" clear@${guest_ip}:/tmp/weighted_exec
  if [[ ${POLICY_PROBE:-0} == 1 ]]; then
    cc -static -O2 -Wall -Wextra "$here/policy_probe.c" -o "$here/.policy_probe"
    "${scp[@]}" "$here/.policy_probe" clear@${guest_ip}:/tmp/policy_probe
  fi
fi
if [[ "$method" == demeter ]]; then
  "${scp[@]}" "$base/experiments/swebench/trajectory_launcher.py" \
    "$placement_module" "$controller_driver" "$artifact_dir/collect.py" \
    clear@${guest_ip}:/tmp/
fi

"${ssh[@]}" 'bash -s' -- "$method" "$demeter_agent_mode" "$demeter_exch_thresh" "$demeter_initial_range_mb" "$phase_mode" "$phase_budget_mib" "$phase_no_events" "$sb_cross_mm" "$sb_file_demotion" "$sb_alloc" "$sb_batch" "$sb_load_period" "$sb_wakeup" "$sb_placement" "$sb_lifecycle" "$sb_wake_us" "$policy" "$static_fast_weight" "$static_slow_weight" <<'REMOTE_SETUP' >"$result_dir/setup.log" 2>"$result_dir/setup.err"
set -euo pipefail
method=$1
demeter_agent_mode=$2
demeter_exch_thresh=$3
demeter_initial_range_mb=$4
phase_mode=$5
phase_budget_mib=$6
phase_no_events=$7
sb_cross_mm=$8
sb_file_demotion=$9
sb_alloc=${10}
sb_batch=${11}
sb_load_period=${12}
sb_wakeup=${13}
sb_placement=${14}
sb_lifecycle=${15}
sb_wake_us=${16}
policy=${17}
static_fast_weight=${18}
static_slow_weight=${19}
sudo mkdir -p /mnt/workload
sudo mount /dev/vdb /mnt/workload
sudo mount --bind /proc /mnt/workload/proc
sudo mount --rbind /dev /mnt/workload/dev
sudo mount --make-rslave /mnt/workload/dev
sudo mount --rbind /sys /mnt/workload/sys
sudo mount --make-rslave /mnt/workload/sys
sudo mount --bind /mnt/workload/usr /mnt/workload/usr
sudo mount -o remount,bind,ro /mnt/workload/usr
# Match the original trace's immutable Python installation prefix.
if [[ -f /mnt/workload/opt/replay-readonly-conda ]]; then
  sudo mount --bind /mnt/workload/opt/conda /mnt/workload/opt/conda
  sudo mount -o remount,bind,ro /mnt/workload/opt/conda
fi
sudo install -m 0755 /tmp/rg /mnt/workload/tmp/rg
sudo install -m 0755 /tmp/policy_exec /mnt/workload/tmp/policy_exec
sudo install -m 0755 /tmp/replay_agentcgroup.py /mnt/workload/tmp/replay_agentcgroup.py
sudo install -m 0644 /tmp/tool_calls.json /mnt/workload/tmp/tool_calls.json
if [[ -f /tmp/trace.jsonl ]]; then sudo install -m 0644 /tmp/trace.jsonl /mnt/workload/tmp/trace.jsonl; fi
sudo mkdir -p /mnt/workload/out
sudo mkdir /sys/fs/cgroup/agent-workload
sudo swapoff -a
sudo sysctl -w vm.overcommit_memory=1 vm.compaction_proactiveness=0 vm.extfrag_threshold=1000 >/dev/null
sudo sh -c 'echo never > /sys/kernel/mm/transparent_hugepage/enabled'
sudo insmod /tmp/PhaseTieing_balloon.ko
if [[ "$method" == tpp ]]; then
  sudo sysctl -w kernel.numa_balancing=2 >/dev/null
  sudo sh -c 'echo 1 > /sys/kernel/mm/numa/demotion_enabled'
  # Match the Demeter artifact's tpp.py launcher: reserve 10% headroom in
  # the fast tier so kswapd proactively demotes cold pages and promotions
  # have space to succeed.
  sudo sysctl -w vm.demote_scale_factor=1000 >/dev/null
else
  sudo sysctl -w kernel.numa_balancing=0 >/dev/null
  sudo sh -c 'echo 0 > /sys/kernel/mm/numa/demotion_enabled'
fi
if [[ "$method" == demeter ]]; then
  extra_module_args=(sandbox_lifecycle_wakeup="$sb_lifecycle" sandbox_sample_wake_us="$sb_wake_us" sandbox_placement_enabled="$sb_placement" sandbox_sample_wakeup="$sb_wakeup" load_latency_sample_period="$sb_load_period" retired_stores_sample_period=65535 sandbox_load_sample_period="$sb_load_period" sandbox_allocation_enabled="$sb_alloc" sandbox_sample_batch="$sb_batch")
    if [[ $(modinfo -p /tmp/PhaseTieing_placement.ko) == *sandbox_cross_mm* ]]; then extra_module_args+=(sandbox_cross_mm="$sb_cross_mm" sandbox_file_demotion="$sb_file_demotion"); fi
    if [[ $(modinfo -p /tmp/PhaseTieing_placement.ko) == *sandbox_load_event* ]]; then extra_module_args+=(sandbox_load_event=33232); fi
    sudo insmod /tmp/PhaseTieing_placement.ko sandbox_diagnostics=1 "${extra_module_args[@]}"
  if [[ "$phase_mode" == 0 && "$demeter_agent_mode" == 1 ]]; then
    # Agent tool processes often live for only a few seconds.  Use bounded
    # initial coverage and a shorter refinement period so useful placement can
    # be reached before the process exits.
    echo 50 | sudo tee /sys/module/PhaseTieing_placement/parameters/split_period_ms >/dev/null
    echo 3 | sudo tee /sys/module/PhaseTieing_placement/parameters/rtree_split_thresh >/dev/null
    echo "$demeter_exch_thresh" | sudo tee /sys/module/PhaseTieing_placement/parameters/rtree_exch_thresh >/dev/null
    echo "$demeter_initial_range_mb" | sudo tee /sys/module/PhaseTieing_placement/parameters/initial_range_size_mb >/dev/null
    echo 1 | sudo tee /sys/module/PhaseTieing_placement/parameters/adaptive_range >/dev/null
  fi
  sudo install -m 0755 /tmp/trajectory_launcher.py /tmp/PhaseTieing
fi
# The noop and random controls use the Demeter kernel only as a common
# substrate.  Fail closed if its placement policy is accidentally active.
if [[ "$method" == demeter ]]; then
  test -d /sys/module/PhaseTieing_placement
else
  test ! -d /sys/module/PhaseTieing_placement
fi
if [[ "$method" == noop || "$method" == random ]]; then
  test "$(sysctl -n kernel.numa_balancing)" = 0
  [[ "$(cat /sys/kernel/mm/numa/demotion_enabled)" =~ ^(0|false)$ ]]
fi
if [[ "$method" == random ]]; then
  # Weighted interleave applies to pages faulted after the one-time snapshot.
  # Each VM is isolated, so these global weights affect no other experiment.
  mapfile -t memory_nodes < <(numactl -H | awk '/^node [0-9]+ size:/ && $4+0>0 {print $2}')
  test "${#memory_nodes[@]}" = 2
  printf '%s\n' "${memory_nodes[0]}" | sudo tee /tmp/fast-node >/dev/null
  printf '%s\n' "${memory_nodes[1]}" | sudo tee /tmp/slow-node >/dev/null
  echo 1 | sudo tee "/sys/kernel/mm/mempolicy/weighted_interleave/node${memory_nodes[0]}" >/dev/null
  echo 4 | sudo tee "/sys/kernel/mm/mempolicy/weighted_interleave/node${memory_nodes[1]}" >/dev/null
  test "$(cat "/sys/kernel/mm/mempolicy/weighted_interleave/node${memory_nodes[0]}")" = 1
  test "$(cat "/sys/kernel/mm/mempolicy/weighted_interleave/node${memory_nodes[1]}")" = 4
  printf 'static_nodes=fast:%s slow:%s weights:1:4\n' "${memory_nodes[0]}" "${memory_nodes[1]}"
fi
if [[ "$policy" == static ]]; then
  echo "$static_fast_weight" | sudo tee /sys/kernel/mm/mempolicy/weighted_interleave/node0 >/dev/null
  echo "$static_slow_weight" | sudo tee /sys/kernel/mm/mempolicy/weighted_interleave/node1 >/dev/null
fi
printf 'baseline_policy=%s\n' "$policy"
cat /sys/kernel/mm/mempolicy/weighted_interleave/node{0,1}
printf 'policy_method=%s\n' "$method"
grep '^PhaseTieing_' /proc/modules || true
printf 'demeter_placement_loaded=%s\n' "$(test -d /sys/module/PhaseTieing_placement && echo yes || echo no)"
printf 'numa_balancing=%s\n' "$(sysctl -n kernel.numa_balancing)"
printf 'demotion_enabled=%s\n' "$(cat /sys/kernel/mm/numa/demotion_enabled)"
sudo chroot /mnt/workload /usr/bin/env \
  PATH=/opt/conda/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  HOME=/root git config --global --add safe.directory /testbed
sudo chroot /mnt/workload /usr/bin/env \
  PATH=/opt/conda/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  HOME=/root git config --global user.email test@test.com
sudo chroot /mnt/workload /usr/bin/env \
  PATH=/opt/conda/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  HOME=/root git config --global user.name Test
# Memory offlining completes asynchronously after the balloon workqueue returns.
sleep 25
sudo sh -c 'echo 3 > /proc/sys/vm/drop_caches'
if [[ "$phase_mode" == 1 ]]; then
  mapfile -t nodes < <(numactl -H | awk '/^node [0-9]+ size:/ && $4+0>0 {print $2}')
  test "${#nodes[@]}" = 2
  fast_node=${nodes[0]}
  slow_node=${nodes[1]}
  echo "[{\"id\":\"0\",\"cgroup\":\"/sys/fs/cgroup/agent-workload\"}]" | sudo tee /tmp/controller-config.json >/dev/null
  no_events=()
  [[ "$phase_no_events" == 1 ]] && no_events+=(--without-runtime-events)
  sudo sh -c 'echo 0 > /sys/kernel/mm/demeter/targets/nr_targets'
  sudo python3 /tmp/controller.py --config /tmp/controller-config.json \
    --socket /mnt/workload/tmp/controller.sock --log /mnt/workload/out/controller.jsonl \
    --budget-mib "$phase_budget_mib" --fast-node "$fast_node" --slow-node "$slow_node" \
    "${no_events[@]}" >/tmp/controller.log 2>/tmp/controller.err &
  echo $! > /tmp/controller.pid
  for _ in $(seq 1 100); do
    [[ -S /mnt/workload/tmp/controller.sock ]] && break
    sleep .1
  done
  test -S /mnt/workload/tmp/controller.sock
  sudo python3 /tmp/collect.py --output /mnt/workload/out/diagnostics --slots 1 --stop /tmp/diag-stop >/tmp/diag-collector.log 2>/tmp/diag-collector.err &
  printf '%s\n' "$slow_node" | sudo tee /mnt/workload/tmp/phase-slow-node >/dev/null
fi
numactl -H
cat /proc/meminfo
cat /proc/vmstat
sysctl kernel.numa_balancing vm.demote_scale_factor 2>/dev/null || true
cat /sys/kernel/mm/numa/demotion_enabled 2>/dev/null || true
cat /sys/devices/system/node/has_cpu /sys/devices/system/node/has_memory 2>/dev/null || true
if [[ "$method" == demeter ]]; then
  grep -H . /sys/module/PhaseTieing_placement/parameters/{split_period_ms,rtree_split_thresh,rtree_exch_thresh,initial_range_size_mb,adaptive_range,load_latency_sample_period,retired_stores_sample_period}
fi
REMOTE_SETUP

if [[ ${POLICY_PROBE:-0} == 1 && "$method" == random ]]; then
  "${ssh[@]}" "sudo /tmp/weighted_exec \$(cat /tmp/fast-node) \$(cat /tmp/slow-node) /tmp/policy_probe" \
    >"$result_dir/policy-probe.log" 2>"$result_dir/policy-probe.err"
fi

if [[ ${SETUP_ONLY:-0} == 1 ]]; then
  exit 0
fi

"${ssh[@]}" 'sudo sh -c "echo 3 > /proc/sys/vm/drop_caches"'
"${ssh[@]}" 'cat /proc/modules; cat /proc/sys/kernel/numa_balancing; cat /sys/kernel/mm/numa/demotion_enabled; cat /sys/kernel/mm/mempolicy/weighted_interleave/node{0,1}' >"$result_dir/policy-preflight.log"

"${scp[@]}" "$here/phase/baselines/guest_state.py" clear@${guest_ip}:/tmp/capacity_guest.py
sudo -n python3 "$here/phase/baselines/host_monitor.py" \
  --api "$api_socket" --output "$result_dir/host-capacity.jsonl" \
  --guest "$guest_ip" --key "$key" --stop "$result_dir/capacity-stop" \
  --expected-reporting "${FREE_PAGE_REPORTING:-off}" --expected-order "$reporting_order" --ready "$result_dir/capacity-ready.json" \
  --policy "${CAPACITY_POLICY:-static}" --fast-mib "$fast_mib" --slow-mib "$slow_mib" \
  >"$result_dir/host-monitor.log" 2>"$result_dir/host-monitor.err" &
monitor_pid=$!
for _ in $(seq 1 100); do
  [[ -f "$result_dir/capacity-ready.json" ]] && break
  kill -0 "$monitor_pid" 2>/dev/null || break
  sleep .1
done
test -f "$result_dir/capacity-ready.json" || { echo "capacity config preflight failed" >&2; exit 97; }
if [[ -n "${REPLAY_BARRIER_DIR:-}" ]]; then
  printf '%s\n' "$guest_ip" >"$REPLAY_BARRIER_DIR/lane$lane.ready"
  for _ in $(seq 1 2400); do
    [[ -f "$REPLAY_BARRIER_DIR/lane$lane.go" ]] && break
    sleep .1
  done
  test -f "$REPLAY_BARRIER_DIR/lane$lane.go" || { echo "replay barrier timeout" >&2; exit 98; }
fi
set +e
"${ssh[@]}" 'bash -s' -- "$method" "$seed" "$trigger_bytes" "$speed" "$replay_timeout_s" "$phase_mode" "${REPLAY_SEARCH_BACKEND:-ripgrep}" "${PHASE_COUNTERS:-0}" "${BIND_TOOL_NODE:--1}" "$policy" <<'REMOTE_RUN' >"$result_dir/replay.log" 2>"$result_dir/replay.err"
set -uo pipefail
method=$1
seed=$2
trigger_bytes=$3
speed=$4
replay_timeout_s=$5
phase_mode=$6
REPLAY_SEARCH_BACKEND=$7
phase_counters=$8
bind_tool_node=$9
policy=${10}
cgroup=/sys/fs/cgroup/agent-workload
random_pid=
if [[ "$method" == random ]]; then
  static_ready_host=/mnt/workload/tmp/static-placement-ready
  static_ready_guest=/tmp/static-placement-ready
  sudo rm -f "$static_ready_host"
  sudo env FAST_NODE="$(cat /tmp/fast-node)" SLOW_NODE="$(cat /tmp/slow-node)" \
    /tmp/randomize_cgroup_once.sh "$cgroup" "$seed" "$trigger_bytes" \
    /mnt/workload/out/randomization.jsonl "$static_ready_host" \
    >/tmp/randomizer.log 2>/tmp/randomizer.err &
  random_pid=$!
fi
replay=(chroot /mnt/workload /usr/bin/env
  PATH=/usr/local/bin:/opt/conda/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin
  REPLAY_SEARCH_BACKEND="${REPLAY_SEARCH_BACKEND:-ripgrep}"
  HOME=/root LANG=C.UTF-8 LC_ALL=C.UTF-8
  EXPECTED_MEMORY_POLICY="$policy"
  /opt/conda/bin/python /tmp/replay_agentcgroup.py /tmp/tool_calls.json
  --output /out/result.json
  --cgroup /sys/fs/cgroup/agent-workload --join-cgroup
  --timing original --speed "$speed")
if [[ "$phase_counters" == 1 ]]; then replay+=(--phase-counters --granite-raw); fi
if [[ "$policy" != phase ]]; then
  replay=(chroot /mnt/workload /tmp/policy_exec "$policy" "${replay[@]:2}")
fi
if [[ -f /mnt/workload/tmp/trace.jsonl ]]; then replay+=(--trace /tmp/trace.jsonl); fi
if [[ "$method" == random ]]; then
  replay+=(--static-weighted-interleave --static-ready-file "$static_ready_guest")
fi
if [[ "$method" == demeter ]]; then
  runner=(sudo)
  if (( replay_timeout_s > 0 )); then
    runner+=(timeout --signal=INT --kill-after=15s "${replay_timeout_s}s")
  fi
  if [[ "$phase_mode" == 1 ]]; then
    "${runner[@]}" /usr/bin/time -f 'TIME wall=%e user=%U sys=%S maxrss_kib=%M minflt=%R majflt=%F' \
      "${replay[@]}" --phase-socket /tmp/controller.sock --phase-slow-node "$(cat /mnt/workload/tmp/phase-slow-node)"
    replay_status=$?
  else
  "${runner[@]}" env DEMETER_TARGET_SLOTS=32 /usr/bin/time \
    -f 'TIME wall=%e user=%U sys=%S maxrss_kib=%M minflt=%R majflt=%F' \
    /tmp/PhaseTieing -- "${replay[@]}" --demeter-tree --demeter-tree-slots 31
  replay_status=$?
  fi
else
  runner=(sudo)
  if (( replay_timeout_s > 0 )); then
    runner+=(timeout --signal=INT --kill-after=15s "${replay_timeout_s}s")
  fi
  command=("${replay[@]}")
  if [[ "$method" == random ]]; then
    command=(/tmp/weighted_exec "$(cat /tmp/fast-node)" "$(cat /tmp/slow-node)" "${replay[@]}")
  fi
  "${runner[@]}" /usr/bin/time \
    -f 'TIME wall=%e user=%U sys=%S maxrss_kib=%M minflt=%R majflt=%F' \
    "${command[@]}"
  replay_status=$?
fi
random_status=0
if [[ -n "$random_pid" ]]; then wait "$random_pid" || random_status=$?; fi
printf '%s\n' "$replay_status" | sudo tee /mnt/workload/out/replay-status >/dev/null
printf '%s\n' "$random_status" | sudo tee /mnt/workload/out/randomizer-status >/dev/null
if [[ "$method" == random ]]; then
  sudo cp /tmp/randomizer.log /tmp/randomizer.err /mnt/workload/out/
fi
if (( replay_status == 0 && random_status != 0 )); then
  exit "$random_status"
fi
if [[ "$policy" == phase ]]; then
sudo touch /tmp/diag-stop
for _ in $(seq 1 100); do [[ -f /mnt/workload/out/diagnostics/done ]] && break; sleep .05; done
test -f /mnt/workload/out/diagnostics/done || exit 99
sudo cp /tmp/diag-collector.err /mnt/workload/out/
fi
exit "$replay_status"
REMOTE_RUN
run_status=$?
set -e
echo "$run_status" >"$result_dir/ssh-status"

"${ssh[@]}" 'cat /sys/fs/cgroup/agent-workload/memory.peak; numastat -m; cat /proc/meminfo; cat /proc/vmstat; find /sys/kernel/mm/demeter -maxdepth 3 -type f -print -exec cat {} \; 2>/dev/null || true; dmesg | tail -300' \
  >"$result_dir/after.log" 2>"$result_dir/after.err" || true
"${ssh[@]}" 'sudo cp /tmp/controller.err /tmp/controller.log /mnt/workload/out/ 2>/dev/null || true; dmesg' >"$result_dir/dmesg.log" 2>"$result_dir/dmesg.err"
"${scp[@]}" -r clear@${guest_ip}:/mnt/workload/out "$result_dir/guest-out"

[[ -f "$result_dir/guest-out/result.json" ]] || exit 1
if [[ "$method" == random ]]; then
  python3 - "$result_dir/guest-out/randomization.jsonl" <<'CHECK_STATIC'
import json
import sys

records = [json.loads(line) for line in open(sys.argv[1])]
pages = [record for record in records if "present" in record]
present = sum(record["present"] for record in pages)
fast = sum(record["after_fast"] for record in pages)
if not present or not 0.18 <= fast / present <= 0.22:
    raise SystemExit(f"initial static fast share outside 18–22%: {fast}/{present}")
CHECK_STATIC
fi
exit "$run_status"
