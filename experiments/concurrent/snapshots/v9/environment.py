import os
from pathlib import Path
W=Path('/r0/home/geerties/HW/Demeter/experiments/agentcgroup-demeter')
def environment(case, medium, lane, n, settings, barrier):
    artifact = 'vmm-v66-ssd-compact' if medium == 'ssd' else 'vmm-v67-compact-cxl'
    if case['guard']:
        artifact += '-readguard'
    env = dict(os.environ)
    tmux = Path(f'/tmp/PhaseTieing-formal-tmux-{lane}')
    tmux.mkdir(mode=0o700, exist_ok=True)
    env.update(VM_LANE=str(lane), ROOT_DISK=str(W / 'validated-root-disks/root-demeter.qcow2'),
        PHASE_ARTIFACT_DIR=str(Path(__file__).resolve().parent / 'artifacts' / artifact), COLD_MEDIA=medium,
        BASELINE_POLICY='phase', HOST_FAST_NUMA_NODE='0', HOST_SLOW_NUMA_NODE='0' if medium == 'ssd' else '1',
        VMM_PRIVATE_NODE='0', GUEST_SYSTEM_TIER='fast', GUEST_CPU_WITH_FAST='1',
        FAST_RAW_MIB='6144', SLOW_RAW_MIB='6144', FREE_PAGE_REPORTING='on',
        PAGE_REPORTING_ORDER='5', WAIT_REPORTING_ORDER='0', BARRIER_REPORTING_START='1',
        CAPACITY_POLICY='static', DEMETER_REPORTING_RECEIPTS='1', PHASE_BUDGET_MIB='6144',
        STARTUP_HEADROOM_MIB='0', SB_FAST_NODE='0', SB_SLOW_NODE='1', SB_ALLOCATION='1',
        SB_PLACEMENT='1', SB_RECYCLE_CREDIT='1', SB_SAMPLE_BATCH='1', SB_SAMPLE_WAKEUP='0',
        SB_LIFECYCLE_WAKEUP='0', SB_SAMPLING='0', SB_FUTURE_BENEFIT='1',
        REPLAY_USE_IMAGE_TEST_ENV='1' if case.get('tool_environment_override') == 'image-testbed' else '0',
        PHASE_NO_EVENTS='0', REPLAY_TIMING='gaps', REPLAY_TIMEOUT_S='0',
        REPLAY_SEARCH_BACKEND='ripgrep', PHASE_COUNTERS='0', BIND_TOOL_NODE='-1',
        PYTEST_COMPAT='1' if case.get('pytest_compat') else '0', ADAPTIVE_MODE='1',
        LATENCY_LIMIT='.2', ADAPTIVE_INITIAL_MIB='0', SSD_ACTIVE_HEADROOM_MIB='0',
        REFERENCE_PROFILE=str(settings), SSD_PAGE_CLUSTER='3', SSD_CHUNK_MIB='32',
        ATTACH_COLD_DISK='1', SSD_SWAP_GIB=str(64 // n), REPLAY_NETWORK_PROXY='0',
        REPLAY_READY_TIMEOUT_S='1800', REPLAY_EXPORT_TIMEOUT_S='9000',
        REPLAY_CREATE_SCRATCHPAD='0', REPLAY_BARRIER_DIR=str(barrier),
        REPLAY_FINISH_BARRIER_DIR=str(barrier), TMUX_TMPDIR=str(tmux))
    return env
