"""Cohort physical evidence must include a late failure after partial completion."""


def resource_window(state):
    left=state.get('release_monotonic_ns',0)
    if state.get('control_timeout') and state.get('result')=='control-relative-timeout':
        right=state['control_timeout']['stop_monotonic_ns']
    elif 'all_done_epoch' in state:
        right=state.get('all_done_monotonic_ns')
        if right is None:
            right=left+int((state['all_done_epoch']-state['release_epoch'])*1e9)
    else:
        # Taken before cleanup; includes any observed VMM loss or OOM.
        right=state.get('final_resources',{}).get('monotonic_ns',0)
    return left,right
