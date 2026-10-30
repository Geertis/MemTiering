#!/usr/bin/env python3
"""Replay AgentCgroup tool calls inside a clean Demeter guest rootfs.

This driver intentionally has no container-engine dependency.  It is copied
into a digest-pinned workload rootfs and executed after the VM harness has made
that rootfs clean.  A parent session cgroup can be sampled every 100 ms; child
processes inherit the cgroup automatically.
"""

from __future__ import annotations

import argparse
import ctypes
import glob
import hashlib
import json
import multiprocessing
import os
import re
import shlex
import socket
import subprocess
import tempfile
import threading
import time
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from typing import Any


#!/usr/bin/env python3
"""Optional real tool counters. Unsupported events stay unavailable, not zero."""
import ctypes
import fcntl
import os
import platform
import struct
import time

# perf_event.h: HW cycles/instructions and LL/read/{access,miss}.
EVENTS={'cycles':(0,0),'instructions':(0,1),'ll_read_access':(3,2),'ll_read_miss':(3,2|(1<<16))}

class PhaseCounters:
    def __init__(self, granite_raw=False):
        self.fds={};self.errors={};self.started=0
        self.events=dict(EVENTS)
        if granite_raw:
            self.events.update(retired_load=(4,0x81d0), retired_load_l3_miss=(4,0x20d1))
    def start(self):
        if platform.machine()!='x86_64':
            self.errors['architecture']='x86_64 syscall adapter required';return self
        libc=ctypes.CDLL(None,use_errno=True);libc.syscall.restype=ctypes.c_long
        for name,(kind,config) in self.events.items():
            attr=bytearray(128)
            # Disabled, inherit forked tools, user-space only, timing metadata.
            struct.pack_into('IIQQQQQ',attr,0,kind,128,config,0,0,3,1|2|(1<<5)|(1<<6))
            buf=(ctypes.c_ubyte*128).from_buffer(attr)
            fd=libc.syscall(298,ctypes.byref(buf),0,-1,-1,8) # CLOEXEC
            if fd<0:
                err=ctypes.get_errno();self.errors[name]={'errno':err,'message':os.strerror(err)};continue
            try:
                fcntl.ioctl(fd,0x2403,0);fcntl.ioctl(fd,0x2400,0)
                self.fds[name]=fd
            except OSError as error:
                self.errors[name]=repr(error);os.close(fd)
        self.started=time.monotonic_ns();return self
    def finish(self):
        output={'scope':'calling process and descendants forked during this tool; user-space only',
                'wall_ns':time.monotonic_ns()-self.started if self.started else 0,
                'events':{},'errors':self.errors}
        for name,fd in self.fds.items():
            try:
                fcntl.ioctl(fd,0x2401,0)
                data=os.read(fd,24)
                if len(data)!=24:raise ValueError('short perf read')
                value,enabled,running=struct.unpack('QQQ',data)
                output['events'][name]={'raw':value,'time_enabled_ns':enabled,'time_running_ns':running,
                    'running_fraction':running/enabled if enabled else None,
                    'scaled':value*enabled/running if running else None}
            except (OSError,ValueError) as error:output['errors'][name]=repr(error)
            finally:os.close(fd)
        self.fds.clear()
        ref=output['events'].get('ll_read_access',{});miss=output['events'].get('ll_read_miss',{})
        # Independently multiplexed events need adequate exposure. This is a
        # generic LL read-event ratio, not a measured DDR/CXL request fraction.
        reliable=all(x.get('running_fraction',0) is not None and x.get('running_fraction',0)>=.8 for x in [ref,miss])
        if reliable and ref.get('scaled'):
            output['ll_read_miss_ratio']=miss['scaled']/ref['scaled']
            output['ll_ratio_valid']=0<=output['ll_read_miss_ratio']<=1
        else:output['ll_read_miss_ratio']=None;output['ll_ratio_valid']=False
        ref=output['events'].get('retired_load',{});miss=output['events'].get('retired_load_l3_miss',{})
        reliable=all(x.get('running_fraction',0) is not None and x.get('running_fraction',0)>=.8 for x in [ref,miss])
        output['retired_load_l3_miss_ratio']=miss['scaled']/ref['scaled'] if reliable and ref.get('scaled') else None
        output['retired_load_l3_ratio_valid']=output['retired_load_l3_miss_ratio'] is not None and 0<=output['retired_load_l3_miss_ratio']<=1
        output['raw_event_source']='Intel perfmon GNR v1.21; ALL_LOADS includes software prefetch; L3_MISS counts instructions with at least one missing uop' if 'retired_load' in self.events else None
        return output



REPLAYABLE = {"Bash", "Read", "Edit", "Write", "Glob", "Grep"}


def phase_kind(tool: str, values: dict[str, Any]) -> int:
    command = str(values.get("command", "")).lower()
    if tool in ("Read", "Glob", "Grep") or re.search(r"\b(grep|rg|find)\b", command):
        return 1
    if re.search(r"\b(pytest|unittest|tox|nosetests)\b|\btest[s]?\b", command):
        return 3
    if re.search(r"\b(pip|make|cmake|gcc|clang|build|compile)\b", command):
        return 2
    return 0


class PhaseRuntime:
    """Emit real tool boundaries before allocation; never read future trace data."""

    def __init__(self, path: Path, sandbox_id: str, slow_node: int):
        self.path, self.sandbox_id, self.slow_node = path, sandbox_id, slow_node
        self.errors: list[str] = []
        self.last_response: dict[str, Any] = {}
        self.metadata = {}
        self.completed = None

    def notify(self, event: str, kind: int = 0) -> dict[str, Any]:
        try:
            with socket.socket(socket.AF_UNIX) as connection:
                connection.settimeout(2)
                connection.connect(str(self.path))
                connection.sendall(json.dumps({"sandbox": self.sandbox_id, "event": event,
                                               "kind": kind, **self.metadata, "completed": self.completed}).encode() + b"\n")
                response = json.loads(connection.makefile("rb").readline(8192))
                if "error" in response:
                    raise RuntimeError(response["error"])
        except (OSError, ValueError, RuntimeError) as error:
            self.errors.append(repr(error))
            response = {"preferred_node": self.slow_node, "degraded": True}
        node = int(response["preferred_node"])
        if not 0 <= node < 64:
            raise ValueError(f"unsupported preferred NUMA node: {node}")
        mask = ctypes.c_ulong(1 << node)
        # x86_64 Linux SYS_set_mempolicy, MPOL_PREFERRED. Descendants inherit
        # this policy at fork/exec; existing mappings retain their identity.
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.syscall(238, 1, ctypes.byref(mask), ctypes.c_ulong(64)) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        self.last_response = response
        return response


def check_weighted_interleave() -> None:
    """Verify that the launcher set the policy before Python started."""
    mode = ctypes.c_int(-1)
    mask = ctypes.c_ulong(0)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.syscall(239, ctypes.byref(mode), ctypes.byref(mask), 64, None, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    nodes: list[int] = []
    for part in Path("/sys/devices/system/node/has_memory").read_text().strip().split(","):
        bounds = [int(value) for value in part.split("-")]
        nodes.extend(range(bounds[0], bounds[-1] + 1))
    expected = sum(1 << node for node in nodes)
    if mode.value != 6 or mask.value != expected:
        raise RuntimeError(
            f"weighted interleave not inherited: mode={mode.value} mask={mask.value:#x}, "
            f"expected mask={expected:#x}"
        )


class DemeterProcessTree:
    """Assign short-lived descendants to independent Demeter target slots."""

    def __init__(self, slots: int, interval: float, slot_start: int = 1, include_self: bool = False):
        self.targets = Path("/sys/kernel/mm/demeter/targets")
        self.slots = slots
        self.interval = interval
        self.slot_start = slot_start
        self.include_self = include_self
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.assigned: dict[int, int] = {}
        self.registrations = 0
        self.missed_no_slot = 0
        self.write_errors = 0
        self.write_error_codes: dict[int, int] = {}

    @staticmethod
    def _children(pid: int) -> list[int]:
        try:
            raw = Path(f"/proc/{pid}/task/{pid}/children").read_text()
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            return []
        return [int(value) for value in raw.split()]

    def _descendants(self) -> set[int]:
        found: set[int] = {os.getpid()} if self.include_self else set()
        pending = [os.getpid()]
        while pending:
            for pid in self._children(pending.pop()):
                if pid not in found:
                    found.add(pid)
                    pending.append(pid)
        return found

    def _write(self, slot: int, pid: int) -> None:
        (self.targets / str(slot) / "pid").write_text(str(pid))

    def _record_write_error(self, error: OSError) -> None:
        self.write_errors += 1
        code = error.errno or 0
        self.write_error_codes[code] = self.write_error_codes.get(code, 0) + 1

    def _run(self) -> None:
        while not self.stop_event.is_set():
            descendants = self._descendants()
            for pid, slot in list(self.assigned.items()):
                if pid not in descendants:
                    try:
                        self._write(slot, -1)
                    except OSError as error:
                        self._record_write_error(error)
                    del self.assigned[pid]
            free = iter(
                slot
                for slot in range(self.slot_start, self.slot_start + self.slots)
                if slot not in self.assigned.values()
            )
            for pid in sorted(descendants - self.assigned.keys()):
                slot = next(free, None)
                if slot is None:
                    self.missed_no_slot += 1
                    break
                try:
                    self._write(slot, pid)
                except OSError as error:
                    self._record_write_error(error)
                    continue
                self.assigned[pid] = slot
                self.registrations += 1
            self.stop_event.wait(self.interval)

    def __enter__(self) -> "DemeterProcessTree":
        available = int((self.targets / "nr_targets").read_text())
        if available < self.slot_start + self.slots:
            raise RuntimeError(
                f"Demeter has {available} slots; need {self.slot_start + self.slots}"
            )
        self.thread = threading.Thread(
            target=self._run, name="demeter-process-tree", daemon=True
        )
        self.thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop_event.set()
        assert self.thread is not None
        self.thread.join()
        for slot in sorted(self.assigned.values()):
            try:
                self._write(slot, -1)
            except OSError as error:
                self._record_write_error(error)
        self.assigned.clear()


def parse_timestamp(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def read_key_values(path: Path) -> dict[str, int | str]:
    values: dict[str, int | str] = {}
    try:
        for line in path.read_text().splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            value = parts[1]
            values[parts[0]] = int(value) if value.isdigit() else value
    except OSError:
        pass
    return values


def read_scalar(path: Path) -> int | str | None:
    try:
        value = path.read_text().strip()
    except OSError:
        return None
    return int(value) if value.isdigit() else value


def read_numa_stat(path: Path) -> dict[str, dict[str, int]]:
    values: dict[str, dict[str, int]] = {}
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return values
    for line in lines:
        parts = line.split()
        if not parts:
            continue
        nodes: dict[str, int] = {}
        for item in parts[1:]:
            if "=" not in item:
                continue
            node, raw_value = item.split("=", 1)
            try:
                nodes[node] = int(raw_value)
            except ValueError:
                continue
        values[parts[0]] = nodes
    return values


def recursive_pid_count(cgroup: Path) -> int:
    pids: set[int] = set()
    for procs in [cgroup / "cgroup.procs", *cgroup.glob("**/cgroup.procs")]:
        try:
            pids.update(int(value) for value in procs.read_text().split())
        except (OSError, ValueError):
            continue
    return len(pids)


class CgroupSampler:
    def __init__(self, cgroup: Path | None, interval: float):
        self.cgroup = cgroup
        self.interval = interval
        self.samples: list[dict[str, Any]] = []
        context = multiprocessing.get_context("fork")
        self.stop_event = context.Event()
        self.phase = context.Array("c", 256)
        self.process: multiprocessing.Process | None = None
        self.spool: Path | None = None
        self.set_phase(-1, "idle")

    def set_phase(self, index: int, tool: str) -> None:
        payload = json.dumps({"tool_index": index, "tool": tool}).encode()
        with self.phase.get_lock():
            self.phase.value = payload

    def start(self) -> None:
        if self.cgroup is None:
            return
        if not (self.cgroup / "cgroup.procs").is_file():
            raise FileNotFoundError(f"not a cgroup v2 directory: {self.cgroup}")
        descriptor, name = tempfile.mkstemp(prefix="demeter-samples-", suffix=".jsonl")
        os.close(descriptor)
        self.spool = Path(name)
        self.process = multiprocessing.get_context("fork").Process(
            target=self._run, name="cgroup-sampler", daemon=True
        )
        self.process.start()

    def _run(self) -> None:
        assert self.cgroup is not None
        assert self.spool is not None
        deadline = time.monotonic()
        with self.spool.open("w", encoding="utf-8") as handle:
            while not self.stop_event.is_set():
                now_mono = time.monotonic()
                with self.phase.get_lock():
                    phase = json.loads(self.phase.value.decode())
                sample = {
                    "monotonic_s": now_mono,
                    "epoch_s": time.time(),
                    **phase,
                    "memory_current": read_scalar(self.cgroup / "memory.current"),
                    "memory_peak": read_scalar(self.cgroup / "memory.peak"),
                    "pids_current": read_scalar(self.cgroup / "pids.current"),
                    "recursive_pid_count": recursive_pid_count(self.cgroup),
                    "cpu_stat": read_key_values(self.cgroup / "cpu.stat"),
                    "memory_events": read_key_values(self.cgroup / "memory.events"),
                    "memory_numa_stat": read_numa_stat(self.cgroup / "memory.numa_stat"),
                }
                handle.write(json.dumps(sample, separators=(",", ":")) + "\n")
                deadline += self.interval
                now_after_sample = time.monotonic()
                if deadline <= now_after_sample:
                    deadline = now_after_sample + self.interval
                self.stop_event.wait(deadline - now_after_sample)

    def stop(self) -> None:
        if self.process is None:
            return
        self.stop_event.set()
        self.process.join()
        self.process = None
        assert self.spool is not None
        self.samples = [json.loads(line) for line in self.spool.read_text().splitlines()]
        self.spool.unlink()
        self.spool = None


def command_result(completed: subprocess.CompletedProcess[bytes]) -> dict[str, Any]:
    # Keep a bounded diagnostic tail.  Hashes remain the comparison primitive;
    # the tail makes environment-mismatch calibration auditable without rerunning
    # a mutable guest disk by hand.
    return {
        "status": completed.returncode,
        "stdout_bytes": len(completed.stdout),
        "stderr_bytes": len(completed.stderr),
        "stdout_sha256": hashlib.sha256(completed.stdout).hexdigest(),
        "stderr_sha256": hashlib.sha256(completed.stderr).hexdigest(),
        "stdout_tail": completed.stdout[-8192:].decode(errors="replace"),
        "stderr_tail": completed.stderr[-8192:].decode(errors="replace"),
    }


class Replayer:
    def __init__(self, args: argparse.Namespace, calls: list[dict[str, Any]]):
        self.args = args
        policy = os.environ.get("EXPECTED_MEMORY_POLICY", "phase")
        mode = ctypes.c_int(-1)
        mask = ctypes.c_ulong(0)
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.syscall(239, ctypes.byref(mode), ctypes.byref(mask), 64, None, 0) != 0:
            raise OSError(ctypes.get_errno(), "initial get_mempolicy failed")
        self.initial_policy = {"expected":policy,"mode":mode.value,"mask":mask.value}
        expected = {"dram":(2,1),"cxl":(2,2),"static":(6,3)}.get(policy)
        if expected is not None and (mode.value,mask.value) != expected:
            raise RuntimeError(f"initial policy mismatch: {self.initial_policy}")
        self.calls = calls
        self.sampler = CgroupSampler(args.cgroup, args.sample_interval)
        self.started_mono = 0.0
        self.original_start = parse_timestamp(calls[0]["timestamp"]) if calls else 0.0
        self.expected_outcomes: dict[str, bool] = getattr(args, "expected_outcomes", {})
        # Claude Code's Bash tool keeps a session environment across calls.
        # Preserve simple exported variables while still executing each recorded
        # command verbatim and retaining its real exit status.
        self.shell_env = os.environ.copy()
        self.search_backend = os.environ.get("REPLAY_SEARCH_BACKEND", "ripgrep")
        if self.search_backend not in {"legacy", "ripgrep"}:
            raise ValueError("unknown search backend")
        if Path("/opt/replay-deps").is_dir():
            self.shell_env["PYTHONPATH"] = "/opt/replay-deps" + (
                ":" + self.shell_env["PYTHONPATH"] if self.shell_env.get("PYTHONPATH") else "")
        if Path("/opt/replay-deps/vmm_pytest_compat.py").exists():
            self.shell_env["PYTEST_PLUGINS"] = "vmm_pytest_compat"
        phase_socket = getattr(args, "phase_socket", None)
        self.phase_runtime = (PhaseRuntime(phase_socket, args.sandbox_id, args.phase_slow_node)
                              if phase_socket else None)

    def _persist_exports(self, command: str) -> list[str]:
        persisted: list[str] = []
        pattern = re.compile(
            r"(?:^|(?:&&|;)\s*)export\s+([A-Za-z_][A-Za-z0-9_]*)=(.*?)"
            r"(?=\s*(?:&&|;|$))",
            re.DOTALL,
        )
        for match in pattern.finditer(command):
            key, raw_value = match.group(1), match.group(2).strip()
            try:
                parts = shlex.split(raw_value)
                value = parts[0] if len(parts) == 1 else raw_value
            except ValueError:
                value = raw_value.strip("'\"")
            value = re.sub(
                r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))",
                lambda item: self.shell_env.get(item.group(1) or item.group(2), ""),
                value,
            )
            self.shell_env[key] = value
            persisted.append(key)
        return persisted

    def replay(self) -> dict[str, Any]:
        os.chdir(self.args.workdir)
        original_head_result = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True
        )
        original_head = (
            original_head_result.stdout.strip() if original_head_result.returncode == 0 else None
        )
        # Start monitoring before joining the workload cgroup so the sampler
        # remains outside the measured Sandbox when --join-cgroup is used.
        self.sampler.start()
        if self.args.join_cgroup:
            if self.args.cgroup is None:
                raise ValueError("--join-cgroup requires --cgroup")
            (self.args.cgroup / "cgroup.procs").write_text(str(os.getpid()))
        if getattr(self.args, "static_weighted_interleave", False):
            check_weighted_interleave()
        ready_file = getattr(self.args, "static_ready_file", None)
        if ready_file is not None:
            deadline = time.monotonic() + 120
            while not ready_file.exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError("initial static placement did not finish")
                time.sleep(0.01)
        self.started_mono = time.monotonic()
        steps: list[dict[str, Any]] = []
        demeter_tree_enabled = getattr(self.args, "demeter_tree", False)
        tree_context = (
            DemeterProcessTree(
                slots=getattr(self.args, "demeter_tree_slots", 31),
                interval=getattr(self.args, "demeter_tree_interval", 0.01),
                slot_start=getattr(self.args, "demeter_tree_slot_start", 1),
                include_self=getattr(self.args, "demeter_tree_self", False),
            )
            if demeter_tree_enabled
            else nullcontext(None)
        )
        tree = None
        try:
            with tree_context as tree:
                for source_index, call in enumerate(self.calls):
                    tool = str(call.get("tool", ""))
                    if tool not in REPLAYABLE:
                        steps.append(
                            {
                                "source_index": source_index,
                                "tool": tool,
                                "skipped": True,
                                "reason": "unsupported-no-sandbox-side-effect"
                                if tool == "TodoWrite"
                                else "unsupported",
                            }
                        )
                        continue
                    print("PHASETIEING_START=" + json.dumps(dict(source_index=source_index,
                        tool=tool, monotonic_s=time.monotonic())), flush=True)
                    step = self._run_step(source_index, call)
                    steps.append(step)
                    print("PHASETIEING_STEP=" + json.dumps({key: step.get(key) for key in
                        ["source_index", "tool", "status", "expected_error", "outcome_match", "elapsed_s", "error"]}), flush=True)
                if self.args.timing == "original" and self.calls:
                    original_end = max(
                        parse_timestamp(call.get("end_timestamp") or call["timestamp"])
                        for call in self.calls
                    )
                    target_end_offset = (original_end - self.original_start) / self.args.speed
                    remaining = self.started_mono + target_end_offset - time.monotonic()
                    if remaining > 0:
                        self.sampler.set_phase(-1, "trace_tail")
                        time.sleep(remaining)
            self.e2e_finished_mono = time.monotonic()
        finally:
            if self.phase_runtime:
                self.phase_runtime.notify("end")
            self.sampler.set_phase(-1, "idle")
            self.sampler.stop()

        diff_command = ["git", "diff", "--binary"]
        if original_head:
            diff_command.append(original_head)
        diff = subprocess.run(diff_command, capture_output=True, cwd=self.args.workdir)
        if getattr(self.args, "output", None):
            self.args.output.with_name("final.patch").write_bytes(diff.stdout)
        final_head_result = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=self.args.workdir
        )
        final_head = final_head_result.stdout.strip() if final_head_result.returncode == 0 else None
        git_status = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            capture_output=True,
            cwd=self.args.workdir,
        )
        status_vector = [step.get("status") for step in steps if not step.get("skipped")]
        checked_outcomes = [
            step["outcome_match"]
            for step in steps
            if not step.get("skipped") and step.get("outcome_match") is not None
        ]
        return {
            "schema_version": 1,
            "command_timeouts_disabled": bool(getattr(self.args, "disable_command_timeouts", False)),
            "initial_memory_policy": self.initial_policy,
            "search_backend": self.search_backend,
            "shell_pythonpath": self.shell_env.get("PYTHONPATH"),
            "source": str(self.args.tool_calls),
            "workdir": str(self.args.workdir),
            "timing": self.args.timing,
            "speed": self.args.speed,
            "static_weighted_interleave": getattr(self.args, "static_weighted_interleave", False),
            "sample_interval_s": self.args.sample_interval,
            "elapsed_s": time.monotonic() - self.started_mono,
            "e2e_started_monotonic_s": self.started_mono,
            "e2e_finished_monotonic_s": self.e2e_finished_mono,
            "e2e_s": self.e2e_finished_mono - self.started_mono,
            "e2e_scope": "sandbox trace replay: initial recorded offset, serial tool service and inter-tool waits; excludes VM boot, model execution and response generation",
            "e2e_wait_protocol": "completion-to-next-start gaps" if self.args.timing == "gaps" else "absolute starts",
            "total_calls": len(self.calls),
            "executed_calls": len(status_vector),
            "skipped_calls": len(self.calls) - len(status_vector),
            "failed_calls": sum(status not in (0, None) for status in status_vector),
            "checked_original_outcomes": len(checked_outcomes),
            "outcome_mismatches": sum(match is False for match in checked_outcomes),
            "status_vector": status_vector,
            "original_git_head": original_head,
            "final_git_head": final_head,
            "git_diff_bytes": len(diff.stdout),
            "git_diff_sha256": hashlib.sha256(diff.stdout).hexdigest(),
            "git_status_bytes": len(git_status.stdout),
            "git_status_sha256": hashlib.sha256(git_status.stdout).hexdigest(),
            "demeter_process_tree": demeter_tree_enabled,
            "demeter_tree_registrations": tree.registrations if tree else 0,
            "demeter_tree_missed_no_slot": tree.missed_no_slot if tree else 0,
            "demeter_tree_write_errors": tree.write_errors if tree else 0,
            "demeter_tree_write_error_codes": tree.write_error_codes if tree else {},
            "phase_aware": self.phase_runtime is not None,
            "phase_runtime_errors": self.phase_runtime.errors if self.phase_runtime else [],
            "steps": steps,
            "resource_samples": [sample for sample in self.sampler.samples
                                 if sample["monotonic_s"] >= self.started_mono],
        }

    def _run_step(self, index: int, call: dict[str, Any]) -> dict[str, Any]:
        tool = str(call["tool"])
        original_offset = parse_timestamp(call["timestamp"]) - self.original_start
        target_offset = original_offset / self.args.speed
        if self.args.timing == "original":
            delay = self.started_mono + target_offset - time.monotonic()
            if delay > 0:
                time.sleep(delay)

        if self.args.timing == "gaps":
            previous = next((c for c in reversed(self.calls[:index]) if c["tool"] in REPLAYABLE), None)
            previous_end = parse_timestamp(previous.get("end_timestamp") or previous["timestamp"]) if previous else self.original_start
            gap = max(0, parse_timestamp(call["timestamp"]) - previous_end) / self.args.speed
            if gap:
                time.sleep(gap)
        actual_offset = time.monotonic() - self.started_mono
        self.sampler.set_phase(index, tool)
        started = time.monotonic()
        error = None
        kind = phase_kind(tool, call.get("input", {}))
        if self.phase_runtime:
            self.phase_runtime.metadata = {
                'signature': hashlib.sha256(json.dumps([tool, call.get('input', {})], sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
                'id': index, 'service_started_s': started, 'e2e_started_s': self.started_mono}
        placement = self.phase_runtime.notify("start", kind) if self.phase_runtime else {}
        bind_node = getattr(self.args, "bind_tool_node", None)
        if bind_node is not None:
            mask = ctypes.c_ulong(1 << bind_node)
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.syscall(238, 2, ctypes.byref(mask), 64) != 0:
                raise OSError(ctypes.get_errno(), "tool MPOL_BIND failed")
        counters = PhaseCounters(granite_raw=getattr(self.args, "granite_raw", False)).start() if getattr(self.args, "phase_counters", False) else None
        execution_started = time.monotonic()
        try:
            result = self._execute(tool, call.get("input", {}))
        except Exception as exc:  # keep replaying so the status vector is auditable
            result = {"status": 125}
            error = repr(exc)
        execution_s = time.monotonic() - execution_started
        counter_result = counters.finish() if counters else None
        if self.phase_runtime:
            self.phase_runtime.notify("end", kind)
        elapsed = time.monotonic() - started
        if self.phase_runtime:
            self.phase_runtime.completed = dict(id=index, signature=self.phase_runtime.metadata['signature'],
                                                service_s=elapsed)
        self.sampler.set_phase(-1, "thinking")
        expected_error = self.expected_outcomes.get(str(call.get("id", "")))
        outcome_match = (
            None
            if expected_error is None
            else ((result.get("status", 125) != 0) == expected_error)
        )
        normalization = None
        if (
            tool == "Bash"
            and expected_error is False
            and result.get("status") == 1
            and result.get("stdout_bytes") == 0
            and re.search(r"(?:^|[|;&]\s*)grep(?:\s|$)", str(call.get("input", {}).get("command", "")))
        ):
            # Claude's Bash wrapper reports grep's ordinary "no matches" exit
            # as a non-error tool result.  Keep status=1 but compare with the
            # recorded wrapper semantics.
            outcome_match = True
            normalization = "grep-no-match-recorded-as-non-error"
        return {
            "source_index": index,
            "tool": tool,
            "original_timestamp": call.get("timestamp"),
            "original_end_timestamp": call.get("end_timestamp"),
            "scheduled_offset_s": target_offset,
            "actual_offset_s": actual_offset,
            "service_started_monotonic_s": started,
            "service_finished_monotonic_s": started + elapsed,
            "schedule_lag_s": actual_offset - target_offset,
            "elapsed_s": elapsed,
            "execution_s": execution_s,
            "phase_overhead_s": elapsed - execution_s if self.phase_runtime else 0,
            "phase_kind": kind,
            "hardware_counters": counter_result,
            "placement_hint": placement,
            "error": error,
            "expected_error": expected_error,
            "outcome_match": outcome_match,
            "outcome_normalization": normalization,
            **result,
        }

    def _execute(self, tool: str, values: dict[str, Any]) -> dict[str, Any]:
        if tool == "Bash":
            timeout_ms = int(values.get("timeout", self.args.command_timeout * 1000))
            timeout = None if getattr(self.args, "disable_command_timeouts", False) else min(timeout_ms / 1000.0, self.args.command_timeout)
            completed = subprocess.run(
                values.get("command", ""),
                shell=True,
                executable="/bin/bash",
                capture_output=True,
                timeout=timeout,
                env=self.shell_env,
            )
            result = command_result(completed)
            result["recorded_timeout_ms"] = values.get("timeout")
            result["applied_timeout_s"] = timeout
            result["persisted_exports"] = self._persist_exports(
                str(values.get("command", ""))
            )
            return result

        if tool == "Read":
            path = Path(values["file_path"])
            offset = max(0, int(values.get("offset", 1)) - 1)
            limit = int(values.get("limit", 2000))
            if offset < 0 or limit < 0:
                raise ValueError("negative Read range")
            import itertools
            with path.open("rb") as handle:
                payload = b"".join(itertools.islice(handle, offset, offset + limit))
            return {"status": 0, "stdout_bytes": len(payload), "stdout_sha256": hashlib.sha256(payload).hexdigest()}

        if tool == "Edit":
            path = Path(values["file_path"])
            old = str(values.get("old_string", ""))
            new = str(values.get("new_string", ""))
            # Match Claude Code's Edit tool: a no-op replacement is a tool
            # error even when old_string exists in the file.
            if old == new:
                raise ValueError("no changes to make: old_string and new_string are identical")
            content = path.read_text()
            occurrences = content.count(old)
            if occurrences == 0:
                raise ValueError(f"old_string not found: {path}")
            replace_all = bool(values.get("replace_all", False))
            if occurrences > 1 and not replace_all:
                raise ValueError(f"old_string is not unique ({occurrences} matches): {path}")
            path.write_text(content.replace(old, new, -1 if replace_all else 1))
            return {"status": 0, "replacements": occurrences if replace_all else 1}

        if tool == "Write":
            path = Path(values["file_path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = str(values.get("content", "")).encode()
            path.write_bytes(payload)
            return {"status": 0, "written_bytes": len(payload)}

        if tool == "Glob":
            pattern = str(values.get("pattern", ""))
            base = str(values.get("path", self.args.workdir))
            combined = pattern if os.path.isabs(pattern) else os.path.join(base, pattern)
            matches = sorted(glob.glob(combined, recursive=True))[:100]
            payload = "\n".join(matches).encode()
            return {"status": 0, "matches": len(matches), "stdout_sha256": hashlib.sha256(payload).hexdigest()}

        if tool == "Grep":
            return self._grep(values)

        raise ValueError(f"unsupported tool: {tool}")

    def _grep(self, values: dict[str, Any]) -> dict[str, Any]:
        if self.search_backend == "ripgrep":
            return self._native_grep(values)
        pattern = re.compile(str(values.get("pattern", "")))
        root = Path(values.get("path", self.args.workdir))
        file_glob = str(values.get("glob", "*"))
        paths = [root] if root.is_file() else root.rglob(file_glob)
        matches: list[str] = []
        for path in paths:
            if not path.is_file():
                continue
            try:
                for line_number, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
                    if pattern.search(line):
                        matches.append(f"{path}:{line_number}:{line}")
                        if len(matches) >= 100:
                            break
            except OSError:
                continue
            if len(matches) >= 100:
                break
        payload = "\n".join(matches).encode()
        return {"status": 0, "matches": len(matches), "stdout_sha256": hashlib.sha256(payload).hexdigest()}

    def _native_grep(self, values: dict[str, Any]) -> dict[str, Any]:
        """Execute bounded native search, respecting ignore rules and tool flags."""
        mode = values.get("output_mode", "files_with_matches")
        args = [getattr(self.args, "rg_path", "/tmp/rg"), "--color", "never", "--no-heading", "--sort", "path"]
        if mode == "files_with_matches":
            args.append("--files-with-matches")
        elif mode == "count":
            args.extend(["--count", "--with-filename"])
        elif mode == "content":
            args.append("--with-filename")
            if values.get("-n", True):
                args.append("--line-number")
            for key in ["-A", "-B", "-C"]:
                if key in values:
                    args.extend([key, str(int(values[key]))])
        else:
            raise ValueError("unknown Grep output mode")
        if values.get("-i"):
            args.append("--ignore-case")
        if values.get("multiline"):
            args.extend(["--multiline", "--multiline-dotall"])
        if values.get("glob"):
            args.extend(["--glob", str(values["glob"])])
        if values.get("type"):
            args.extend(["--type", str(values["type"])])
        args.extend(["--regexp", str(values.get("pattern", "")), "--", str(values.get("path", self.args.workdir))])
        offset = int(values.get("offset", 0))
        limit = int(values.get("head_limit", 100))
        if offset < 0 or limit < 0:
            raise ValueError("negative Grep range")
        # Bound returned output without holding a complete project scan in Python.
        chunks = []
        truncated = False
        with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=self.shell_env) as child:
            for index, line in enumerate(child.stdout):
                if index < offset:
                    continue
                if limit and len(chunks) >= limit:
                    truncated = True
                    child.terminate()
                    break
                chunks.append(line)
            errors = child.stderr.read()
            status = child.wait()
        payload = b"".join(chunks)
        # rg exit 1 means an ordinary no-match tool result.
        return {"status": 0 if status in (0, 1) or truncated else status,
                "matches": len(chunks), "stdout_bytes": len(payload),
                "stdout_sha256": hashlib.sha256(payload).hexdigest(),
                "stderr_tail": errors[-8192:].decode(errors="replace"),
                "search_truncated": truncated, "search_argv": args}


def validate(calls: list[dict[str, Any]]) -> dict[str, Any]:
    tool_counts: dict[str, int] = {}
    errors: list[str] = []
    for index, call in enumerate(calls):
        tool = str(call.get("tool", ""))
        tool_counts[tool] = tool_counts.get(tool, 0) + 1
        for required in ("timestamp", "tool", "input"):
            if required not in call:
                errors.append(f"call {index}: missing {required}")
        if "end_timestamp" not in call:
            errors.append(f"call {index}: missing end_timestamp")
        if not isinstance(call.get("input"), dict):
            errors.append(f"call {index}: input is not an object")
        try:
            parse_timestamp(call.get("timestamp", ""))
            parse_timestamp(call.get("end_timestamp", ""))
        except (TypeError, ValueError):
            errors.append(f"call {index}: invalid timestamp")
    unsupported = {name: count for name, count in tool_counts.items() if name not in REPLAYABLE}
    return {
        "calls": len(calls),
        "tool_counts": tool_counts,
        "unsupported": unsupported,
        "schema_errors": errors,
        "valid": not errors,
    }


def load_expected_outcomes(path: Path | None) -> dict[str, bool]:
    outcomes: dict[str, bool] = {}
    if path is None:
        return outcomes
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid trace JSON on line {line_number}: {exc}") from exc
            content = event.get("message", {}).get("content", [])
            if not isinstance(content, list):
                continue
            for item in content:
                if not isinstance(item, dict) or item.get("type") != "tool_result":
                    continue
                tool_id = item.get("tool_use_id")
                if tool_id:
                    outcomes[str(tool_id)] = bool(item.get("is_error", False))
    return outcomes


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("tool_calls", type=Path)
    parser.add_argument("--workdir", type=Path, default=Path("/testbed"))
    parser.add_argument("--output", type=Path, default=Path("/tmp/agentcgroup-replay.json"))
    parser.add_argument("--timing", choices=("original", "gaps", "none"), default="original")
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--command-timeout", type=float, default=600.0)
    parser.add_argument("--disable-command-timeouts", action="store_true", help="Observe natural completion; recorded timeout fields remain metadata")
    parser.add_argument("--cgroup", type=Path)
    parser.add_argument("--join-cgroup", action="store_true")
    parser.add_argument("--static-weighted-interleave", action="store_true")
    parser.add_argument("--static-ready-file", type=Path)
    parser.add_argument("--sample-interval", type=float, default=0.1)
    parser.add_argument("--demeter-tree", action="store_true")
    parser.add_argument("--demeter-tree-slots", type=int, default=31)
    parser.add_argument("--demeter-tree-interval", type=float, default=0.01)
    parser.add_argument("--demeter-tree-slot-start", type=int, default=1)
    parser.add_argument("--demeter-tree-self", action="store_true")
    parser.add_argument("--rg-path", default="/tmp/rg")
    parser.add_argument("--granite-raw", action="store_true")
    parser.add_argument("--phase-counters", action="store_true")
    parser.add_argument("--bind-tool-node", type=int, choices=range(64))
    parser.add_argument("--phase-socket", type=Path)
    parser.add_argument("--sandbox-id", default="0")
    parser.add_argument("--phase-slow-node", type=int, default=1)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument(
        "--trace",
        type=Path,
        help="raw trace.jsonl used to compare replay success/error outcomes",
    )
    args = parser.parse_args()
    if (
        args.speed <= 0
        or args.sample_interval <= 0
        or args.command_timeout <= 0
        or args.demeter_tree_slots <= 0
        or args.demeter_tree_interval <= 0
    ):
        parser.error("speed, sample interval, and command timeout must be positive")

    calls = json.loads(args.tool_calls.read_text())
    if not isinstance(calls, list):
        raise SystemExit("tool_calls file must contain a JSON array")
    validation = validate(calls)
    if args.validate_only:
        print(json.dumps(validation, indent=2, sort_keys=True))
        raise SystemExit(0 if validation["valid"] else 2)
    if not validation["valid"]:
        raise SystemExit("invalid tool_calls schema; run with --validate-only")

    args.expected_outcomes = load_expected_outcomes(args.trace)
    result = Replayer(args, calls).replay()
    result["validation"] = validation
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2))
    print(
        "REPLAY_SUMMARY="
        + json.dumps({key: value for key, value in result.items() if key not in {"steps", "resource_samples"}}, sort_keys=True)
    )


if __name__ == "__main__":
    main()
