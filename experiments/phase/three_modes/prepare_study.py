#!/usr/bin/env python3
"""Freeze the strict no-history heat ablation module on the validated topology."""
import hashlib,json,shutil
from pathlib import Path
W=Path(__file__).resolve().parents[2]
R=W.parents[1]/'sosp25-demeter'
A=W/'phase/artifacts/vmm-v14-future-ablation'
assert not A.exists()
shutil.copytree(W/'phase/artifacts/vmm-v13-validated',A)
shutil.copy2(R/'build/demeter/mm/PhaseTieing/PhaseTieing_placement.ko',A/'PhaseTieing_placement.ko')
shutil.copy2(R/'kernel/demeter/mm/PhaseTieing/sandbox.c',A/'sandbox.c')
(A/'SHA256SUMS.json').write_text(json.dumps({p.name:hashlib.sha256(p.read_bytes()).hexdigest()
 for p in A.iterdir() if p.is_file() and p.name!='SHA256SUMS.json'},indent=2)+'\n')
print(A)
