#!/usr/bin/env python3
"""Fetch the four public, immutable M1 trace inputs and verify every byte."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--checkout',type=Path,help='Existing AgentCgroup checkout; avoids a network clone')
    p.add_argument('--output',type=Path,default=ROOT/'.state/concurrent-inputs')
    a=p.parse_args()
    manifest=json.loads((ROOT/'experiments/concurrent/inputs.json').read_text())
    a.output.mkdir(parents=True,exist_ok=True)
    checkout=a.checkout or a.output/'upstream'
    if not checkout.exists():
        subprocess.run(['git','clone','--filter=blob:none','--no-checkout',manifest['upstream'],str(checkout)],check=True)
    subprocess.run(['git','-C',str(checkout),'cat-file','-e',manifest['commit']+'^{commit}'],check=True)
    count=0
    for case in manifest['cases']:
        folder=a.output/'traces'/case['name']
        folder.mkdir(parents=True,exist_ok=True)
        for name,digest in case['input_hashes'].items():
            blob=subprocess.check_output(['git','-C',str(checkout),'show',
                manifest['commit']+':'+case['relative_source']+'/'+name])
            if hashlib.sha256(blob).hexdigest()!=digest:
                raise RuntimeError('immutable upstream input mismatch: '+case['name']+'/'+name)
            target=folder/name
            if target.exists() and target.read_bytes()!=blob:
                raise RuntimeError('refusing to overwrite a different input: '+str(target))
            target.write_bytes(blob)
            count+=1
    print(f'INPUT_PASS: {count} exact files at {a.output / "traces"}')


if __name__=='__main__':main()
