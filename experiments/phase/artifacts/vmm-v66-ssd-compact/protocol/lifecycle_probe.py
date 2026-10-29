#!/usr/bin/env python3
"""Optional real tool counters. Unsupported events stay unavailable, not zero."""
import ctypes
import fcntl
import os
import platform
import struct
import time

# perf_event.h: HW cycles/instructions and LL/read/{access,miss}.
EVENTS={'cycles':(0,0),'instructions':(0,1),'ll_read_access':(3,2),'ll_read_miss':(3,2|(1<<16))}

class PhaseCounters:
    def __init__(self, granite_raw=False):
        self.fds={};self.errors={};self.started=0
        self.events=dict(EVENTS)
        if granite_raw:
            self.events.update(retired_load=(4,0x81d0), retired_load_l3_miss=(4,0x20d1))
    def start(self):
        if platform.machine()!='x86_64':
            self.errors['architecture']='x86_64 syscall adapter required';return self
        libc=ctypes.CDLL(None,use_errno=True);libc.syscall.restype=ctypes.c_long
        for name,(kind,config) in self.events.items():
            attr=bytearray(128)
            # Disabled, inherit forked tools, user-space only, timing metadata.
            struct.pack_into('IIQQQQQ',attr,0,kind,128,config,0,0,3,1|2|(1<<5)|(1<<6))
            buf=(ctypes.c_ubyte*128).from_buffer(attr)
            fd=libc.syscall(298,ctypes.byref(buf),0,-1,-1,8) # CLOEXEC
            if fd<0:
                err=ctypes.get_errno();self.errors[name]={'errno':err,'message':os.strerror(err)};continue
            try:
                fcntl.ioctl(fd,0x2403,0);fcntl.ioctl(fd,0x2400,0)
                self.fds[name]=fd
            except OSError as error:
                self.errors[name]=repr(error);os.close(fd)
        self.started=time.monotonic_ns();return self
    def finish(self):
        output={'scope':'calling process and descendants forked during this tool; user-space only',
                'wall_ns':time.monotonic_ns()-self.started if self.started else 0,
                'events':{},'errors':self.errors}
        for name,fd in self.fds.items():
            try:
                fcntl.ioctl(fd,0x2401,0)
                data=os.read(fd,24)
                if len(data)!=24:raise ValueError('short perf read')
                value,enabled,running=struct.unpack('QQQ',data)
                output['events'][name]={'raw':value,'time_enabled_ns':enabled,'time_running_ns':running,
                    'running_fraction':running/enabled if enabled else None,
                    'scaled':value*enabled/running if running else None}
            except (OSError,ValueError) as error:output['errors'][name]=repr(error)
            finally:os.close(fd)
        self.fds.clear()
        ref=output['events'].get('ll_read_access',{});miss=output['events'].get('ll_read_miss',{})
        # Independently multiplexed events need adequate exposure. This is a
        # generic LL read-event ratio, not a measured DDR/CXL request fraction.
        reliable=all(x.get('running_fraction',0) is not None and x.get('running_fraction',0)>=.8 for x in [ref,miss])
        if reliable and ref.get('scaled'):
            output['ll_read_miss_ratio']=miss['scaled']/ref['scaled']
            output['ll_ratio_valid']=0<=output['ll_read_miss_ratio']<=1
        else:output['ll_read_miss_ratio']=None;output['ll_ratio_valid']=False
        ref=output['events'].get('retired_load',{});miss=output['events'].get('retired_load_l3_miss',{})
        reliable=all(x.get('running_fraction',0) is not None and x.get('running_fraction',0)>=.8 for x in [ref,miss])
        output['retired_load_l3_miss_ratio']=miss['scaled']/ref['scaled'] if reliable and ref.get('scaled') else None
        output['retired_load_l3_ratio_valid']=output['retired_load_l3_miss_ratio'] is not None and 0<=output['retired_load_l3_miss_ratio']<=1
        output['raw_event_source']='Intel perfmon GNR v1.21; ALL_LOADS includes software prefetch; L3_MISS counts instructions with at least one missing uop' if 'retired_load' in self.events else None
        return output


#!/usr/bin/env python3
"""Ordinary unmodified Python allocation: concurrent forks, no mbind helper."""
import json
import socket
import subprocess
import time
from pathlib import Path

BASE = Path('/sys/kernel/mm/demeter/sandboxes/0')
CG = Path('/sys/fs/cgroup/probe')
result = {'complete':False}
children = []

def event(action):
    with socket.socket(socket.AF_UNIX) as c:
        c.connect('/tmp/probe-controller.sock')
        c.sendall(json.dumps({'sandbox':'probe','event':action,'kind':3}).encode()+b'\n')
        r=json.loads(c.makefile('rb').readline()); assert 'error' not in r, r
        return r

def stats():
    return {k:int(v) for k,v in (line.split() for line in (BASE/'stats').read_text().splitlines())}

program = '''import json,sys
a=bytearray(32*1024*1024)
for i in range(0,len(a),4096): a[i]=(i//4096)%251
print(json.dumps({'ready':True}),flush=True)
sys.stdin.readline()
assert all(a[i]==(i//4096)%251 for i in range(0,len(a),4096))
print(json.dumps({'verify':True}),flush=True)
'''
try:
    Path('/sys/module/PhaseTieing_placement/parameters/sandbox_placement_enabled').write_text('0')
    result['start']=event('start'); before=stats()
    for _ in range(8):
        child=subprocess.Popen(['bash','-c','echo $$ > "$1/cgroup.procs"; exec /usr/bin/python3 -c "$2"',
                                'runner',str(CG),program],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
        children.append(child)
    for c in children: assert json.loads(c.stdout.readline())['ready']
    time.sleep(.2); result['allocated']=stats(); result['numa']=(CG/'memory.numa_stat').read_text()
    enabled=bool(result['allocated']['allocation_enabled']); result['enabled']=enabled
    fast=result['allocated']['allocation_fast']-before['allocation_fast']
    slow=result['allocated']['allocation_slow']-before['allocation_slow']
    if enabled:
        assert 0 < fast <= result['start']['quota_pages'], result
        assert slow > 40000 and fast+slow > 65536, result
        anon=next(line.split()[1:] for line in result['numa'].splitlines() if line.startswith('anon '))
        fast_anon=int(next(v.split('=')[1] for v in anon if v.startswith('N0=')))
        assert fast_anon <= result['start']['quota_pages']*4096, result
        assert result['allocated']['allocation_remaining']==0, result
        # A residency update cannot hand out the spent phase allowance again.
        time.sleep(.2); assert stats()['allocation_remaining']==0
    result['end']=event('end')
    for c in children:
        c.stdin.write('check\n'); c.stdin.flush()
        assert json.loads(c.stdout.readline())['verify']; assert c.wait(timeout=5)==0
    # No task VMA identity survives exit. The rule remains until Sandbox teardown.
    time.sleep(.3); result['teardown']=stats()
    assert result['teardown']['tasks']==0 and result['teardown']['regions']==0
    result['complete']=True
    print('ALLOCATION_PROBE_PASS',json.dumps(result),flush=True)
finally:
    for c in children:
        if c.poll() is None: c.kill(); c.wait()
    Path('/tmp/probe-result.json').write_text(json.dumps(result,indent=2)+'\n')

assert result['allocated'].get('sample_wakeups',0)>0, result['allocated']
c=PhaseCounters(granite_raw=True).start()
v=bytearray(8<<20)
for i in range(0,len(v),64): v[i]=(i//64)%251
hardware=c.finish()
p=Path('/tmp/probe-result.json');d=json.loads(p.read_text());d['hardware_preflight']=hardware;p.write_text(json.dumps(d,indent=2)+'\n')
print('HARDWARE_PREFLIGHT',json.dumps(hardware),flush=True)

# Retire rules and perf producers while two processes keep faulting new pages.
# This checks RCU/reference and irq_work teardown, not quota performance.
subprocess.run(['kill',Path('/tmp/probe-controller.pid').read_text().strip()],check=True)
time.sleep(.1)
root=BASE.parent
(root/'nr_sandboxes').write_text('0')
stress_code="import time;\nfor n in range(100):\n a=bytearray(4<<20);a[0]=37;a[-1]=97;time.sleep(.01);assert a[0]==37 and a[-1]==97\nprint('CONTENT_OK',flush=True)"
jobs=[subprocess.Popen(['bash','-c','echo $$ > \"$1/cgroup.procs\"; exec /usr/bin/python3 -c \"$2\"','runner',str(CG),stress_code],stdout=subprocess.PIPE,text=True) for _ in range(2)]
try:
 for n in range(20):
  (root/'nr_sandboxes').write_text('1')
  (BASE/'cgroup').write_text('/probe')
  (BASE/'quota_pages').write_text('16384')
  (BASE/'residency').write_text('0 0')
  (BASE/'phase').write_text('1 3 1000')
  time.sleep(.005)
  (root/'nr_sandboxes').write_text('0')
 for c in jobs: assert c.stdout.readline().strip()=='CONTENT_OK' and c.wait(timeout=10)==0
 d['concurrent_rule_teardown_pass']=True
 p.write_text(json.dumps(d,indent=2)+'\n')
 print('CONCURRENT_RULE_TEARDOWN_PASS',flush=True)
finally:
 for c in jobs:
  if c.poll() is None:c.kill();c.wait()
 (root/'nr_sandboxes').write_text('0')

# Exercise module exit while ordinary children continue allocating.
jobs=[subprocess.Popen(['bash','-c','echo $$ > "$1/cgroup.procs"; exec /usr/bin/python3 -c "$2"','runner',str(CG),stress_code.replace('range(100)','range(300)')],stdout=subprocess.PIPE,text=True) for _ in range(2)]
try:
 for n in range(3):
  (root/'nr_sandboxes').write_text('1')
  (BASE/'cgroup').write_text('/probe')
  (BASE/'quota_pages').write_text('16384')
  (BASE/'residency').write_text('0 0')
  (BASE/'phase').write_text('1 3 1000')
  time.sleep(.03)
  current=stats()
  if n==0:
   assert current['lifecycle_wakeup_enabled']==1 and current['lifecycle_fork_notifications']>0
   d['lifecycle_stats']=current
  subprocess.run(['rmmod','PhaseTieing_placement'],check=True)
  subprocess.run(['insmod','/tmp/PhaseTieing_placement.ko','sandbox_sample_diagnostics=0','sandbox_load_event=33232'],check=True)
 for c in jobs:assert c.stdout.readline().strip()=='CONTENT_OK' and c.wait(timeout=10)==0
 d['live_module_teardown_pass']=True
 p.write_text(json.dumps(d,indent=2)+'\n')
 print('LIVE_MODULE_TEARDOWN_PASS',flush=True)
finally:
 for c in jobs:
  if c.poll() is None:c.kill();c.wait()
 (root/'nr_sandboxes').write_text('0')
