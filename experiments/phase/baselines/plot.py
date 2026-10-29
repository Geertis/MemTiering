#!/usr/bin/env python3
"""Whole-VMM memory/performance tradeoffs, including all independent VM points."""
import argparse
import json
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);a=p.parse_args();R=a.root.resolve()
d=json.loads((R/'analysis.json').read_text());traces=list(dict.fromkeys(r['trace'] for r in d['trials']))
colors={'dram':'#34495e','cxl':'#c0392b','static':'#2980b9','phase':'#229954'}
columns=min(4,len(traces));nrows=(len(traces)+columns-1)//columns
fig,axes=plt.subplots(nrows,columns,figsize=(3.9*columns,3.5*nrows+0.5),squeeze=False)
for ax,trace in zip(axes.flat,traces):
    rows=[r for r in d['trials'] if r['trace']==trace and r['valid']]
    refs={r['repeat']:r for r in rows if r['policy']=='dram'}
    for policy in colors:
        group=[r for r in rows if r['policy']==policy]
        if not group:continue
        x=[r['host_ddr_replay_peak_mib'] for r in group]
        y=[r['tool_service_sum_s']/refs[r['repeat']]['tool_service_sum_s'] for r in group]
        ax.scatter(x,y,color=colors[policy],alpha=.35,s=24)
        import statistics
        ax.scatter(statistics.median(x),statistics.median(y),color=colors[policy],marker='D',s=55,label=policy)
    ax.axhline(1.0,color='#888',ls=':',lw=1)
    ax.set_title(trace);ax.set_xlabel('Whole-VMM DDR sampled peak (MiB)');ax.set_ylabel('Tool service / DRAM')
    ax.grid(alpha=.15)
for ax in list(axes.flat)[len(traces):]:ax.set_visible(False)
axes.flat[0].legend(fontsize=8)
scope='Whole Guest RAM mapping; ' if d.get('scope','').startswith('Whole Guest RAM') else ''
fig.suptitle(f"{scope}independent VMs: {d['speed']:g}x offsets; reporting on, order 9\nDotted line marks native DRAM tool service, not a business-p99 acceptance test",fontsize=12)
fig.tight_layout()
for ext in ['png','pdf']:fig.savefig(R/f'whole-vmm-tradeoff.{ext}',dpi=180,bbox_inches='tight',pad_inches=.15)
