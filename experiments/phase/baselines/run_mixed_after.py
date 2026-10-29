#!/usr/bin/env python3
import subprocess
import sys
import time
from pathlib import Path
W=Path(__file__).resolve().parents[2];H=Path(__file__).resolve().parent;R=W/'results/baselines-20261009'
while not (R/'single-vm-finished.json').exists():time.sleep(1)
raise SystemExit(subprocess.call([sys.executable,str(H/'run_mixed.py'),'--root',str(R/'mixed')],cwd=W))
