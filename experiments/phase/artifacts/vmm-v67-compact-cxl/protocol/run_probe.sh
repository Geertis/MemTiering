#!/usr/bin/env bash
set -euo pipefail

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
sb_probe_period_ms=${SB_PROBE_PERIOD_MS:-50}
[[ "$sb_probe_period_ms" =~ ^[1-9][0-9]*$ ]] || exit 2
sb_alloc=${SB_ALLOCATION:-1}
sb_batch=${SB_SAMPLE_BATCH:-1}
sb_load_period=${SB_LOAD_PERIOD:-65535}
[[ "$sb_alloc" =~ ^[01]$ && "$sb_batch" =~ ^[01]$ ]] || exit 2
sb_cross_mm=${SB_CROSS_MM:-1}
sb_file_demotion=${SB_FILE_DEMOTION:-1}
[[ "$sb_cross_mm" =~ ^[01]$ && "$sb_file_demotion" =~ ^[01]$ ]] || exit 2
sb_page_heat=${SB_PAGE_HEAT:-1}
[[ "$sb_page_heat" =~ ^[0-9]+$ ]] || exit 2
sb_confidence=${SB_CONFIDENCE_PRIOR:-4}
[[ "$sb_confidence" =~ ^[0-9]+$ ]] || exit 2
sb_wait_batch=${SB_WAIT_BATCH_PAGES:-1024}
[[ "$sb_wait_batch" =~ ^[0-9]+$ ]] || exit 2
sb_reclaim_time=${SB_RECLAIM_TIME_MS:-20}
[[ "$sb_reclaim_time" =~ ^[0-9]+$ ]] || exit 2
sb_load_event=${SB_LOAD_EVENT:-33232}
[[ "$sb_load_event" =~ ^[0-9]+$ ]] || exit 2
sb_sample_diag=${SB_SAMPLE_DIAGNOSTICS:-1}
[[ "$sb_sample_diag" =~ ^[0-9]+$ ]] || exit 2
sb_load_weight=${SB_LOAD_WEIGHT:-1000}
[[ "$sb_load_weight" =~ ^[0-9]+$ ]] || exit 2
sb_store_weight=${SB_STORE_WEIGHT:-1000}
[[ "$sb_store_weight" =~ ^[0-9]+$ ]] || exit 2
sb_active_kind=${SB_ACTIVE_KIND_LEARNING:-1}
[[ "$sb_active_kind" =~ ^[01]$ ]] || exit 2
sb_skip_scan=${SB_SKIP_SCAN:-1}
[[ "$sb_skip_scan" =~ ^[01]$ ]] || exit 2
sb_load_period=${SB_LOAD_PERIOD:-65535}
[[ "$sb_load_period" =~ ^[0-9]+$ ]] || exit 2
phase_mode=0
[[ "$method" == phase ]] && phase_mode=1
demeter_exch_thresh=${DEMETER_EXCH_THRESH:-33554432}
demeter_initial_range_mb=${DEMETER_INITIAL_RANGE_MB:-0}
lane=${VM_LANE:?set VM_LANE to an unused lane from 0 to 3}
[[ "$lane" =~ ^[0-7]$ ]] || { echo "VM_LANE must be from 0 to 3" >&2; exit 2; }
exec 9>"/tmp/PhaseTieing-agent-lane${lane}.lock"
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
method=demeter
if [[ "$method" == noop ]]; then
  method=random
  [[ "$trigger_bytes" != 0 ]] || trigger_bytes=1
fi
[[ -f "$golden" && -f "$trace_dir/tool_calls.json" && -f "$trace_dir/trace.jsonl" ]] || usage
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
balloon_module=${BALLOON_MODULE_PATH:-$artifact_dir/PhaseTieing_balloon.ko}
[[ -f "$balloon_module" ]] || exit 2
helper_binary=${BENEFIT_HELPER:-$here/phase/lookahead/sb_workload}
probe_timeout=${BENEFIT_TIMEOUT:-600}
[[ "$probe_timeout" =~ ^[1-9][0-9]*$ ]] || exit 2
kernel=demeter
[[ "$method" == tpp ]] && kernel=tpp

mkdir -p "$(dirname "$result_dir")"
mkdir "$result_dir"
printf '%s\n' "$requested_method" >"$result_dir/requested-method"
python3 - "$result_dir" "$artifact_dir" "$sb_cross_mm" "$sb_file_demotion" "${PROBE_PROGRAM:-$here/phase/probe.py}" "$helper_binary" "$0" "$fast_mib" "$slow_mib" "$balloon_module" <<'PROTOCOL'
import hashlib,json,sys,subprocess
from pathlib import Path
out,artifact,cross,files,program,helper,launcher,fast_mib,slow_mib,balloon_module=sys.argv[1:]
paths=[Path(artifact)/n for n in ('vmlinux.bin','PhaseTieing_placement.ko','controller.py','collect.py')]+[Path(program),Path(helper),Path(helper).with_suffix(".c"),Path(launcher),Path(balloon_module)]
manifest={'method':Path(artifact).name,'host_kernel':subprocess.check_output(['uname','-r'],text=True).strip(),'cross_mm':int(cross),'file_demotion':int(files),'latency_delta_ns':int(__import__('os').environ.get('BENEFIT_DELTA_NS','80')),'initial_copy_ns':int(__import__('os').environ.get('BENEFIT_COPY_NS','20000')),'bootstrap_quota_mib':64,'case':__import__('os').environ.get('BENEFIT_CASE','bench'),'module_params':{k:__import__('os').environ.get(k,d) for k,d in [('SB_PAGE_HEAT', '1'), ('SB_CONFIDENCE_PRIOR', '4'), ('SB_WAIT_BATCH_PAGES', '1024'), ('SB_RECLAIM_TIME_MS', '20'), ('SB_LOAD_EVENT', '33232'), ('SB_SAMPLE_DIAGNOSTICS', '1'), ('SB_LOAD_WEIGHT', '1000'), ('SB_STORE_WEIGHT', '1000'), ('SB_LOAD_PERIOD', '65535'), ('SB_SKIP_SCAN', '1'), ('SB_ACTIVE_KIND_LEARNING', '1'), ('SB_ALLOCATION','1'), ('SB_SAMPLE_BATCH','1')]},'fast_mib':int(fast_mib),'slow_mib':int(slow_mib),'files':{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}}
archive=Path(out)/'protocol-inputs';archive.mkdir()
for p in paths:
 if p.is_file():
  import shutil
  shutil.copy2(p,archive/p.name)
(Path(out)/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
PROTOCOL
scratch="$here/.scratch-$(basename "$result_dir")-$$.ext4"
root_scratch="$here/.scratch-root-$(basename "$result_dir")-$$.qcow2"
session="agentcg-${method}-$$"
api_socket="$vm_scripts/cloud-hypervisor-$vm_method-lane$lane.sock"
vm_log="$result_dir/vm.log"
vm_started=0
dhcp_started=0

cleanup() {
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
  "DEMETER_KERNEL_IMAGE='$kernel_image' FAST_MIB='$fast_mib' SLOW_MIB='$slow_mib' GUEST_CPU_WITH_FAST='$guest_cpu_with_fast' HOST_CPU_START='$host_cpu_start' VM_TAP='$tap' VM_MAC='$mac' VM_API_SOCKET='$api_socket' ROOT_DISK='$root_disk' WORKLOAD_DISK='$scratch' '$vm_scripts/start-vm.sh' '$vm_method' > '$vm_log' 2>&1"
vm_started=1

ready=0
for _ in $(seq 1 60); do
  if "${ssh[@]}" 'uname -r' >"$result_dir/uname.txt" 2>/dev/null; then ready=1; break; fi
  sleep 2
done
[[ $ready == 1 ]] || { echo "guest SSH timeout" >&2; exit 1; }

"${scp[@]}" \
  "$here/replay_agentcgroup.py" "$here/randomize_cgroup_once.sh" \
  "$base/experiments/noop-random/build/randomize_pages" \
  "$trace_dir/tool_calls.json" "$trace_dir/trace.jsonl" \
  "$balloon_module" \
  clear@${guest_ip}:/tmp/
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
    "$placement_module" "$artifact_dir/controller.py" \
    clear@${guest_ip}:/tmp/
fi

"${ssh[@]}" 'bash -s' -- "$method" "$demeter_agent_mode" "$demeter_exch_thresh" "$demeter_initial_range_mb" "$phase_mode" "$phase_budget_mib" "$phase_no_events" "$sb_probe_period_ms" "$sb_cross_mm" "$sb_file_demotion" "${BENEFIT_DELTA_NS:-80}" "${BENEFIT_COPY_NS:-20000}" "$sb_page_heat" "$sb_confidence" "$sb_wait_batch" "$sb_reclaim_time" "$sb_load_event" "$sb_sample_diag" "$sb_load_weight" "$sb_store_weight" "$sb_load_period" "$sb_skip_scan" "$sb_active_kind" "$sb_alloc" "$sb_batch" <<'REMOTE_SETUP' >"$result_dir/setup.log" 2>"$result_dir/setup.err"
set -euo pipefail
method=$1
demeter_agent_mode=$2
demeter_exch_thresh=$3
demeter_initial_range_mb=$4
phase_mode=$5
phase_budget_mib=$6
phase_no_events=$7
sb_probe_period_ms=$8
sb_cross_mm=$9
sb_file_demotion=${10}
sb_page_heat=${13}
sb_confidence=${14}
sb_wait_batch=${15}
sb_reclaim_time=${16}
sb_load_event=${17}
sb_sample_diag=${18}
sb_load_weight=${19}
sb_store_weight=${20}
sb_load_period=${21}
sb_skip_scan=${22}
sb_active_kind=${23}
sb_alloc=${24}
sb_batch=${25}
export BENEFIT_DELTA_NS=${11} BENEFIT_COPY_NS=${12}
sudo mkdir -p /mnt/workload
sudo mount /dev/vdb /mnt/workload
sudo mount --bind /proc /mnt/workload/proc
sudo mount --rbind /dev /mnt/workload/dev
sudo mount --make-rslave /mnt/workload/dev
sudo mount --rbind /sys /mnt/workload/sys
sudo mount --make-rslave /mnt/workload/sys
sudo mount --bind /mnt/workload/usr /mnt/workload/usr
sudo mount -o remount,bind,ro /mnt/workload/usr
sudo install -m 0755 /tmp/replay_agentcgroup.py /mnt/workload/tmp/replay_agentcgroup.py
sudo install -m 0644 /tmp/tool_calls.json /mnt/workload/tmp/tool_calls.json
sudo install -m 0644 /tmp/trace.jsonl /mnt/workload/tmp/trace.jsonl
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
  if [[ "$phase_mode" == 1 ]]; then
    extra_module_args=()
    if [[ $(modinfo -p /tmp/PhaseTieing_placement.ko) == *sandbox_active_kind_learning* ]]; then extra_module_args+=(sandbox_active_kind_learning="$sb_active_kind"); fi
    if [[ $(modinfo -p /tmp/PhaseTieing_placement.ko) == *sandbox_skip_unprofitable_scan* ]]; then extra_module_args+=(sandbox_skip_unprofitable_scan="$sb_skip_scan"); fi
    if [[ $(modinfo -p /tmp/PhaseTieing_placement.ko) == *sandbox_cross_mm* ]]; then extra_module_args+=(sandbox_cross_mm="$sb_cross_mm" sandbox_file_demotion="$sb_file_demotion"); fi
    sudo insmod /tmp/PhaseTieing_placement.ko sandbox_allocation_enabled="$sb_alloc" sandbox_sample_batch="$sb_batch" sandbox_load_sample_period="$sb_load_period" sandbox_page_heat="$sb_page_heat" sandbox_confidence_prior="$sb_confidence" sandbox_wait_batch_pages="$sb_wait_batch" sandbox_reclaim_time_ms="$sb_reclaim_time" sandbox_load_event="$sb_load_event" sandbox_sample_diagnostics="$sb_sample_diag" sandbox_load_weight_permille="$sb_load_weight" sandbox_store_weight_permille="$sb_store_weight" sandbox_diagnostics=1 sandbox_latency_delta_ns="${BENEFIT_DELTA_NS:-80}" sandbox_initial_copy_ns="${BENEFIT_COPY_NS:-20000}" "${extra_module_args[@]}" sandbox_period_ms="$sb_probe_period_ms"
  else
    extra_module_args=()
    if [[ $(modinfo -p /tmp/PhaseTieing_placement.ko) == *sandbox_active_kind_learning* ]]; then extra_module_args+=(sandbox_active_kind_learning="$sb_active_kind"); fi
    if [[ $(modinfo -p /tmp/PhaseTieing_placement.ko) == *sandbox_skip_unprofitable_scan* ]]; then extra_module_args+=(sandbox_skip_unprofitable_scan="$sb_skip_scan"); fi
    if [[ $(modinfo -p /tmp/PhaseTieing_placement.ko) == *sandbox_cross_mm* ]]; then extra_module_args+=(sandbox_cross_mm="$sb_cross_mm" sandbox_file_demotion="$sb_file_demotion"); fi
    sudo insmod /tmp/PhaseTieing_placement.ko sandbox_allocation_enabled="$sb_alloc" sandbox_sample_batch="$sb_batch" sandbox_load_sample_period="$sb_load_period" sandbox_page_heat="$sb_page_heat" sandbox_confidence_prior="$sb_confidence" sandbox_wait_batch_pages="$sb_wait_batch" sandbox_reclaim_time_ms="$sb_reclaim_time" sandbox_load_event="$sb_load_event" sandbox_sample_diagnostics="$sb_sample_diag" sandbox_load_weight_permille="$sb_load_weight" sandbox_store_weight_permille="$sb_store_weight" sandbox_diagnostics=1 sandbox_latency_delta_ns="${BENEFIT_DELTA_NS:-80}" sandbox_initial_copy_ns="${BENEFIT_COPY_NS:-20000}" "${extra_module_args[@]}"
  fi
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

"${scp[@]}" "$artifact_dir/collect.py" clear@${guest_ip}:/tmp/
"${scp[@]}" "$helper_binary" clear@${guest_ip}:/tmp/
"${scp[@]}" "$here/phase/sb_probe" clear@${guest_ip}:/tmp/
"${scp[@]}" "${PROBE_PROGRAM:-$here/phase/probe.py}" clear@${guest_ip}:/tmp/probe.py
set +e
"${ssh[@]}" 'bash -s' -- "$probe_timeout" "${BENEFIT_CASE:-bench}" <<'REMOTE_PROBE' >"$result_dir/probe.log" 2>"$result_dir/probe.err"
set -euo pipefail
sudo kill "$(cat /tmp/controller.pid)" || true
sleep .3
sudo sh -c 'echo 0 > /sys/kernel/mm/demeter/sandboxes/nr_sandboxes'
sudo mkdir /sys/fs/cgroup/probe
echo '[{"id":"probe","cgroup":"/sys/fs/cgroup/probe"}]' | sudo tee /tmp/probe-config.json >/dev/null
sudo python3 /tmp/controller.py --config /tmp/probe-config.json \
  --socket /tmp/probe-controller.sock --log /mnt/workload/out/probe-controller.jsonl \
  --budget-mib 64 --minimum-mib 8 --fast-node 0 --slow-node 1 \
  >/tmp/probe-controller.log 2>/tmp/probe-controller.err &
echo $! > /tmp/probe-controller.pid
for _ in $(seq 1 100); do [[ -S /tmp/probe-controller.sock ]] && break; sleep .1; done
status=0
sudo timeout --signal=INT --kill-after=5s "${1}s" python3 /tmp/probe.py 0 1 "$2" || status=$?
sudo cp /tmp/probe-result.json /tmp/probe-controller.err /mnt/workload/out/ 2>/dev/null || true
sudo cp -r /tmp/probe-diagnostics /mnt/workload/out/diagnostics 2>/dev/null || true
cat /sys/kernel/mm/demeter/sandboxes/0/stats 2>/dev/null || true
exit "$status"
REMOTE_PROBE
probe_status=$?
set -e
printf '%s\n' "$probe_status" >"$result_dir/ssh-status"
"${ssh[@]}" 'dmesg; cat /tmp/probe-controller.err' >"$result_dir/dmesg.log"
"${scp[@]}" -r clear@${guest_ip}:/mnt/workload/out "$result_dir/guest-out"
exit "$probe_status"
