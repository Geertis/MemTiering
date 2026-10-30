"""Offline checks for the portable port of the measured concurrent runner."""
import ast
from collections import Counter
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('portable_concurrent', REPO / 'tools/concurrent.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class ConcurrentReleaseTest(unittest.TestCase):
    def settings(self):
        return runner.config(REPO / 'experiments/concurrent/site.example.json')

    def test_pairs_share_arrivals_and_have_no_execution_deadline(self):
        c = self.settings()
        c.update(dram_gib=[7, 8, 10, 12], repetitions=[1, 2, 3])
        matrix = runner.matrix(c)
        self.assertEqual(len(matrix), 48)
        for start in range(0, len(matrix), 4):
            pair = matrix[start:start+4]
            self.assertTrue(all(x['slots'] == pair[0]['slots'] for x in pair))
            self.assertEqual(sorted(Counter(x['case'] for x in pair[0]['slots']).values()), [2]*4)
            self.assertTrue(all(x['deadline_s'] is None for x in pair[0]['slots']))

    def test_prepare_is_offline_and_generated_modules_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = self.settings()
            c['lab_root'] = str(Path(tmp) / 'owned-lab')
            with contextlib.redirect_stdout(io.StringIO()):
                runner.prepare(c)
            base, w, h, root = runner.layout(c)
            self.assertFalse((base / 'storage').exists())
            self.assertFalse((root / 'bootstrap-complete.json').exists())
            program = ('import pressure; from environment import environment; '
                       'assert len(pressure.plans()) == 4; '
                       'assert pressure.ROOT.name == "PhaseTieing-repro-7g"')
            subprocess.run(['python3', '-B', '-c', program], cwd=h, check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            for path in base.rglob('*.py'):
                ast.parse(path.read_text())
            for path in base.rglob('*.sh'):
                subprocess.run(['bash', '-n', str(path)], check=True, capture_output=True)
            env = (h / 'environment.py').read_text()
            self.assertIn("REPLAY_TIMEOUT_S='0'", env)
            self.assertIn("SB_SAMPLING='0'", env)
            script = (h / 'run_single.sh').read_text()
            self.assertIn('--disable-command-timeouts', script)
            self.assertIn('/sys/module/PhaseTieing_placement', script)
            self.assertNotIn(runner.OLD, script)
            self.assertIn("ROOT / 'nvme-cold.raw'", (h / 'outer.py').read_text())
            with self.assertRaises(FileExistsError):
                runner.prepare(c)

    def test_invalid_geometry_is_rejected(self):
        c = self.settings()
        c['l0']['helper_cores'] = [32]
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'site.json'
            p.write_text(json.dumps(c))
            with self.assertRaisesRegex(ValueError, 'disjoint'):
                runner.config(p)

    def test_real_bash_replay_can_outlive_recorded_api_limit(self):
        paths = [REPO / 'experiments/phase/artifacts' / name / 'replay_agentcgroup.py'
                 for name in runner.VARIANTS]
        for index, path in enumerate(paths):
            with self.subTest(artifact=path.parent.name):
                spec = importlib.util.spec_from_file_location('released_timeout_' + str(index), path)
                module = importlib.util.module_from_spec(spec)
                import sys
                sys.modules[spec.name] = module
                spec.loader.exec_module(module)
                replay = module.Replayer.__new__(module.Replayer)
                replay.args = SimpleNamespace(command_timeout=600, disable_command_timeouts=True)
                replay.shell_env = os.environ.copy()
                replay._persist_exports = lambda command: {}
                result = replay._execute('Bash', dict(command='sleep 0.12; printf natural-completion', timeout=1))
                self.assertEqual(result['status'], 0)
                self.assertEqual(result['recorded_timeout_ms'], 1)
                self.assertIsNone(result['applied_timeout_s'])
                self.assertIn('natural-completion', result['stdout_tail'])


if __name__ == '__main__':
    unittest.main()
