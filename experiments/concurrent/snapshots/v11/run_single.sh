#!/usr/bin/env bash
set -euo pipefail
cold_media=${COLD_MEDIA:-cxl}
[[ "$cold_media" =~ ^(cxl|ssd)$ ]] || exit 2
if [[ "${WAIT_REPORTING_ORDER:--1}" != -1 ]]; then
  [[ "$cold_media" =~ ^(cxl|ssd)$ && "${ADAPTIVE_MODE:-0}" == 1 && "${WAIT_REPORTING_ORDER}" == 0 && "${PAGE_REPORTING_ORDER:-5}" == 5 && "${BARRIER_REPORTING_START:-0}" == 1 ]] || { echo "fine reporting requires the explicit adaptive SSD/barrier treatment" >&2; exit 2; }
fi
[[ "${VALIDATE_MEDIA_ONLY:-0}" != 1 ]] || exit 0
policy=${BASELINE_POLICY:?set BASELINE_POLICY to dram, cxl, static, or phase}
[[ "$policy" =~ ^(dram|cxl|static|phase)$ ]] || exit 2
static_fast_weight=${STATIC_FAST_WEIGHT:-1}
static_slow_weight=${STATIC_SLOW_WEIGHT:-4}
[[ "$static_fast_weight" =~ ^[1-9][0-9]*$ && "$static_slow_weight" =~ ^[1-9][0-9]*$ ]] || exit 2
reporting_order=${PAGE_REPORTING_ORDER:-9}
[[ "$reporting_order" =~ ^([0-9])$ ]] || exit 2

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
[[ "$lane" =~ ^([0-9]|1[0-5])$ ]] || { echo "VM_LANE must be from 0 to 15" >&2; exit 2; }
if [[ -e "/tmp/demeter-agent-lane${lane}.lock" ]]; then exec 9<>"/tmp/demeter-agent-lane${lane}.lock"; else exec 9>"/tmp/demeter-agent-lane${lane}.lock"; fi
flock -n 9 || { echo "VM lane $lane is busy" >&2; exit 1; }
subnet=$((92 + lane))
guest_ip="192.168.$subnet.100"
host_ip="192.168.$subnet.1/24"
tap="demeter$lane"
mac=$(printf '2e:89:a8:e4:93:%02x' "$lane")
host_cpu_start=$((lane * 4)) # V3: disjoint L2 execution pools; host controls use CPUs 32-35
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
artifact_dir=${PHASE_ARTIFACT_DIR:-$here/phase/artifacts/vmm-v54-ssd-placement}
placement_module="$artifact_dir/demeter_placement.ko"
replay_driver="$artifact_dir/replay_agentcgroup.py"
controller_driver="$artifact_dir/controller.py"
kernel_image="$artifact_dir/vmlinux.bin"
if [[ "$requested_method" == native ]]; then
  placement_module="$base/baselines/20261003-current/bin/demeter_placement.ko"
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
python3 - "$result_dir" "$requested_method" "$trace_dir" "$fast_mib" "$slow_mib" "$phase_budget_mib" "$phase_no_events" "$placement_module" "$kernel_image" "$replay_driver" "$controller_driver" "$artifact_dir/collect.py" "$0" "$artifact_dir/demeter_balloon.ko" "$artifact_dir/rg" "$artifact_dir/vmm_pytest_compat.py" <<'MANIFEST'
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
import shutil,fcntl
blobroot=Path('/r0/home/geerties/HW/Demeter/experiments/agentcgroup-demeter/results/PhaseTieing-concurrent-20261030-v11-controller-health/_input_blobs')
blobroot.mkdir(parents=True,exist_ok=True)
def archive_copy(source,destination):
 source=Path(source);destination=Path(destination)
 digest=hashlib.sha256(source.read_bytes()).hexdigest()
 blob=blobroot/digest
 with (blobroot/(digest+'.lock')).open('a') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX)
  if not blob.exists():
   temporary=blobroot/(digest+'.'+str(os.getpid())+'.tmp')
   shutil.copy2(source,temporary);temporary.replace(blob)
  os.link(blob,destination)
for name in files+[str(Path(trace)/'tool_calls.json'),*([str(Path(trace)/'trace.jsonl')] if (Path(trace)/'trace.jsonl').exists() else [])]:
 archive_copy(name,archive/Path(name).name)

extras=[Path('/r0/home/geerties/HW/Demeter/experiments/agentcgroup-demeter/phase/PhaseTieing_concurrent_v11/verify_topology.py'), Path('/r0/home/geerties/HW/Demeter/experiments/agentcgroup-demeter/phase/all_traces_v67/relocate_private.py'), Path(files[0]).parent / 'start-vm.sh', Path('/r0/home/geerties/HW/Demeter/experiments/agentcgroup-demeter/phase/PhaseTieing_concurrent_v11/host_monitor.py'), Path('/r0/home/geerties/HW/Demeter/experiments/agentcgroup-demeter/phase/PhaseTieing_concurrent_v11/guest_state.py')]
extras += [Path(files[0]).parent / n for n in ['cxl_controller.py','ssd_controller.py','legacy_controller.py','adaptive_controller.py','feedback.py','actuator.py','placement.py','history.py']]
if os.environ.get('REFERENCE_PROFILE'): extras.append(Path(os.environ['REFERENCE_PROFILE']))
for path in extras:
 manifest['files'][str(path)]=hashlib.sha256(path.read_bytes()).hexdigest()
 archive_copy(path,archive/path.name)
p=Path('/r0/home/geerties/HW/Demeter/experiments/agentcgroup-demeter/phase/PhaseTieing_concurrent_v11/startup_monitor.py')
manifest['files'][str(p)]=hashlib.sha256(p.read_bytes()).hexdigest()
archive_copy(p,archive/p.name)
vmm=Path('/r0/home/geerties/HW/Demeter/sosp25-demeter/hypervisor/target/host-returns-v19/cloud-hypervisor')
manifest['files'][str(vmm)]=hashlib.sha256(vmm.read_bytes()).hexdigest()
archive_copy(vmm, archive/'cloud-hypervisor')
p=Path(files[0]).parent/'policy_exec'
manifest['files'][str(p)]=hashlib.sha256(p.read_bytes()).hexdigest()
archive_copy(p,archive/p.name)
manifest['guest_tier_nodes']={'fast':int(os.environ.get('SB_FAST_NODE','1')),'slow':int(os.environ.get('SB_SLOW_NODE','0'))}
manifest['vmm_private_node']=int(os.environ['VMM_PRIVATE_NODE'])
manifest['future_benefit']=int(os.environ.get('SB_FUTURE_BENEFIT','1'))
manifest['startup_headroom_mib']=int(os.environ.get('STARTUP_HEADROOM_MIB','0'))
manifest['system_tier']=os.environ.get('GUEST_SYSTEM_TIER','fast')
manifest['host_zone_nodes']=[int(os.environ.get('HOST_FAST_NUMA_NODE','0')),int(os.environ.get('HOST_SLOW_NUMA_NODE','2'))]
manifest['host_receipts']=int(os.environ.get('DEMETER_REPORTING_RECEIPTS','1'))
manifest['recycle_credit']=int(os.environ.get('SB_RECYCLE_CREDIT','1'))
manifest['sampling_enabled']=int(os.environ.get('SB_SAMPLING','1'))
manifest['pytest_compat']=int(os.environ.get('PYTEST_COMPAT','0'))
manifest['adaptive_mode']=int(os.environ.get('ADAPTIVE_MODE','0'))
manifest['latency_limit']=float(os.environ.get('LATENCY_LIMIT','.05'))
manifest['adaptive_initial_mib']=int(os.environ.get('ADAPTIVE_INITIAL_MIB','6144'))
manifest['ssd_active_headroom_mib']=int(os.environ.get('SSD_ACTIVE_HEADROOM_MIB','16'))
manifest['page_cluster']=int(os.environ.get('SSD_PAGE_CLUSTER','0'))
manifest['cold_media']=os.environ.get('COLD_MEDIA','cxl')
manifest['ssd_wait_mib']=int(os.environ.get('SSD_WAIT_MIB','16'))
manifest['ssd_chunk_mib']=int(os.environ.get('SSD_CHUNK_MIB','8'))
manifest['replay_timing']=os.environ.get('REPLAY_TIMING','gaps')
manifest['replay_use_image_test_env']=int(os.environ.get('REPLAY_USE_IMAGE_TEST_ENV','0'))
manifest['network_repair_proxy']=bool(int(os.environ.get('REPLAY_NETWORK_PROXY','0')))
manifest['restore_scratchpad']=bool(int(os.environ.get('REPLAY_CREATE_SCRATCHPAD','0')))
manifest['baseline_policy']=os.environ['BASELINE_POLICY']
manifest['static_weights']=[int(os.environ.get('STATIC_FAST_WEIGHT','1')),int(os.environ.get('STATIC_SLOW_WEIGHT','4'))]
manifest['barrier_reporting_start']=int(os.environ.get('BARRIER_REPORTING_START','0'))
manifest['drain_reporting_start']=int(os.environ.get('DRAIN_REPORTING_START','0'))
manifest['pause_reporting_active']=int(os.environ.get('PAUSE_REPORTING_ACTIVE','0'))
manifest['wait_reporting_order']=int(os.environ.get('WAIT_REPORTING_ORDER','-1'))
manifest['wait_reporting_delay_ms']=int(os.environ.get('WAIT_REPORTING_DELAY_MS','500'))
manifest['page_reporting_order']=int(__import__('os').environ.get('PAGE_REPORTING_ORDER','9'))
manifest['capacity_policy']=__import__('os').environ.get('CAPACITY_POLICY','static')
manifest['free_page_reporting']=__import__('os').environ.get('FREE_PAGE_REPORTING','off')
manifest['raw_zones_mib']=[int(__import__('os').environ.get(k,'16384')) for k in ['FAST_RAW_MIB','SLOW_RAW_MIB']]
(Path(out)/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
MANIFEST
scratch="$here/.scratch-$(basename "$result_dir")-$$.ext4"
root_scratch="$here/.scratch-root-$(basename "$result_dir")-$$.qcow2"
swap_scratch=
if [[ "$cold_media" == ssd || "${ATTACH_COLD_DISK:-0}" == 1 ]]; then
  swap_scratch="/pilot-swap/.scratch-swap-$(basename "$result_dir")-$$.raw"
  fallocate -l "${SSD_SWAP_MIB:?}M" "$swap_scratch"
  chmod 600 "$swap_scratch"
  mkswap -L agent-ssd "$swap_scratch" >"$result_dir/swap-format.log"
  findmnt -T "$swap_scratch" -J >"$result_dir/ssd-filesystem.json"
  lsblk -J -o NAME,TYPE,SIZE,ROTA,MOUNTPOINTS >"$result_dir/ssd-block-topology.json"
fi
session="agentcg-${method}-$$"
api_socket="$vm_scripts/cloud-hypervisor-$vm_method-lane$lane.sock"
vm_log="$result_dir/vm.log"
vm_started=0
dhcp_started=0
proxy_pid=
proxy_url=
monitor_pid=
startup_monitor_pid=

cleanup() {
  if [[ -n "$proxy_pid" ]]; then kill "$proxy_pid" 2>/dev/null || true; wait "$proxy_pid" 2>/dev/null || true; fi
  if [[ -n "$startup_monitor_pid" ]]; then
    touch "$result_dir/startup-stop"
    wait "$startup_monitor_pid" || true
  fi
  if [[ -n "$monitor_pid" ]]; then
    touch "$result_dir/capacity-stop"
    wait "$monitor_pid" || true
  fi
  if (( vm_started == 1 )); then
    "${ssh[@]}" 'sudo cat /tmp/controller.err /tmp/controller.log 2>/dev/null || true' >"$result_dir/startup-controller.log" 2>&1 || true
    "${ssh[@]}" 'sudo cat /mnt/workload/out/controller.jsonl 2>/dev/null || true' >"$result_dir/startup-controller-events.jsonl" 2>&1 || true
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
  if [[ -n "$swap_scratch" ]]; then rm -f -- "$swap_scratch"; fi
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
if [[ "${REPLAY_NETWORK_PROXY:-0}" == 1 ]]; then
  proxy_port=$((18760 + lane))
  python3 "${V63_PACKAGE_PROXY_HELPER:?}" --bind "${host_ip%/*}" --port "$proxy_port" --log "$result_dir/network-proxy.jsonl" >"$result_dir/network-proxy.log" 2>&1 &
  proxy_pid=$!
  proxy_url="http://${host_ip%/*}:$proxy_port"
fi
sudo -n unlink "$api_socket" 2>/dev/null || true
tmux new-session -d -s "$session" 9>&- \
  "SSD_SWAP_DISK='$swap_scratch' VMM_PRIVATE_NODE='${VMM_PRIVATE_NODE:?}' GUEST_SYSTEM_TIER='${GUEST_SYSTEM_TIER:-fast}' HOST_FAST_NUMA_NODE='${HOST_FAST_NUMA_NODE:-0}' HOST_SLOW_NUMA_NODE='${HOST_SLOW_NUMA_NODE:-2}' FREE_PAGE_REPORTING='${FREE_PAGE_REPORTING:-off}' FAST_RAW_MIB='${FAST_RAW_MIB:-16384}' SLOW_RAW_MIB='${SLOW_RAW_MIB:-16384}' DEMETER_KERNEL_IMAGE='$kernel_image' FAST_MIB='$fast_mib' SLOW_MIB='$slow_mib' GUEST_CPU_WITH_FAST='$guest_cpu_with_fast' HOST_CPU_START='$host_cpu_start' VM_TAP='$tap' VM_MAC='$mac' VM_API_SOCKET='$api_socket' DEMETER_REPORTING_RECEIPTS='${DEMETER_REPORTING_RECEIPTS:-1}' ROOT_DISK='$root_disk' WORKLOAD_DISK='$scratch' '$artifact_dir/start-vm.sh' '$vm_method' > '$vm_log' 2>&1"
vm_started=1
sudo -n python3 "$here/phase/PhaseTieing_concurrent_v11/startup_monitor.py" --disk "$scratch" --output "$result_dir/host-lifecycle.jsonl" --stop "$result_dir/startup-stop" >"$result_dir/startup-monitor.log" 2>"$result_dir/startup-monitor.err" &
startup_monitor_pid=$!

ready=0
for _ in $(seq 1 60); do
  if "${ssh[@]}" 'uname -r' >"$result_dir/uname.txt" 2>/dev/null; then ready=1; break; fi
  sleep 2
done
[[ $ready == 1 ]] || { echo "guest SSH timeout" >&2; exit 1; }
"${ssh[@]}" 'cat /proc/cmdline; cat /proc/sys/vm/mem_profiling; cat /proc/meminfo' >"$result_dir/boot-footprint.txt"
topology_phase=$phase_mode
[[ "$cold_media" == ssd ]] && topology_phase=0
sudo -n python3 "$here/phase/PhaseTieing_concurrent_v11/verify_topology.py" --api "$api_socket" \
  --boot-log "$vm_log" --output "$result_dir/guest-tier-map.json" \
  --zones "${HOST_FAST_NUMA_NODE:?},${HOST_SLOW_NUMA_NODE:?}" --phase "$topology_phase" \
  --fast-node "${SB_FAST_NODE:?}" --slow-node "${SB_SLOW_NODE:?}"
"${ssh[@]}" "echo '$reporting_order' | sudo tee /sys/module/page_reporting/parameters/page_reporting_order" >"$result_dir/page-reporting-order.log"

"${scp[@]}" \
  "$artifact_dir/rg" "$replay_driver" "$here/randomize_cgroup_once.sh" \
  "$base/experiments/noop-random/build/randomize_pages" \
  "$trace_dir/tool_calls.json" \
  "$artifact_dir/demeter_balloon.ko" "$artifact_dir/policy_exec" "$artifact_dir/vmm_pytest_compat.py" \
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
    "$placement_module" "$controller_driver" "$artifact_dir/cxl_controller.py" "$artifact_dir/ssd_controller.py" "$artifact_dir/legacy_controller.py" "$artifact_dir/adaptive_controller.py" "$artifact_dir/feedback.py" "$artifact_dir/actuator.py" "$artifact_dir/placement.py" "$artifact_dir/history.py" "$artifact_dir/collect.py" \
    clear@${guest_ip}:/tmp/
fi

if [[ -n "${REFERENCE_PROFILE:-}" ]]; then "${scp[@]}" "$REFERENCE_PROFILE" clear@${guest_ip}:/tmp/reference-profile.json; fi

"${ssh[@]}" 'bash -s' -- "$method" "$demeter_agent_mode" "$demeter_exch_thresh" "$demeter_initial_range_mb" "$phase_mode" "$phase_budget_mib" "$phase_no_events" "$sb_cross_mm" "$sb_file_demotion" "$sb_alloc" "$sb_batch" "$sb_load_period" "$sb_wakeup" "$sb_placement" "$sb_lifecycle" "$sb_wake_us" "$policy" "$static_fast_weight" "$static_slow_weight" "${SB_SAMPLING:-1}" "${PYTEST_COMPAT:-0}" "${SB_FAST_NODE:-1}" "${SB_SLOW_NODE:-0}" "${SB_FUTURE_BENEFIT:-1}" "${STARTUP_HEADROOM_MIB:-0}" "${SB_RECYCLE_CREDIT:-1}" "${WAIT_REPORTING_ORDER:--1}" "${WAIT_REPORTING_DELAY_MS:-500}" "$reporting_order" "${PAUSE_REPORTING_ACTIVE:-0}" "${DRAIN_REPORTING_START:-0}" "${BARRIER_REPORTING_START:-0}" "$cold_media" "${SSD_WAIT_MIB:-16}" "${SSD_CHUNK_MIB:-8}" "${ADAPTIVE_MODE:-0}" "${LATENCY_LIMIT:-.05}" "${ADAPTIVE_INITIAL_MIB:-6144}" "${SSD_FILE_WAIT_S:-.5}" "${SSD_ANON_WAIT_S:-3}" "${SSD_PAGE_CLUSTER:-0}" "${SSD_ACTIVE_HEADROOM_MIB:-16}" <<'REMOTE_SETUP' >"$result_dir/setup.log" 2>"$result_dir/setup.err"
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
sb_sampling=${20}
pytest_compat=${21}
guest_fast_node=${22}
guest_slow_node=${23}
future_benefit=${24}
startup_headroom=${25}
recycle_credit=${26}
wait_reporting_order=${27}
wait_reporting_delay_ms=${28}
active_reporting_order=${29}
pause_reporting_active=${30}
drain_reporting_start=${31}
barrier_reporting_start=${32}
cold_media=${33}
ssd_wait_mib=${34}
ssd_chunk_mib=${35}
adaptive_mode=${36}
latency_limit=${37}
adaptive_initial=${38}
file_wait=${39}
anon_wait=${40}
page_cluster=${41}
active_headroom=${42}
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
if [[ "$pytest_compat" == 1 ]]; then
  sudo mkdir -p /mnt/workload/opt/replay-deps
  sudo install -m 0644 /tmp/vmm_pytest_compat.py /mnt/workload/opt/replay-deps/vmm_pytest_compat.py
fi
sudo mkdir -p /mnt/workload/out
sudo mkdir /sys/fs/cgroup/agent-workload
sudo swapoff -a
if [[ "$cold_media" == ssd ]]; then
  test "$(sudo blkid -s LABEL -o value /dev/vdc)" = agent-ssd
  if [[ -e /sys/module/zswap/parameters/enabled ]]; then echo N | sudo tee /sys/module/zswap/parameters/enabled; fi
  sudo swapon /dev/vdc
  sudo sysctl -w vm.swappiness=100 vm.page-cluster="$page_cluster"
  cat /proc/swaps
fi
sudo sysctl -w vm.overcommit_memory=1 vm.compaction_proactiveness=0 vm.extfrag_threshold=1000 >/dev/null
sudo sh -c 'echo never > /sys/kernel/mm/transparent_hugepage/enabled'
sudo insmod /tmp/demeter_balloon.ko
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
if [[ "$method" == demeter && "$cold_media" == cxl ]]; then
  extra_module_args=(sandbox_recycle_credit="$recycle_credit" sandbox_fast_node="$guest_fast_node" sandbox_slow_node="$guest_slow_node" sandbox_future_benefit="$future_benefit" sandbox_sampling_enabled="$sb_sampling" sandbox_lifecycle_wakeup="$sb_lifecycle" sandbox_sample_wake_us="$sb_wake_us" sandbox_placement_enabled="$sb_placement" sandbox_sample_wakeup="$sb_wakeup" load_latency_sample_period="$sb_load_period" retired_stores_sample_period=65535 sandbox_load_sample_period="$sb_load_period" sandbox_allocation_enabled="$sb_alloc" sandbox_sample_batch="$sb_batch")
    if [[ $(modinfo -p /tmp/demeter_placement.ko) == *sandbox_cross_mm* ]]; then extra_module_args+=(sandbox_cross_mm="$sb_cross_mm" sandbox_file_demotion="$sb_file_demotion"); fi
    if [[ $(modinfo -p /tmp/demeter_placement.ko) == *sandbox_load_event* ]]; then extra_module_args+=(sandbox_load_event=33232); fi
    sudo insmod /tmp/demeter_placement.ko sandbox_diagnostics=1 "${extra_module_args[@]}"
  if [[ "$phase_mode" == 0 && "$demeter_agent_mode" == 1 ]]; then
    # Agent tool processes often live for only a few seconds.  Use bounded
    # initial coverage and a shorter refinement period so useful placement can
    # be reached before the process exits.
    echo 50 | sudo tee /sys/module/demeter_placement/parameters/split_period_ms >/dev/null
    echo 3 | sudo tee /sys/module/demeter_placement/parameters/rtree_split_thresh >/dev/null
    echo "$demeter_exch_thresh" | sudo tee /sys/module/demeter_placement/parameters/rtree_exch_thresh >/dev/null
    echo "$demeter_initial_range_mb" | sudo tee /sys/module/demeter_placement/parameters/initial_range_size_mb >/dev/null
    echo 1 | sudo tee /sys/module/demeter_placement/parameters/adaptive_range >/dev/null
  fi
  sudo install -m 0755 /tmp/trajectory_launcher.py /tmp/demeter
fi
# The noop and random controls use the Demeter kernel only as a common
# substrate.  Fail closed if its placement policy is accidentally active.
if [[ "$method" == demeter && "$cold_media" == cxl ]]; then
  test -d /sys/module/demeter_placement
else
  test ! -d /sys/module/demeter_placement
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
grep '^demeter_' /proc/modules || true
printf 'demeter_placement_loaded=%s\n' "$(test -d /sys/module/demeter_placement && echo yes || echo no)"
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
  fast_node=$guest_fast_node
  slow_node=$guest_slow_node
  test -d "/sys/devices/system/node/node$fast_node"
  test -d "/sys/devices/system/node/node$slow_node"
  echo "[{\"id\":\"0\",\"cgroup\":\"/sys/fs/cgroup/agent-workload\"}]" | sudo tee /tmp/controller-config.json >/dev/null
  no_events=()
  [[ "$phase_no_events" == 1 ]] && no_events+=(--without-runtime-events)
  if [[ "$cold_media" == cxl ]]; then sudo sh -c 'echo 0 > /sys/kernel/mm/demeter/targets/nr_targets'; fi
  reporting_flags=()
  if [[ "$wait_reporting_order" != -1 ]]; then
    reporting_flags+=(--wait-reporting-order "$wait_reporting_order" --active-reporting-order "$active_reporting_order" --wait-reporting-delay-ms "$wait_reporting_delay_ms")
  fi
  [[ "$pause_reporting_active" == 1 ]] && reporting_flags+=(--pause-reporting-active)
  [[ "$drain_reporting_start" == 1 ]] && reporting_flags+=(--drain-reporting-start)
  [[ "$barrier_reporting_start" == 1 ]] && reporting_flags+=(--barrier-reporting-start)
  media_flags=(--cold-media "$cold_media")
  if [[ "$cold_media" == ssd ]]; then media_flags+=(--wait-mib "$ssd_wait_mib" --chunk-mib "$ssd_chunk_mib"); fi
  if [[ "$adaptive_mode" == 1 ]]; then
    media_flags+=(--adaptive --latency-limit "$latency_limit" --initial-mib "$adaptive_initial" --active-headroom-mib "$active_headroom" --reference-profile /tmp/reference-profile.json --file-wait-s "$file_wait" --anon-wait-s "$anon_wait")
  fi
  sudo python3 /tmp/controller.py "${media_flags[@]}" --config /tmp/controller-config.json \
    --socket /mnt/workload/tmp/controller.sock --log /mnt/workload/out/controller.jsonl \
    --budget-mib "$phase_budget_mib" --startup-headroom-mib "$startup_headroom" --fast-node "$fast_node" --slow-node "$slow_node" \
    "${no_events[@]}" "${reporting_flags[@]}" >/tmp/controller.log 2>/tmp/controller.err &
  echo $! > /tmp/controller.pid
  for _ in $(seq 1 100); do
    [[ -S /mnt/workload/tmp/controller.sock ]] && break
    sleep .1
  done
  test -S /mnt/workload/tmp/controller.sock
  if [[ "$cold_media" == cxl ]]; then
  sudo python3 /tmp/collect.py --output /mnt/workload/out/diagnostics --slots 1 --stop /tmp/diag-stop >/tmp/diag-collector.log 2>/tmp/diag-collector.err &
  fi
  fallback_node=$slow_node
  [[ "$cold_media" == ssd ]] && fallback_node=$fast_node
  printf '%s\n' "$fallback_node" | sudo tee /mnt/workload/tmp/phase-slow-node >/dev/null
fi
numactl -H
cat /proc/meminfo
cat /proc/vmstat
sysctl kernel.numa_balancing vm.demote_scale_factor 2>/dev/null || true
cat /sys/kernel/mm/numa/demotion_enabled 2>/dev/null || true
cat /sys/devices/system/node/has_cpu /sys/devices/system/node/has_memory 2>/dev/null || true
if [[ "$method" == demeter && "$cold_media" == cxl ]]; then
  grep -H . /sys/module/demeter_placement/parameters/{split_period_ms,rtree_split_thresh,rtree_exch_thresh,initial_range_size_mb,adaptive_range,load_latency_sample_period,retired_stores_sample_period}
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

if [[ "${REPLAY_CREATE_SCRATCHPAD:-0}" == 1 ]]; then "${ssh[@]}" 'sudo mkdir -p /mnt/workload/tmp/scratchpad'; fi
"${scp[@]}" "$(dirname "$0")/guest_state.py" clear@${guest_ip}:/tmp/capacity_guest.py
monitor_order_flags=()
[[ "${WAIT_REPORTING_ORDER:--1}" != -1 ]] && monitor_order_flags+=(--wait-order "$WAIT_REPORTING_ORDER")
sudo -n python3 "$here/phase/PhaseTieing_concurrent_v11/host_monitor.py" \
  --api "$api_socket" --output "$result_dir/host-capacity.jsonl" \
  --guest "$guest_ip" --key "$key" --stop "$result_dir/capacity-stop" \
  --expected-zones "${HOST_FAST_NUMA_NODE:?},${HOST_SLOW_NUMA_NODE:?}" --expected-reporting "${FREE_PAGE_REPORTING:-off}" --expected-order "$reporting_order" --ready "$result_dir/capacity-ready.json" \
  "${monitor_order_flags[@]}" --expected-receipts "${DEMETER_REPORTING_RECEIPTS:-1}" --policy "${CAPACITY_POLICY:-static}" --fast-mib "$fast_mib" --slow-mib "$slow_mib" \
  >"$result_dir/host-monitor.log" 2>"$result_dir/host-monitor.err" &
monitor_pid=$!
for _ in $(seq 1 100); do
  [[ -f "$result_dir/capacity-ready.json" ]] && break
  kill -0 "$monitor_pid" 2>/dev/null || break
  sleep .1
done
test -f "$result_dir/capacity-ready.json" || { echo "capacity config preflight failed" >&2; exit 97; }
sudo -n python3 "$here/phase/all_traces_v67/relocate_private.py" --api "$api_socket" \
  --node "${VMM_PRIVATE_NODE:?}" --output "$result_dir/vmm-private-placement.json" \
  >"$result_dir/vmm-private-placement.log" 2>"$result_dir/vmm-private-placement.err"
if [[ -n "${REPLAY_BARRIER_DIR:-}" ]]; then
  printf '%s\n' "$guest_ip" >"$REPLAY_BARRIER_DIR/lane$lane.ready"
  for _ in $(seq 1 $(( ${REPLAY_READY_TIMEOUT_S:-600} * 10 ))); do
    [[ -f "$REPLAY_BARRIER_DIR/abort" ]] && exit 98
    [[ -f "$REPLAY_BARRIER_DIR/lane$lane.go" ]] && break
    sleep .1
  done
  test -f "$REPLAY_BARRIER_DIR/lane$lane.go" || { echo "replay barrier timeout" >&2; exit 98; }
fi
set +e
"${ssh[@]}" 'bash -s' -- "$method" "$seed" "$trigger_bytes" "$speed" "$replay_timeout_s" "$phase_mode" "${REPLAY_SEARCH_BACKEND:-ripgrep}" "${PHASE_COUNTERS:-0}" "${BIND_TOOL_NODE:--1}" "$policy" "${REPLAY_TIMING:-original}" "$cold_media" "${REPLAY_USE_IMAGE_TEST_ENV:-0}" "$proxy_url" <<'REMOTE_RUN' >"$result_dir/replay.log" 2>"$result_dir/replay.err"
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
replay_timing=${11}
COLD_MEDIA=${12}
use_image_test_env=${13}
network_proxy=${14:-}
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
tool_path=/usr/local/bin:/opt/conda/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin
selected_tool_env=existing-base
if [[ "$use_image_test_env" == 1 && -x /mnt/workload/opt/conda/envs/testbed/bin/python ]]; then
  tool_path=/opt/conda/envs/testbed/bin:$tool_path
  selected_tool_env=image-testbed
fi
sudo python3 - "$use_image_test_env" "$selected_tool_env" "$tool_path" "$network_proxy" <<'TOOL_ENV'
import json,sys
from pathlib import Path
Path('/mnt/workload/out/tool-environment.json').write_text(json.dumps(dict(requested_image_test_env=int(sys.argv[1]),selected=sys.argv[2],path=sys.argv[3],network_proxy=sys.argv[4] or None),indent=2)+'\n')
TOOL_ENV
network_env=()
if [[ -n "$network_proxy" ]]; then network_env=(HTTP_PROXY="$network_proxy" HTTPS_PROXY="$network_proxy" http_proxy="$network_proxy" https_proxy="$network_proxy" NO_PROXY=localhost,127.0.0.1); fi
replay=(chroot /mnt/workload /usr/bin/env "${network_env[@]}"
  PATH="$tool_path"
  REPLAY_SEARCH_BACKEND="${REPLAY_SEARCH_BACKEND:-ripgrep}"
  HOME=/root LANG=C.UTF-8 LC_ALL=C.UTF-8
  EXPECTED_MEMORY_POLICY="$policy"
  /opt/conda/bin/python /tmp/replay_agentcgroup.py /tmp/tool_calls.json
  --output /out/result.json
  --cgroup /sys/fs/cgroup/agent-workload --join-cgroup
  --timing "$replay_timing" --speed "$speed" --disable-command-timeouts)
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
    /tmp/demeter -- "${replay[@]}" --demeter-tree --demeter-tree-slots 31
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
if [[ "$policy" == phase && "${COLD_MEDIA:-cxl}" == cxl ]]; then
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

if [[ -n "${REPLAY_FINISH_BARRIER_DIR:-}" ]]; then
  printf '%s\n' "$run_status" >"$REPLAY_FINISH_BARRIER_DIR/lane$lane.done"
  while [[ ! -f "$REPLAY_FINISH_BARRIER_DIR/export.go" ]]; do
    [[ -f "$REPLAY_FINISH_BARRIER_DIR/export.go" ]] && break
    [[ -f "$REPLAY_FINISH_BARRIER_DIR/abort" ]] && exit 98
    sleep .1
  done
  test -f "$REPLAY_FINISH_BARRIER_DIR/export.go" || exit 98
fi

"${ssh[@]}" 'sudo python3 -' <<'PACKAGES'
import email.parser,glob,json
from pathlib import Path
root=Path('/mnt/workload');result={}
patterns=['opt/conda/lib/python*/site-packages','opt/conda/envs/*/lib/python*/site-packages','usr/local/lib/python*/site-packages','testbed/venv/lib/python*/site-packages','testbed/.venv/lib/python*/site-packages','testbed/testenv/lib/python*/site-packages','testbed/env/lib/python*/site-packages']
for pattern in patterns:
    for directory in glob.glob(str(root/pattern)):
        rows=[]
        for metadata in Path(directory).glob('*.dist-info/METADATA'):
            data=email.parser.Parser().parsestr(metadata.read_text(errors='replace'),headersonly=True)
            if data.get('Name') and data.get('Version'):rows.append([data['Name'].lower().replace('_','-'),data['Version']])
        result[str(Path(directory).relative_to(root))]=sorted(rows)
(root/'out/environment-packages.json').write_text(json.dumps(result,sort_keys=True,indent=2)+'\n')
PACKAGES

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
