"""Independent semantic, physical and paired-latency audit for formal cohorts."""
import gzip,hashlib,importlib.util,json,re,statistics,sys
from pathlib import Path
H=Path(__file__).resolve().parent
W=H.parents[1]
ROOT=W/'results/PhaseTieing-concurrent-20261030-v9-startup-admission'
MIB=1<<20
sys.path.insert(0,str(W/'phase/all_traces_v67'))
from eligibility import check as fidelity
spec=importlib.util.spec_from_file_location('formal_signature',W/'phase/cold_media/analyze.py')
common=importlib.util.module_from_spec(spec);spec.loader.exec_module(common)
def open_log(path):
 path=Path(path)
 return path.open() if path.exists() else gzip.open(str(path)+'.gz','rt')
def read(path):
    path = Path(path)
    if path.exists():
        return json.loads(path.read_text())
    with gzip.open(str(path) + '.gz', 'rt') as f:
        return json.load(f)

def audit_slot(folder, job):
    target = folder / f"slot-{job['lane']}"
    reasons = []
    row = dict(slot=job['lane'], trace=job['case']['name'], complete=False, valid=False)
    if not (target / 'guest-out/result.json').exists():
        row['reasons'] = ['missing complete replay result']
        return row
    data = read(target / 'guest-out/result.json')
    calls = read(Path(job['case']['trace']) / 'tool_calls.json')
    if sorted(x['source_index'] for x in data['steps']) != list(range(len(calls))):
        reasons.append('partial trace')
    if data.get('phase_runtime_errors') or bool(data.get('phase_aware')) != (job['strategy'] in ['P','F']):
        reasons.append('phase runtime error/mismatch')
    if data.get('timing') != 'gaps' or data.get('speed') != 1:
        reasons.append('original waiting protocol mismatch')
    if (target / 'ssh-status').read_text().strip() != '0':
        reasons.append('replay runner nonzero')
    manifest = read(target / 'manifest.json')
    for name, digest in manifest['files'].items():
        source = target / 'protocol-inputs' / Path(name).name
        if not source.exists() or hashlib.sha256(source.read_bytes()).hexdigest() != digest:
            reasons.append('archived input hash mismatch: ' + Path(name).name)
    steps = [s for s in data['steps'] if not s.get('skipped')]
    row.update(complete=True, name=f"slot-{job['lane']}", e2e_s=data['e2e_s'],
        service_s=sum(s['elapsed_s'] for s in steps),
        execution_s=sum(s['execution_s'] for s in steps),
        executed_calls=len(steps), longest_tool_s=max((s['elapsed_s'] for s in steps), default=0),
        signature=common.signature(data), original_outcome_mismatches=data['outcome_mismatches'],
        reasons=reasons, valid=not reasons)
    # Checks recorded tool/test outcomes; does not execute or time a baseline.
    row = fidelity(folder, job['case'], row, baseline=None, persist=False)
    errors = list(row['reasons'])
    if not data.get('command_timeouts_disabled'):
        errors.append('protocol mismatch: command elapsed-time limit was enabled')
    if any(step.get('applied_timeout_s') is not None for step in steps if step['tool']=='Bash'):
        errors.append('protocol mismatch: Bash execution cutoff applied')
    kernel = (target / 'dmesg.log').read_text(errors='replace')
    if re.search(r'BUG:|Oops:|Kernel panic|general protection fault|oom-kill:|Out of memory: Killed process', kernel):
        errors.append('L2 kernel fault/OOM')
    start = data['e2e_started_monotonic_s']
    end = data['e2e_finished_monotonic_s']
    left = None
    area = dict(fast=0., cold=0.)
    peak = dict(fast=0., cold=0.)
    covered = 0.
    offsets = []
    guest_samples = []
    monitor_errors = []
    for line in open_log(target / 'host-capacity.jsonl'):
        point = json.loads(line)
        if point['event'] == 'monitor_error':
            monitor_errors.append(point['error'])
        if point['event'] != 'sample':
            continue
        if point.get('guest_fresh'):
            offsets.append(point['host_monotonic_ns'] - point['guest']['guest_monotonic_ns'])
        if start - 1 <= point['guest']['guest_monotonic_ns'] / 1e9 <= end + 1:
            guest_samples.append(point['guest'])
        if left is not None:
            lo, hi = left['guest']['guest_monotonic_ns']/1e9, point['guest']['guest_monotonic_ns']/1e9
            dt = max(0., min(end, hi) - max(start, lo))
            if dt and 0 < hi-lo <= 1 and left['guest_fresh'] and point['guest_fresh']:
                covered += dt
                for tier, node in [('fast', 'N0'), ('cold', 'N1')]:
                    value = left['rss_node_bytes'].get(node, 0) / MIB
                    area[tier] += value * dt
                    peak[tier] = max(peak[tier], value)
        left = point
    if monitor_errors:
        errors.append('observer error: ' + str(monitor_errors))
    if guest_samples and any(g['memory_events'].get('oom', 0) or g['memory_events'].get('oom_kill', 0) for g in guest_samples):
        errors.append('Sandbox memcg OOM')
    row.update(sampling_coverage=covered/(end-start),
        fast_mean_mib=area['fast']/covered if covered else None,
        cold_mean_mib=area['cold']/covered if covered else None,
        fast_sampled_peak_mib=peak['fast'], cold_sampled_peak_mib=peak['cold'],
        swap_peak_mib=max((s['swap_bytes']/MIB for s in guest_samples), default=0),
        pswpin_pages=guest_samples[-1]['vmstat'].get('pswpin',0)-guest_samples[0]['vmstat'].get('pswpin',0) if guest_samples else None,
        pswpout_pages=guest_samples[-1]['vmstat'].get('pswpout',0)-guest_samples[0]['vmstat'].get('pswpout',0) if guest_samples else None)
    if offsets:
        offset = statistics.median(offsets)
        row['l1_e2e_start_ns'] = start * 1e9 + offset
        row['l1_e2e_end_ns'] = end * 1e9 + offset
    row.update(valid=not errors, reasons=sorted(set(errors)))
    return row


def audit(label):
    folder=ROOT/'cohorts'/label
    state=read(folder/'status.json')
    jobs=read(folder/'jobs.json') if (folder/'jobs.json').exists() else []
    rows=[]
    for job in jobs:
        try:
            rows.append(audit_slot(folder,job))
        except Exception as error:
            rows.append(dict(slot=job['lane'],trace=job['case']['name'],
                complete=False,valid=False,reasons=['audit error: '+repr(error)]))
    left=min((r['l1_e2e_start_ns'] for r in rows if 'l1_e2e_start_ns' in r),
             default=state.get('release_monotonic_ns', 0))
    right=max((r['l1_e2e_end_ns'] for r in rows if 'l1_e2e_end_ns' in r),
              default=state.get('final_resources', {}).get('monotonic_ns', 0))
    live_min=None;live_max=0;peak_charge=0;swap_peak=0;minimum_available=None
    oom=False;points=0
    with open_log(folder/'l1-resources.jsonl') as stream:
        for line in stream:
            p=json.loads(line)
            if left and not left<=p['monotonic_ns']<=right:continue
            points+=1
            live=sum(len(v.get('vmms',[])) for k,v in p['domains'].items() if k.startswith('slot-'))
            live_min=live if live_min is None else min(live_min,live)
            live_max=max(live_max,live)
            parent=p['domains'].get('PhaseTieing-formal',{})
            peak_charge=max(peak_charge,int(parent.get('memory.current',0)))
            swap_peak=max(swap_peak,int(parent.get('memory.swap.current',0)))
            events=dict(line.split() for line in parent.get('memory.events','').splitlines())
            oom=oom or any(int(events.get(k,0)) for k in ['oom','oom_kill'])
            available=p['memory']['MemAvailable']
            minimum_available=available if minimum_available is None else min(minimum_available,available)
    dmesg=(folder/'l1-dmesg.log').read_text(errors='replace')
    oom=oom or bool(re.search(r'oom-kill:|Out of memory: Killed process|Memory cgroup out of memory',dmesg))
    passed=(state['result']=='completed' and len(rows)==state['n'] and all(r['valid'] for r in rows)
        and live_min==state['n'] and not oom and swap_peak==0)
    classification=('completed-valid' if passed else 'completed-semantic-failure') if state['result']=='completed' else (
        'resource-failure' if oom else 'censored-or-incomplete')
    result=dict(classification=classification,label=label,strategy=state['strategy'],medium=state['medium'],n=state['n'],
        repetition=state['repetition'],driver_result=state['result'],correctness_valid=sum(r['valid'] for r in rows),
        valid=passed,rows=rows,minimum_live_vmms=live_min,maximum_live_vmms=live_max,
        l1_oom=oom,l1_swap_peak_bytes=swap_peak,parent_charge_peak_mib=peak_charge/MIB,
        minimum_available_mib=minimum_available/MIB if minimum_available is not None else None,
        observation_points=points,reference=state.get('reference'),latency=None)
    if state.get('reference') and (ROOT/'cohorts'/state['reference']/'audit.json').exists():
        ref=read(ROOT/'cohorts'/state['reference']/'audit.json')
        paired=[]
        for row in rows:
            counterpart=next((r for r in ref['rows'] if r['slot']==row['slot'] and r['trace']==row['trace']),None)
            if counterpart and row.get('complete') and counterpart.get('complete'):
                same=json.dumps(row['signature'],sort_keys=True)==json.dumps(counterpart['signature'],sort_keys=True)
                paired.append(dict(slot=row['slot'],same_signature=same,
                    e2e_growth=row['e2e_s']/counterpart['e2e_s']-1,
                    service_growth=row['service_s']/counterpart['service_s']-1))
                if not same:result['valid']=False
        result['latency']=dict(reference_valid=ref['valid'],paired=paired,
            e2e_pass={str(e):bool(result['valid'] and ref['valid'] and len(paired)==state['n'] and
                all(p['e2e_growth']<=e for p in paired)) for e in [.05,.1,.15,.2]})
    (folder/'audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print('AUDITED',label,'valid='+str(result['valid']),
        'correct='+str(result['correctness_valid'])+'/'+str(state['n']),flush=True)
    return result
