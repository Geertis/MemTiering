#!/usr/bin/env bash
set -euo pipefail

method=${1:?usage: start-vm.sh METHOD}
fast_mib=${FAST_MIB:-640}
slow_mib=${SLOW_MIB:-4096}
vcpus=${VCPUS:-4}
guest_cpu_with_fast=${GUEST_CPU_WITH_FAST:-0}
host_cpu_start=${HOST_CPU_START:-0}
host_slow_numa_node=${HOST_SLOW_NUMA_NODE:-2}
host_fast_numa_node=${HOST_FAST_NUMA_NODE:-0}
if [[ ! -d /sys/devices/system/node/node${host_slow_numa_node} ]]; then
  echo "host NUMA node ${host_slow_numa_node} is unavailable" >&2
  exit 2
fi
vm_tap=${VM_TAP:-demeter0}
vm_mac=${VM_MAC:-2e:89:a8:e4:92:00}
base=${PHASE_TIEING_ROOT:?}
repo="$base/backend"
here="$base/.state/run"
vm_api_socket=${VM_API_SOCKET:-$here/cloud-hypervisor-$method.sock}
workload_disk=${WORKLOAD_DISK:-astroid-rootfs.ext4}
root_disk=${ROOT_DISK:-$run/root-$method.qcow2}
if [[ "$workload_disk" != /* ]]; then
  workload_disk="$here/$workload_disk"
fi
workload_disk_arg="path=$workload_disk"
if [[ ${WORKLOAD_READONLY:-0} == 1 ]]; then
  workload_disk_arg+=",readonly=on"
fi

case "$method" in
  demeter|noop) kernel=demeter ;;
  tpp) kernel=tpp ;;
  *) echo "unknown method: $method" >&2; exit 2 ;;
esac
kernel_image=${DEMETER_KERNEL_IMAGE:-$repo/build/$kernel/arch/x86/boot/compressed/vmlinux.bin}
extra_workload_disks=()
if [[ -n ${SSD_SWAP_DISK:-} ]]; then
  test -f "$SSD_SWAP_DISK"
  extra_workload_disks+=("path=$SSD_SWAP_DISK,direct=on,id=ssd-swap")
fi
if [[ -n ${EXTRA_WORKLOAD_DISKS:-} ]]; then
  IFS=: read -r -a extra_disk_paths <<< "$EXTRA_WORKLOAD_DISKS"
  for disk_path in "${extra_disk_paths[@]}"; do
    [[ -f "$disk_path" ]] || { echo "missing workload disk: $disk_path" >&2; exit 2; }
    extra_workload_disks+=("path=$disk_path")
  done
fi

fast_raw_mib=${FAST_RAW_MIB:-16384}
slow_raw_mib=${SLOW_RAW_MIB:-16384}
reporting=${FREE_PAGE_REPORTING:-off}
[[ "$fast_raw_mib" =~ ^[1-9][0-9]*$ && "$slow_raw_mib" =~ ^[1-9][0-9]*$ && "$reporting" =~ ^(on|off)$ ]] || exit 2
(( fast_raw_mib >= fast_mib && slow_raw_mib >= slow_mib )) || exit 2
fast_zone=$((fast_raw_mib * 1024 * 1024))
slow_zone=$((slow_raw_mib * 1024 * 1024))
fast_balloon=$((fast_zone - fast_mib * 1024 * 1024))
slow_balloon=$((slow_zone - slow_mib * 1024 * 1024))

cpu_config="boot=$vcpus"
if [[ $vcpus == 4 ]]; then
  host_cpu_end=$((host_cpu_start + 3))
  cpu_config+=",affinity=[0@[$host_cpu_start-$host_cpu_end],1@[$host_cpu_start-$host_cpu_end],2@[$host_cpu_start-$host_cpu_end],3@[$host_cpu_start-$host_cpu_end]]"
fi

if [[ "${GUEST_SYSTEM_TIER:-fast}" == slow ]]; then
  numa_config=(
    'guest_numa_id=1,memory_zones=[dram]'
    'guest_numa_id=2,cpus=[0-3],memory_zones=[slow]'
  )
elif [[ "$guest_cpu_with_fast" == 1 ]]; then
  numa_config=(
    'guest_numa_id=1,cpus=[0-3],memory_zones=[dram]'
    'guest_numa_id=2,memory_zones=[slow]'
  )
else
  numa_config=(
    'guest_numa_id=0,cpus=[0-3]'
    'guest_numa_id=1,memory_zones=[dram]'
    'guest_numa_id=2,memory_zones=[slow]'
  )
fi

exec sudo -n env DEMETER_REPORTING_RECEIPTS="${DEMETER_REPORTING_RECEIPTS:-1}" numactl --membind="${VMM_PRIVATE_NODE:?}" "$repo/bin/cloud-hypervisor" \
  --cpus "$cpu_config" \
  --memory size=0,shared=on \
  --memory-zone id=dram,size=$fast_zone,shared=on,host_numa_node=$host_fast_numa_node \
                id=slow,size=$slow_zone,shared=on,host_numa_node=$host_slow_numa_node \
  --numa "${numa_config[@]}" \
  --balloon "size=[$fast_balloon,$slow_balloon],heterogeneous_memory=on,free_page_reporting=$reporting" \
  --kernel "$kernel_image" \
  --cmdline 'root=/dev/vda2 rw console=ttyS0 modprobe.blacklist=virtio_balloon transparent_hugepage=never cgroup_no_v1=all sysctl.vm.mem_profiling=never' \
  --disk path="$root_disk" "$workload_disk_arg" "${extra_workload_disks[@]}" \
  --net "tap=$vm_tap,mac=$vm_mac" \
  --console off --serial tty --api-socket "path=$vm_api_socket"
