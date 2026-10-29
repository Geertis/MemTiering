#!/usr/bin/env python3
import subprocess
import sys
import time
from pathlib import Path
W=Path(__file__).resolve().parents[2];H=Path(__file__).resolve().parent;R=W/'results/baselines-20261009'
while not (R/'whole-ram-v3/finished.json').exists():time.sleep(1)
for name in ['screen','realtime','whole-ram-v3']:
    subprocess.run([sys.executable,str(H/'analyze.py'),'--root',str(R/name)],check=True,cwd=W)
    subprocess.run([str(W/'.venv-capacity-analysis/bin/python'),str(H/'plot.py'),'--root',str(R/name)],check=True,cwd=W)
subprocess.run([sys.executable,str(H/'analyze_mixed.py'),'--root',str(R/'mixed')],check=True,cwd=W)
subprocess.run([sys.executable,str(H/'tool_quality.py')],check=True,cwd=W)
subprocess.run(['numactl','--cpunodebind=1','--membind=1',sys.executable,str(H/'audit.py')],check=True,cwd=W)
subprocess.run([sys.executable,str(H/'build_report.py')],check=True,cwd=W)
(R/'all-finished').touch()
