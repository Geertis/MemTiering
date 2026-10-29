#!/usr/bin/env python3
"""Reconstruct the released Linux tree from a verified archive and patch series."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / 'backend'


def sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path,
                        default=ROOT / '.state/cache/linux-6.10.tar.gz')
    parser.add_argument('--destination', type=Path,
                        default=ROOT / '.state/kernel/linux-6.10')
    args = parser.parse_args()
    archive, destination = args.archive.resolve(), args.destination.resolve()
    if destination.exists():
        parser.error(f'destination exists; choose a fresh directory: {destination}')
    specification = json.loads((BACKEND / 'downloads.json').read_text())['kernel']
    archive.parent.mkdir(parents=True, exist_ok=True)
    if not archive.exists():
        with tempfile.NamedTemporaryFile(dir=archive.parent, delete=False) as stream:
            download = Path(stream.name)
        try:
            print('Downloading', specification['url'], flush=True)
            urllib.request.urlretrieve(specification['url'], download)
            if sha256(download) != specification['sha256']:
                raise RuntimeError('download checksum mismatch')
            download.replace(archive)
        finally:
            download.unlink(missing_ok=True)
    if sha256(archive) != specification['sha256']:
        raise SystemExit('kernel archive checksum mismatch')
    patches = BACKEND / 'patches'
    series = [line.strip() for line in (patches / 'series').read_text().splitlines()
              if line.strip() and not line.lstrip().startswith('#')]
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='prepare-', dir=destination.parent) as temporary:
        unpacked = Path(temporary)
        print('Extracting verified Linux 6.10 archive', flush=True)
        with tarfile.open(archive) as tar:
            tar.extractall(unpacked, filter='data')
        trees = list(unpacked.iterdir())
        if len(trees) != 1 or not trees[0].is_dir():
            raise RuntimeError('expected one source directory in kernel archive')
        tree = trees[0]
        for name in series:
            patch_path = patches / name
            if patch_path.parent != patches or not patch_path.is_file():
                raise RuntimeError(f'invalid patch in series: {name}')
            print('Applying', name, flush=True)
            with patch_path.open('rb') as patch:
                subprocess.run(['patch', '--batch', '--forward', '-p1'],
                               cwd=tree, stdin=patch, check=True)
        # The final module snapshot is authoritative, including its Kbuild file.
        shutil.copytree(BACKEND / 'kernel-overrides', tree, dirs_exist_ok=True)
        (tree / '.PhaseTieing-source.json').write_text(json.dumps({
            'project': 'PhaseTieing', 'linux': '6.10',
            'archive_sha256': specification['sha256'], 'patches': series,
        }, indent=2) + '\n')
        tree.rename(destination)
    print('Prepared', destination, flush=True)


if __name__ == '__main__':
    main()
