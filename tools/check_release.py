#!/usr/bin/env python3
"""Check release names, local syntax and (when present) release hashes."""
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]
SKIP = {'.git', '.state', '__pycache__'}


def files():
    for directory, folders, names in os.walk(ROOT):
        folders[:] = [name for name in folders if name not in SKIP]
        for name in names:
            yield Path(directory) / name


def main():
    paths = list(files())
    python_count = shell_count = 0
    for path in paths:
        relative = path.relative_to(ROOT)
        if any(re.search('demeter', part, re.I) for part in relative.parts):
            raise SystemExit(f'old project name in filename: {relative}')
        if path.suffix == '.py':
            ast.parse(path.read_bytes(), filename=str(relative))
            python_count += 1
        if path.suffix == '.sh' or relative.parts[0] == 'bin':
            subprocess.run(['bash', '-n', str(path)], check=True)
            shell_count += 1
    manifest_path = ROOT / 'release-manifest.json'
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        declared = {item['path']: item for item in manifest['files']}
        actual = {str(path.relative_to(ROOT)) for path in paths if path != manifest_path}
        if set(declared) != actual:
            raise SystemExit(f'manifest path mismatch: {set(declared) ^ actual}')
        for name, item in declared.items():
            data = (ROOT / name).read_bytes()
            if len(data) != item['bytes'] or hashlib.sha256(data).hexdigest() != item['sha256']:
                raise SystemExit(f'manifest content mismatch: {name}')
    print(f'SOURCE_PASS: {len(paths)} files; {python_count} Python and '
          f'{shell_count} shell syntax checks; filenames and release manifest checked')


if __name__ == '__main__':
    main()
