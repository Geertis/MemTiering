import argparse
import hashlib
import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path

H = Path(__file__).resolve().parent
W = H.parents[1]
spec = importlib.util.spec_from_file_location('faithful', H / 'faithful_replay.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class NativeReplayTest(unittest.TestCase):
    def test_search_modes_flags_ignore_and_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)
            subprocess.run(['git','init','-q',str(p)],check=True)
            (p/'.gitignore').write_text('ignored.py\n')
            (p/'ignored.py').write_text('ALPHA\n')
            (p/'a.py').write_text('before\nAlpha\nafter\n')
            (p/'b.txt').write_text('Beta\n')
            args=argparse.Namespace(workdir=p,cgroup=None,sample_interval=.1,
                                    rg_path=str(W/'results/multitrace-20261009/rg'))
            r=m.Replayer(args,[])
            out=r._grep({'pattern':'alpha','-i':True,'output_mode':'files_with_matches'})
            self.assertEqual(out['matches'],1)
            self.assertEqual(out['stdout_sha256'],hashlib.sha256((str(p/'a.py')+'\n').encode()).hexdigest())
            # Explicit rg --glob overrides ignore rules, matching the backend's contract.
            explicit=r._grep({'pattern':'alpha','-i':True,'glob':'*.py','output_mode':'files_with_matches'})
            self.assertEqual(explicit['matches'],2)
            content=r._grep({'pattern':'Alpha','path':str(p/'a.py'),'output_mode':'content','-C':1})
            self.assertEqual(content['matches'],3)
            missing=r._grep({'pattern':'NOT_PRESENT','output_mode':'content'})
            self.assertEqual((missing['status'],missing['stdout_bytes']),(0,0))
            invalid=r._grep({'pattern':'[','output_mode':'content'})
            self.assertEqual(invalid['status'],2)
    def test_read_range_matches_requested_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);f=p/'f';f.write_bytes(b'first\nsecond\nlast')
            args=argparse.Namespace(workdir=p,cgroup=None,sample_interval=.1)
            r=m.Replayer(args,[])
            out=r._execute('Read',{'file_path':str(f),'offset':2,'limit':1})
            self.assertEqual(out['stdout_sha256'],hashlib.sha256(b'second\n').hexdigest())
            out=r._execute('Read',{'file_path':str(f),'offset':3,'limit':2})
            self.assertEqual(out['stdout_sha256'],hashlib.sha256(b'last').hexdigest())

if __name__=='__main__':unittest.main()
