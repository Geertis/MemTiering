#!/usr/bin/env bash
set -euo pipefail

if [[ $# != 4 && $# != 5 ]]; then
  echo "usage: $0 CGROUP SEED TRIGGER_BYTES OUTPUT_JSONL [READY_FILE]" >&2
  exit 2
fi

cgroup=$1
seed=$2
trigger_bytes=$3
output=$4
ready_file=${5:-}
randomizer=${RANDOMIZER:-/tmp/randomize_pages}
poll_seconds=${POLL_SECONDS:-0.02}
fast_percent=${FAST_PERCENT:-20}
fast_node=${FAST_NODE:-1}
slow_node=${SLOW_NODE:-2}

[[ -d "$cgroup" && -x "$randomizer" ]] || exit 2
: >"$output"
seen_workload=0
while true; do
  current=$(cat "$cgroup/memory.current")
  pids=$(cat "$cgroup/pids.current")
  if (( pids > 0 )); then seen_workload=1; fi
  if (( current >= trigger_bytes && pids > 0 )); then break; fi
  if (( seen_workload == 1 && pids == 0 )); then
    echo '{"error":"workload-exited-before-trigger"}' >>"$output"
    exit 1
  fi
  sleep "$poll_seconds"
done

frozen=0
cleanup() {
  if (( frozen == 1 )); then echo 0 >"$cgroup/cgroup.freeze" || true; fi
}
trap cleanup EXIT
echo 1 >"$cgroup/cgroup.freeze"
frozen=1
for _ in $(seq 1 500); do
  grep -q '^frozen 1$' "$cgroup/cgroup.events" && break
  sleep 0.01
done
grep -q '^frozen 1$' "$cgroup/cgroup.events"

index=0
partial_failures=0
while read -r pid; do
  [[ -n "$pid" ]] || continue
  derived_seed=$((seed + index * 0x9e3779b9))
  if ! "$randomizer" "$pid" "$derived_seed" "$fast_percent" "$fast_node" "$slow_node" \
    >>"$output"; then
    partial_failures=$((partial_failures + 1))
  fi
  index=$((index + 1))
done <"$cgroup/cgroup.procs"

printf '{"summary":true,"processes":%d,"partial_failures":%d}\n' \
  "$index" "$partial_failures" >>"$output"

echo 0 >"$cgroup/cgroup.freeze"
frozen=0
if [[ -n "$ready_file" ]]; then
  cp /proc/vmstat "${output%.jsonl}.vmstat"
  printf 'ready\n' >"$ready_file"
fi
