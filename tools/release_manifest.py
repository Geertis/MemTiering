#!/usr/bin/env python3
"""Regenerate the source release inventory after a reviewed source change."""
import hashlib
import json
from check_release import ROOT, files


def main():
    manifest_path = ROOT / 'release-manifest.json'
    inventory = []
    for path in sorted(files()):
        if path == manifest_path:
            continue
        data = path.read_bytes()
        inventory.append({'path': str(path.relative_to(ROOT)),
                          'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)})
    manifest_path.write_text(json.dumps({
        'project': 'PhaseTieing',
        'scope': 'runtime, kernel backend, VMM, evaluation source and report summaries',
        'revisions': {'ssd': 'v66-compact', 'cxl': 'v67-compact', 'linux': '6.10',
                      'cloud_hypervisor': '36.0.0', 'concurrent': 'v7-adaptive-reserve-matched-ceiling'},
        'backend_source_included': True,
        'prebuilt_backend_included': False,
        'files': inventory,
    }, ensure_ascii=False, indent=2) + '\n')
    print(f'Manifest written: {len(inventory)} files')


if __name__ == '__main__':
    main()
