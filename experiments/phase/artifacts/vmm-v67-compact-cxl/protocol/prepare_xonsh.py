#!/usr/bin/env python3
"""Add recorded Python 3.12 pytest dependencies to a new image only."""
import hashlib
import json
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

W = Path(__file__).resolve().parents[2]
R = W / 'results/multitrace-20261009'
source = W / 'xonsh-3255-golden.ext4'
output = W / 'xonsh-3255-faithful-20261009-v2.ext4'
if output.exists():
    raise SystemExit('refusing overwrite of prepared image')
deps = R / 'xonsh-python-deps-v2'
deps.mkdir()
for wheel in sorted((R / 'wheels').glob('*.whl')):
    with zipfile.ZipFile(wheel) as z:
        for name in z.namelist():
            assert not name.startswith('/') and '..' not in Path(name).parts
        z.extractall(deps)
subprocess.run(['cp', '--reflink=auto', '--sparse=always', str(source), str(output)], check=True)
mount = Path(tempfile.mkdtemp(prefix='xonsh-prepare-', dir=R))
mounted = False
try:
    subprocess.run(['sudo', '-n', 'mount', '-o', 'loop', str(output), str(mount)], check=True)
    mounted = True
    subprocess.run(['sudo', '-n', 'cp', '-r', str(deps), str(mount / 'opt/replay-deps')], check=True)
    check = subprocess.check_output(['sudo', '-n', 'chroot', str(mount), '/opt/conda/bin/python', '-c',
        'import sys;sys.path.insert(0,"/opt/replay-deps");import pytest,pluggy;print(sys.version);print(pytest.__version__,pluggy.__version__)'], text=True)
    assert '9.0.2 1.6.0' in check
    print(check, flush=True)
finally:
    if mounted:
        subprocess.run(['sudo', '-n', 'umount', str(mount)], check=True)
    mount.rmdir()
def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(8<<20), b''): h.update(chunk)
    return h.hexdigest()
record={'source':str(source),'output':str(output),'source_sha256':sha(source),
        'output_sha256':sha(output),'wheel_sha256':{p.name:sha(p) for p in (R/'wheels').glob('*.whl')},
        'validation':check,'reason':'Original trace reports Python 3.12.3 and pytest 9.0.2; testbed Conda uses Python 3.7 and is not the recorded tool environment.',
        'original_image_modified':False}
(R/'xonsh-image-v2.json').write_text(json.dumps(record,indent=2)+'\n')
print('PREPARED',output,flush=True)
