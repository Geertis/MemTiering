"""Paired cohort stopping rule; never impose a tool or PhaseTieing deadline."""
import math


def paired_policy(spec, reference_state=None, reference_audit=None):
    if spec['strategy'] != 'F':
        return None
    if reference_state is None or reference_audit is None:
        raise ValueError('F requires its completed, valid PhaseTieing reference')
    if (reference_state.get('strategy') != 'P' or
            reference_state.get('result') != 'completed' or
            not reference_audit.get('valid') or reference_audit.get('l1_oom')):
        raise ValueError('invalid PhaseTieing reference')
    for key in ['medium', 'dram_gib', 'n', 'repetition', 'seed', 'slots']:
        if spec[key] != reference_state[key]:
            raise ValueError('unmatched PhaseTieing reference: ' + key)
    duration = reference_state['all_done_epoch'] - reference_state['release_epoch']
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('reference batch duration must be finite and positive')
    return dict(classification='control-relative-timeout', factor=2,
                reference_label=reference_state['label'], reference_batch_s=duration,
                limit_s=2 * duration, scope='F cohort only')


def expired(policy, elapsed_s, all_done=False):
    # Completion is checked before stopping; export/cleanup is outside batch time.
    return bool(policy and not all_done and elapsed_s >= policy['limit_s'])
