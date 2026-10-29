#!/usr/bin/env python3
"""Standalone PNG/PDF curve of measured savings under E2E bounds."""
import argparse
import json
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--root', type=Path, required=True)
    a = p.parse_args()
    data = json.loads((a.root / 'analysis.json').read_text())
    traces = sorted({c['trace'] for c in data['constraints']})
    fig, axes = plt.subplots(1, len(traces), figsize=(4.3 * len(traces), 3.5), squeeze=False)
    for ax, trace in zip(axes[0], traces):
        for media, color, marker in [('cxl', '#2171b5', 'o'), ('ssd', '#d95f0e', 's')]:
            points = [c for c in data['constraints'] if c['trace'] == trace and c['media'] == media]
            x = [c['limit_pct'] for c in points]
            y = [c['selected']['dram_mean_save_pct'] if c['selected'] else float('nan') for c in points]
            ax.plot(x, y, marker=marker, color=color, label=media.upper())
        ax.set_title(trace)
        ax.set_xticks([5, 10, 15, 20])
        ax.set_xlabel('Allowed E2E increase (%)')
        ax.set_ylabel('Whole-VMM DRAM mean saving (%)')
        ax.set_ylim(0, 105)
        ax.grid(alpha=.2)
        ax.legend()
    label = 'Full recorded waits, paired repeats' if data['full_wait_e2e'] else 'Compressed waits: exploratory only'
    fig.suptitle(label + ' | criterion: ' + data['criterion'])
    fig.tight_layout()
    for suffix in ['png', 'pdf']:
        fig.savefig(a.root / ('e2e-dram-savings.' + suffix), dpi=180)
    plt.close(fig)


if __name__ == '__main__':
    main()
