"""Resource admission and E2E-aligned, read-only interference observations."""
import json
from pathlib import Path
import statistics
import threading
import time

from execution import ROOT,read


def psi(kind):
    result={}
    for line in (Path('/proc/pressure')/kind).read_text().splitlines():
        fields=line.split();result[fields[0]]={k:float(v) for k,v in (f.split('=') for f in fields[1:])}
    return result


def snapshot():
    mem={l.split(':')[0]:int(l.split()[1])*1024 for l in Path('/proc/meminfo').read_text().splitlines() if len(l.split())>=2 and l.split()[1].isdigit()}
    nodes={}
    for p in Path('/sys/devices/system/node').glob('node[0-9]*'):
        v={l.split()[2].rstrip(':'):int(l.split()[3])*1024 for l in (p/'meminfo').read_text().splitlines() if len(l.split())>=4}
        nodes[p.name]=dict(total=v.get('MemTotal',0),reclaimable_estimate=max(0,v.get('MemFree',0)+v.get('FilePages',0)-v.get('Shmem',0)-v.get('Dirty',0)-v.get('Writeback',0)))
    return dict(epoch=time.time(),host_monotonic_ns=time.monotonic_ns(),memory_available=mem['MemAvailable'],
        nodes=nodes,memory_psi=psi('memory'),io_psi=psi('io'),loadavg=Path('/proc/loadavg').read_text().strip())


class Monitor:
    def __init__(self):
        self.stop=threading.Event();self.path=ROOT/'host-resources.jsonl';self.thread=threading.Thread(target=self.run,daemon=True)
    def run(self):
        with self.path.open('a') as f:
            while not self.stop.is_set():
                f.write(json.dumps(snapshot())+'\n');f.flush();self.stop.wait(1)
    def start(self):self.thread.start()
    def close(self):self.stop.set();self.thread.join()


def admission(slots):
    s=snapshot();gib=1<<30
    reasons=[]
    if s['memory_available']<(64+24*slots)*gib:reasons.append('global memory reserve')
    if s['nodes']['node0']['reclaimable_estimate']<(32+16*slots)*gib:reasons.append('Host fast-node reserve')
    if s['nodes']['node2']['reclaimable_estimate']<(16+8*slots)*gib:reasons.append('Host CXL-node reserve')
    if s['memory_psi']['some']['avg10']>.5:reasons.append('Host memory pressure')
    if s['io_psi']['some']['avg10']>2:reasons.append('Host I/O pressure')
    return reasons,s


class ResourceDeferred(RuntimeError):pass

def await_admission(slots,max_wait=180):
    deadline=time.monotonic()+max_wait
    while True:
        reasons,s=admission(slots)
        if not reasons:return s
        print('WAIT RESOURCE ADMISSION',reasons,flush=True)
        if time.monotonic()>deadline:raise ResourceDeferred('Host resource admission: '+str(reasons))
        time.sleep(10)


def diagnostics(root,row):
    folder=root/row['name'];data=read(folder/'guest-out/result.json')
    offsets=[]
    for line in (folder/'host-capacity.jsonl').open():
        p=json.loads(line)
        if p.get('event')=='sample' and p.get('guest_fresh'):
            offsets.append(p['host_monotonic_ns']-p['guest']['guest_monotonic_ns'])
    offset=statistics.median(offsets)
    lo=data['e2e_started_monotonic_s']*1e9+offset-.5e9
    hi=data['e2e_finished_monotonic_s']*1e9+offset+.5e9
    samples=[json.loads(line) for line in (ROOT/'host-resources.jsonl').open()]
    samples=[p for p in samples if lo<=p['host_monotonic_ns']<=hi]
    return dict(host_window_mapping='median fresh Guest-to-Host monotonic offset, with 0.5s boundary margin',
        host_start_ns=lo,host_end_ns=hi,resource_samples=len(samples),
        host_memory_psi_some_max_avg10=max((p['memory_psi']['some']['avg10'] for p in samples),default=0),
        host_io_psi_some_max_avg10=max((p['io_psi']['some']['avg10'] for p in samples),default=0),
        min_host_available_bytes=min((p['memory_available'] for p in samples),default=None))
