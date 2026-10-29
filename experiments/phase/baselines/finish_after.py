#!/usr/bin/env python3
"""Finish analysis, image audit and report after every experiment VM has exited."""
import subprocess
import sys
import time
from pathlib import Path
W=Path(__file__).resolve().parents[2];H=Path(__file__).resolve().parent;R=W/'results/baselines-20261009'
while not (R/'mixed/finished.json').exists():time.sleep(1)
for name in ['screen','realtime']:
    subprocess.run([sys.executable,str(H/'analyze.py'),'--root',str(R/name)],check=True,cwd=W)
    subprocess.run([str(W/'.venv-capacity-analysis/bin/python'),str(H/'plot.py'),'--root',str(R/name)],check=True,cwd=W)
subprocess.run([sys.executable,str(H/'analyze_mixed.py'),'--root',str(R/'mixed')],check=True,cwd=W)
subprocess.run(['numactl','--cpunodebind=1','--membind=1',sys.executable,str(H/'audit.py')],check=True,cwd=W)
subprocess.run([sys.executable,str(H/'build_report.py')],check=True,cwd=W)
(R/'all-finished').touch()
