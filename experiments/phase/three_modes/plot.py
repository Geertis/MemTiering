#!/usr/bin/env python3
"""Publication/export plots of measured points, including uncertainty ranges."""
import argparse
import json
import math
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True)
p.add_argument('--main-only',action='store_true');a=p.parse_args()
R=a.root.resolve();data=json.loads((R/'analysis.json').read_text())
traces=list(dict.fromkeys(s['trace'] for s in data['summaries']))
labels={'numba-9636':'Numba','dask-5510':'Dask','pydicom-2065':'Pydicom','xonsh-local':'Xonsh local','xonsh-haiku':'Xonsh Haiku'}

def style(strategy):
    if strategy=='dram':return '#222222','*','All DRAM'
    if strategy=='cxl':return '#c84642','o','All CXL'
    variant=''.join(x for x in strategy if not x.isdigit());quota=''.join(x for x in strategy if x.isdigit())
    color,marker={'full':('#1267a6','D'),'heat':('#c28119','^'),'alloc':('#6b56a0','s'),
                  'reclaim':('#348663','P'),'reactive':('#667477','X'),'legacy':('#4a8e73','v'),
                  'reactivealloc':('#705491','X'),'eventpref':('#b05487','s'),'reactivepref':('#966b35','v')}[variant]
    return color,marker, f'{variant}, q={int(quota)/1024:g} GiB'

for metric in ['ddr_peak_mib','ddr_mean_mib']:
    columns=min(2,len(traces));rows=math.ceil(len(traces)/columns)
    fig,axs=plt.subplots(rows,columns,figsize=(6.1*columns,3.7*rows),squeeze=False)
    handles={}
    for ax,trace in zip(axs.flat,traces):
        available=[s for s in data['summaries'] if s['trace']==trace and s.get('complete')]
        representative='full' if any(s['strategy'].startswith('full') for s in available) else 'reclaim'
        group=[s for s in available
               if (not a.main_only or s['strategy'] in ['dram','cxl'] or s['strategy'].startswith(representative))]
        ax.axvspan(-5,5,color='#dcebdd',alpha=.7)
        ax.axvline(5,color='#47764c',ls='--',lw=1)
        ax.axhline(1,color='#777777',ls=':',lw=1)
        for s in group:
            color,marker,label=style(s['strategy'])
            x=100*(s['ratios']['service_s']-1);y=s['ratios'].get(metric,0)
            alpha=1 if s['valid'] else .3
            artist=ax.scatter([x],[y],s=80,color=color,marker=marker,alpha=alpha,zorder=3)
            handles[label]=artist
            cis=s.get('bootstrap_95ci',{})
            xc=cis.get('service_s');yc=cis.get(metric)
            if xc and yc:
                ax.errorbar(x,y,xerr=[[max(0,x-100*(xc[0]-1))],[max(0,100*(xc[1]-1)-x)]],
                    yerr=[[max(0,y-yc[0])],[max(0,yc[1]-y)]],color=color,lw=1,capsize=2,alpha=alpha,zorder=2)
            if s['strategy'] not in ['dram','cxl']:
                variant=''.join(c for c in s['strategy'] if not c.isdigit())
                offset={'full':(5,17),'heat':(5,-16),'reclaim':(5,0),'alloc':(5,-28)}.get(variant,(5,6))
                ax.annotate(label,(x,y),xytext=offset,textcoords='offset points',fontsize=8,color=color)
        # Keep the complete uncertainty bars visible, including intervals wider
        # than the spread of median points.
        xbounds=[];ybounds=[]
        for s in group:
            xbounds.append(100*(s['ratios']['service_s']-1));ybounds.append(s['ratios'].get(metric,0))
            cis=s.get('bootstrap_95ci',{})
            if cis.get('service_s'):xbounds.extend(100*(v-1) for v in cis['service_s'])
            if cis.get(metric):ybounds.extend(cis[metric])
        maxx=max(xbounds,default=5);minx=min(xbounds,default=0);maxy=max(ybounds,default=1)
        ax.set_xlim(min(-6,minx-2),max(10,maxx+8));ax.set_ylim(-.035,max(1.1,maxy+.1))
        ax.set_title(labels.get(trace,trace),loc='left',fontweight='bold')
        ax.set_xlabel('Tool service sum change vs all DRAM (%)')
        ax.set_ylabel(('Peak' if metric=='ddr_peak_mib' else 'Mean')+' VMM DDR / all DRAM')
        ax.grid(alpha=.18)
    for ax in list(axs.flat)[len(traces):]:ax.set_visible(False)
    fig.legend(handles.values(),handles.keys(),loc='lower center',ncol=min(4,len(handles)),frameon=False,fontsize=9)
    fig.tight_layout(rect=(0,.08,1,1))
    name='peak-tradeoff' if metric=='ddr_peak_mib' else 'mean-tradeoff'
    if a.main_only:name+='-main'
    fig.savefig(R/(name+'.png'),dpi=180,bbox_inches='tight')
    fig.savefig(R/(name+'.pdf'),bbox_inches='tight');plt.close(fig)
print('Saved peak and mean tradeoff plots:',R)
