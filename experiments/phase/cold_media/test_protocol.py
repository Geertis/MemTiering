"""Timing, accounting and constraint checks for the executable protocol."""
import argparse
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

H = Path(__file__).resolve().parent
sys.path.insert(0, str(H))
from analyze import integrate, paired_stats, select

spec = importlib.util.spec_from_file_location('media_replay',
    H.parents[1] / 'phase/artifacts/vmm-v25-cold-media-e2e/replay_agentcgroup.py')
replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay)


class TimingTests(unittest.TestCase):
    def test_real_replay_e2e_preserves_wait_and_excludes_cleanup(self):
        clock = SimpleNamespace(now=100.)
        calls = [dict(tool='Read', input={}, timestamp='2026-01-01T00:00:00Z', end_timestamp='2026-01-01T00:00:01Z'),
                 dict(tool='Read', input={}, timestamp='2026-01-01T00:00:03Z', end_timestamp='2026-01-01T00:00:04Z')]
        runner = object.__new__(replay.Replayer)
        runner.args = argparse.Namespace(workdir=H, join_cgroup=False, timing='gaps', speed=1,
                                         tool_calls=H / 'tool_calls.json', sample_interval=.1)
        runner.calls, runner.original_start = calls, replay.parse_timestamp(calls[0]['timestamp'])
        runner.sampler, runner.phase_runtime = Mock(), Mock()
        runner.sampler.samples = []
        runner.expected_outcomes, runner.shell_env, runner.search_backend = {}, {}, 'ripgrep'
        runner.initial_policy = {}
        runner.phase_runtime.errors = []
        durations = iter([10., .5])
        def advance(dt):
            clock.now += dt
        def execute(*_):
            advance(next(durations))
            return dict(status=0)
        def notify(*_):
            advance(.2)
            return {}
        def git(*args, **kwargs):
            advance(2.)
            return SimpleNamespace(returncode=0, stdout='head\n' if kwargs.get('text') else b'')
        runner._execute = execute
        runner.phase_runtime.notify.side_effect = notify
        runner.sampler.stop.side_effect = lambda: advance(3.)
        with patch.object(replay.time, 'monotonic', side_effect=lambda: clock.now), \
             patch.object(replay.time, 'sleep', side_effect=advance), \
             patch.object(replay.subprocess, 'run', side_effect=git), \
             patch.object(replay.os, 'chdir'):
            result = runner.replay()
        self.assertAlmostEqual(result['e2e_s'], 13.3)
        self.assertAlmostEqual(result['elapsed_s'] - result['e2e_s'], 9.2)
        first, last = result['steps']
        self.assertAlmostEqual(last['service_started_monotonic_s'] - first['service_finished_monotonic_s'], 2.)
        self.assertAlmostEqual(sum(s['elapsed_s'] for s in result['steps']), 11.3)


def sample(t, value, fresh=True):
    return dict(guest={'guest_monotonic_ns': int(t * 1e9)}, guest_fresh=fresh,
                rss_node_bytes={'N0': int(value * (1 << 20)), 'N2': 0})


class AccountingTests(unittest.TestCase):
    def test_mean_weights_time_not_sample_count(self):
        stats = integrate([sample(0, 100), sample(.25, 200), sample(1, 0)], 0, 1)
        self.assertEqual(stats['ddr_mean_mib'], 175)
        self.assertEqual(stats['coverage'], 1)

    def test_stale_and_missing_physical_samples_are_not_free_memory(self):
        stats = integrate([sample(0, 100), sample(2, 0)], 0, 2)
        self.assertEqual(stats['coverage'], 0)
        self.assertIsNone(stats['ddr_mean_mib'])
        self.assertEqual(integrate([sample(0, 100, False), sample(.5, 0)], 0, .5)['coverage'], 0)


class ConstraintTests(unittest.TestCase):
    def candidate(self, strategy, saving, median, worst):
        return dict(strategy=strategy, repeats=3, dram_mean_save_pct=saving,
                    e2e_increase_pct=median, e2e_worst_pct=worst)

    def test_each_e2e_limit_selects_saving_and_keeps_outliers(self):
        candidates = [self.candidate('dram', 0, 0, 0),
                      self.candidate('mild', 20, 3, 4),
                      self.candidate('strong', 40, 9, 12),
                      self.candidate('extreme', 60, 16, 19)]
        results = select(candidates, [.05, .10, .15, .20], 3)
        self.assertEqual([r['selected']['strategy'] for r in results], ['mild', 'mild', 'strong', 'extreme'])

    def test_invalid_work_and_insufficient_repeats_cannot_win(self):
        base = dict(repeat=1, valid=True, signature={'work': 1}, e2e_s=100,
                    service_s=10, ddr_mean_mib=100, ddr_peak_mib=200,
                    ddr_integral_gib_s=10)
        bad = dict(base, signature={'work': 2}, ddr_mean_mib=1)
        self.assertIsNone(paired_stats([bad], [base]))
        candidate = self.candidate('one-sample', 99, 0, 0)
        candidate['repeats'] = 1
        self.assertIsNone(select([candidate], [.20], 3)[0]['selected'])


if __name__ == '__main__':
    unittest.main()
