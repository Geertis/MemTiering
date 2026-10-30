"""Exercise the real Bash replay path with a shorter recorded API timeout."""
import importlib.util
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest


class NaturalCompletion(unittest.TestCase):
    def test_all_four_artifacts_ignore_recorded_limit_when_explicitly_disabled(self):
        root=Path(__file__).resolve().parent/'artifacts'
        for index,path in enumerate(sorted(root.glob('*/replay_agentcgroup.py'))):
            with self.subTest(artifact=path.parent.name):
                spec=importlib.util.spec_from_file_location('replay_timeout_test_'+str(index),path)
                module=importlib.util.module_from_spec(spec)
                sys.modules[spec.name]=module
                spec.loader.exec_module(module)
                replay=module.Replayer.__new__(module.Replayer)
                replay.args=SimpleNamespace(command_timeout=600,disable_command_timeouts=True)
                replay.shell_env=os.environ.copy()
                replay._persist_exports=lambda command: {}
                # The real command outlives its recorded 1 ms API limit.
                result=replay._execute('Bash',dict(command="sleep 0.12; printf natural-completion",timeout=1))
                self.assertEqual(result['status'],0)
                self.assertEqual(result['recorded_timeout_ms'],1)
                self.assertIsNone(result['applied_timeout_s'])
                self.assertIn('natural-completion',result['stdout_tail'])


if __name__=='__main__':unittest.main()
