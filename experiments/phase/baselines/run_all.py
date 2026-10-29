#!/usr/bin/env python3
"""Supplementary cohorts run sequentially; keep screening separate from 1x."""
import json
import subprocess
import sys
import time
from pathlib import Path

W=Path(__file__).resolve().parents[2];H=Path(__file__).resolve().parent
R=W/'results/baselines-20261009'
while not (R/'initial-image-audit.json').exists():time.sleep(1)
for directory,args in [
    ('screen',['--speed','10','--repeats','3']),
    ('realtime',['--speed','1','--repeats','3','--traces','dask-5510','xonsh-haiku'])]:
    cmd=[sys.executable,str(H/'run_matrix.py'),'--root',str(R/directory),*args]
    status=subprocess.call(cmd,cwd=W)
    if status:raise SystemExit(status)
    status=subprocess.call([sys.executable,str(H/'analyze.py'),'--root',str(R/directory)],cwd=W)
    if status:raise SystemExit(status)
(R/'single-vm-finished.json').write_text(json.dumps({'epoch':time.time(),'screen_vms':84,'realtime_vms':24})+'\n')
