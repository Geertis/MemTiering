"""Common start/export barriers; retained bounded preflight failures."""
from concurrent.futures import ThreadPoolExecutor,wait
import json
from pathlib import Path
import subprocess
import time
from execution import execute,read,write

def pressure():
    return {k:float(dict(x.split('=') for x in Path('/proc/pressure',k).read_text().splitlines()[0].split()[1:])['avg10']) for k in ['memory','io']}

def wave(root,tasks,barrier,notify=lambda *a,**k:None):
    assert len({t['lane'] for t in tasks})==len(tasks)
    barrier.mkdir(parents=True,exist_ok=False);log=barrier/'events.jsonl'
    def event(kind,**fields):
        with log.open('a') as f:f.write(json.dumps(dict(event=kind,epoch=time.time(),**fields))+'\n')
    event('launch',tasks=[dict(case=t['case']['name'],label=t['label'],profile=t['profile'],lane=t['lane']) for t in tasks])
    for t in tasks:
        name=t['case']['name']+'/'+t['label']
        write(root/(name+'.orchestration.json'),dict(name=name,barrier=str(barrier),started_epoch=time.time(),repeat=t.get('repeat'),baseline=t.get('baseline_name')))
    pool=ThreadPoolExecutor(max_workers=len(tasks))
    futures=[pool.submit(execute,root,t['case'],t['label'],t['lane'],t['profile'],t.get('timeout_s',1800),barrier) for t in tasks]
    released=False;error=None
    try:
        deadline=time.monotonic()+300
        while not all((barrier/f'lane{t["lane"]}.ready').exists() for t in tasks):
            if any(f.done() for f in futures):raise RuntimeError('VM failed before common ready barrier')
            if time.monotonic()>deadline:raise TimeoutError('Guest readiness deadline')
            time.sleep(.25)
        event('all-ready');notify('preflight-settling')
        stable=0;deadline=time.monotonic()+180
        while stable<5:
            if any(f.done() for f in futures):raise RuntimeError('VM exited before common start')
            p=pressure();stable=stable+1 if p['memory']<=.5 and p['io']<=2 else 0
            event('quiet-sample',pressure=p,consecutive=stable)
            if time.monotonic()>deadline:raise TimeoutError('Host did not settle before measurement')
            time.sleep(1)
        event('quiet-admitted',pressure=pressure())
        for t in tasks:(barrier/f'lane{t["lane"]}.go').touch()
        event('released');released=True;notify('formal-running')
        deadline=time.monotonic()+max(t.get('timeout_s',1800) for t in tasks)+120
        while not all((barrier/f'lane{t["lane"]}.done').exists() for t in tasks):
            if any(f.done() for f in futures):raise RuntimeError('VM exited before common export')
            if time.monotonic()>deadline:raise TimeoutError('Workload safety deadline')
            time.sleep(.5)
        event('all-workloads-finished');(barrier/'export.go').touch()
    except Exception as exc:
        error=repr(exc);event('aborted',reason=error);(barrier/'abort').touch();(barrier/'export.go').touch()
    finally:
        if error:
            _,pending=wait(futures,timeout=45)
            for t,f in zip(tasks,futures):
                if f in pending:
                    p=root/(t['case']['name']+'/'+t['label']+'.isolation.json')
                    if p.exists():
                        unit=read(p)['unit']
                        if not unit.startswith('acg67-'):raise RuntimeError('Unexpected unit ownership')
                        subprocess.run(['sudo','-n','systemctl','stop',unit],check=False)
        pool.shutdown(wait=True)
    rows=[f.result() for f in futures]
    write(barrier/'rows.json',rows);event('all-exported-and-stopped',released=released)
    return rows,released,error
