#!/usr/bin/env python3
import subprocess
import sys
import time
from pathlib import Path
W=Path(__file__).resolve().parents[2];H=Path(__file__).resolve().parent;R=W/'results/baselines-20261009'
while not (R/'mixed/finished.json').exists():time.sleep(1)
raise SystemExit(subprocess.call([sys.executable,str(H/'run_whole_matrix.py'),'--root',str(R/'whole-ram-v3')],cwd=W))
