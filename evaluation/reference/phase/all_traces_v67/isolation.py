"""Per-lane systemd resource domains and a read-only isolation ledger."""
import json
import os
from pathlib import Path
import subprocess
import time

LANES={6:dict(vcpus='24-27',helpers='34-35',allowed='24-27,34-35'),
       7:dict(vcpus='28-31',helpers='32-33',allowed='28-33'),
       1:dict(vcpus='4-7',helpers='16-17',allowed='4-7,16-17'),
       2:dict(vcpus='8-11',helpers='18-19',allowed='8-11,18-19'),
       3:dict(vcpus='12-15',helpers='20-21',allowed='12-15,20-21')}
MEMORY_MAX=24*(1<<30)
MEMORY_LOW=12*(1<<30)

def save(p,value):
    tmp=p.with_name(p.name+'.tmp');tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n');tmp.replace(p)

def cpus(value):
    result=set()
    for item in value.replace(' ',',').split(','):
        if not item:continue
        if '-' in item:
            start,end=map(int,item.split('-'));result.update(range(start,end+1))
        else:result.add(int(item))
    return result

def run_isolated(cmd,cwd,env,stdout,lane,folder):
    settings=LANES[lane]
    unit=f'acg67-l{lane}-{os.getpid()}-{time.time_ns()%100000000}'
    cg=Path('/sys/fs/cgroup/system.slice')/(unit+'.service')
    props=['User=geerties','WorkingDirectory='+str(cwd),'AllowedCPUs='+settings['allowed'],
        'CPUAffinity='+settings['helpers'],'AllowedMemoryNodes=0,2',f'MemoryMax={MEMORY_MAX}',
        f'MemoryLow={MEMORY_LOW}','MemorySwapMax=0','MemoryAccounting=yes','CPUAccounting=yes','IOAccounting=yes',
        'OOMPolicy=stop','KillMode=control-group','TimeoutStopSec=45s']
    launch=['sudo','-n','systemd-run','--quiet','--wait','--pipe','--collect','--unit='+unit]
    launch.extend('--property='+p for p in props)
    launch+=['/usr/bin/env',*[k+'='+str(v) for k,v in env.items()],*cmd]
    metadata=folder.with_name(folder.name+'.isolation.json');samples=folder.with_name(folder.name+'.isolation.jsonl')
    summary=dict(unit=unit+'.service',lane=lane,settings=settings,memory_max_bytes=MEMORY_MAX,
        memory_low_bytes=MEMORY_LOW,host_swap_max_bytes=0,started_epoch=time.time(),samples=0,
        peak_charge_bytes=0,peak_host_swap_bytes=0,memory_events={},effective_cpu_sets=[],effective_mem_sets=[])
    save(metadata,summary)
    with samples.open('x') as stream:
        process=subprocess.Popen(launch,cwd=cwd,stdout=stdout,stderr=subprocess.STDOUT)
        try:
            while process.poll() is None:
                if cg.exists():
                    point=dict(epoch=time.time())
                    for name in ['memory.current','memory.peak','memory.swap.current','memory.events','cpuset.cpus.effective','cpuset.mems.effective','memory.stat','memory.pressure','cpu.stat','cpu.pressure','io.stat','io.pressure']:
                        try:point[name]=(cg/name).read_text().strip()
                        except FileNotFoundError:pass
                    summary['samples']+=1
                    summary['peak_charge_bytes']=max(summary['peak_charge_bytes'],int(point.get('memory.peak',0)),int(point.get('memory.current',0)))
                    summary['peak_host_swap_bytes']=max(summary['peak_host_swap_bytes'],int(point.get('memory.swap.current',0)))
                    for line in point.get('memory.events','').splitlines():
                        key,value=line.split();summary['memory_events'][key]=max(summary['memory_events'].get(key,0),int(value))
                    for source,target in [('cpuset.cpus.effective','effective_cpu_sets'),('cpuset.mems.effective','effective_mem_sets')]:
                        if point.get(source) and point[source] not in summary[target]:summary[target].append(point[source])
                    stream.write(json.dumps(point)+'\n');stream.flush()
                time.sleep(.5)
            summary.update(finished_epoch=time.time(),returncode=process.returncode)
        finally:
            if process.poll() is None:
                subprocess.run(['sudo','-n','systemctl','stop',unit+'.service'],check=False,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                process.wait()
            save(metadata,summary)
    return subprocess.CompletedProcess(cmd,process.returncode)

def check(folder,lane):
    data=json.loads(folder.with_name(folder.name+'.isolation.json').read_text());errors=[]
    if not data['samples']:errors.append('no Host resource-domain samples')
    if data['peak_host_swap_bytes']:errors.append('Host swapped a VM resource domain')
    if any(data['memory_events'].get(k,0) for k in ['max','oom','oom_kill']):errors.append('Host resource-domain limit/oom event')
    if any(cpus(v)!=cpus(LANES[lane]['allowed']) for v in data['effective_cpu_sets']):errors.append('Host CPU domain drift')
    if any(cpus(v)!={0,2} for v in data['effective_mem_sets']):errors.append('Host NUMA domain drift')
    return errors,data
