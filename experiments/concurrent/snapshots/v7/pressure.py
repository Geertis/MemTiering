"""Run paired cold-tier pressure points, starting with PhaseTieing itself."""
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import sys
import time

from outer import ROOT, W, start, stop, ssh, save
from deploy import provision, transfer
import campaign

H = Path(__file__).resolve().parent
BASELINE = W / 'results/PhaseTieing-concurrent-20261029-v3-baseline'


def update(stage, **values):
    state = json.loads((ROOT/'status.json').read_text())
    state.update(stage=stage, updated_epoch=time.time(), **values)
    save(ROOT/'status.json', state)
    spec = state.get('current_spec', {})
    Path('/r0/home/geerties/HW/report/261029PhaseTieing并发超售正式实验进展.md').write_text(
        '# PhaseTieing 真实冷层压力实验进展\n\n'
        f"更新时间：{time.strftime('%Y-%m-%d %H:%M:%S %z')}；阶段：`{stage}`；当前组：`{state.get('active')}`。\n\n"
        '宽松全 DRAM 批次已停止，保留为执行参考，不计入 PhaseTieing 效果。\n\n'
        '本轮固定 N=8、36 vCPU，探索 7 GiB DRAM，每 L2 4 vCPU / 12 GiB 名义内存、一条轨迹，CPU 池互不重叠。'
        'CXL/SSD 分别使用全体共享的 64 GiB 冷层，PhaseTieing 与同介质均分上层配额对照（共用动态安全预留）；当前为探索轮，边界点再补三重复。\n\n'
        f"当前介质 `{spec.get('medium')}`，策略 `{spec.get('strategy')}`；已通过 {len(state.get('completed', []))} 组，"
        f"异常 {len(state.get('failed', []))} 组。\n\n"
        '跨 VM 上层使用现有 waterfill/settle_grants，实测释放后才授予额度；记录实际冷层驻留、迁移/交换、swap、配额收据和性能。'
        '嵌套 PEBS 限制仍存在，未获得真实样本时不声称验证了采样驱动的下层机制。\n\n'
        f'本批数据：`{ROOT}`。\n')
    print(time.strftime('%Y-%m-%d %H:%M:%S'), stage, values.get('active'), flush=True)


campaign.update = update


def plans():
    refs = json.loads((BASELINE/'matrix.json').read_text())
    ref = next(x for x in refs if x['label']=='v3-c0-n8-r1')
    matrix = []
    for dram in [7]:
        for medium, strategy in [('cxl','P'), ('cxl','F'), ('ssd','P'), ('ssd','F')]:
            matrix.append(dict(label=f'v7-{medium}-{strategy}-d{dram}-n8-r1',
                category='CAPACITY-EXPLORATION', strategy=strategy, medium=medium,
                dram_gib=dram, cxl_gib=64 if medium=='cxl' else 0, n=8,
                repetition=1, seed=ref['seed'], reference=None,
                exploratory=True, external_reference=str(BASELINE/'cohorts'/ref['label']/'audit.json'),
                quota_mib=6144 if strategy=='F' else None, slots=ref['slots']))
    save(ROOT/'matrix.json', matrix)
    return matrix


def prepare(spec):
    campaign.source_guard()
    start(spec['label'], spec['dram_gib'], spec['cxl_gib'])
    campaign.host_audit(spec['label'])
    provision()
    transfer([H, ROOT/'protocol.json', ROOT/'inputs/cases.json'], spec['label']+'-deployment')
    ssh(f'mkdir -p {ROOT}/plans {ROOT}/cohorts {ROOT}/_input_blobs; '
        + ('sudo mount /dev/vdc /pilot-swap; sudo chown hs:hs /pilot-swap' if spec['medium']=='ssd' else 'true'),
        check=True)
    for name in ['.scratch-*.ext4','.scratch-*.qcow2']:
        ssh(f"sudo find {W} -maxdepth 1 -name '{name}' -type f -delete", check=True)


def mechanism(label):
    folder=ROOT/'cohorts'/label
    pool=[]
    p=folder/'global-pool.jsonl.gz'
    if p.exists():
        with gzip.open(p,'rt') as stream:pool=[json.loads(line) for line in stream]
    cold=0
    swap=0
    acknowledged=set()
    phase_targets=set()
    grants=set()
    for p in folder.glob('slot-*/host-capacity.jsonl.gz'):
        with gzip.open(p,'rt') as stream:
            for line in stream:
                v=json.loads(line)
                if v.get('event')!='sample':continue
                cold=max(cold,v.get('rss_node_bytes',{}).get('N1',0))
                swap=max(swap,v['guest'].get('swap_bytes',0))
    for p in folder.glob('slot-*/guest-out/controller.jsonl.gz'):
        with gzip.open(p,'rt') as stream:
            for line in stream:
                v=json.loads(line)
                if v.get('global_grant_seq'):
                    acknowledged.add(p.parent.parent.name)
                    phase_targets.add(v.get('target_mib'))
                    grants.add(v.get('global_grant_mib'))
    result=dict(pool_ticks=len(pool), budget_mib=pool[-1]['budget_mib'] if pool else None,
        budget_ceiling_mib=pool[0]['budget_ceiling_mib'] if pool else None,
        minimum_budget_mib=min((v['budget_mib'] for v in pool),default=None),
        minimum_physical_headroom_mib=min((v['physical_headroom_mib'] for v in pool),default=None),
        total_grants_within_budget=bool(pool and all(sum(v['grants_mib'].values())<=v['budget_mib'] for v in pool)),
        guest_global_grant_acknowledgements=len(acknowledged),
        distinct_effective_targets_mib=sorted(phase_targets), distinct_grants_mib=sorted(grants),
        per_slot_cold_rss_peak_bytes=cold, per_slot_swap_peak_bytes=swap,
        actual_cold_tier_used=bool(cold or swap))
    save(folder/'mechanism-audit.json',result)
    return result


def main():
    with (ROOT/'pressure.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        save(ROOT/'pressure-process.json',dict(pid=os.getpid(),started_epoch=time.time()))
        completed=[]
        failed=[]
        budgets={}
        for spec in plans():
            budget_key=(spec['medium'],spec['dram_gib'])
            if budget_key in budgets:spec['payload_budget_mib']=budgets[budget_key]
            update('booting-tiered-pressure',active=spec['label'],current_spec=spec,completed=completed,failed=failed)
            try:
                prepare(spec)
                result=campaign.run(spec,existing_outer=True)
                m=mechanism(spec['label'])
                if m['budget_ceiling_mib'] is not None:budgets.setdefault(budget_key,m['budget_ceiling_mib'])
                if not result['valid'] or not m['actual_cold_tier_used'] or m['guest_global_grant_acknowledgements']!=8:
                    failed.append(spec['label'])
                    save(ROOT/'failure-alert.json',dict(epoch=time.time(),label=spec['label'],audit=result,mechanism=m))
                    update('failure-needs-immediate-diagnosis',active=None,failed=failed,completed=completed,
                        diagnostic_host=spec['label'])
                    return
                completed.append(spec['label'])
                update('tiered-pressure-cohort-passed',active=None,completed=completed,failed=failed)
                stop(spec['label'])
            except BaseException as error:
                save(ROOT/'infrastructure-alert.json',dict(epoch=time.time(),label=spec['label'],error=repr(error)))
                update('infrastructure-needs-immediate-diagnosis',active=None,error=repr(error),
                    diagnostic_host=spec['label'],failed=failed,completed=completed)
                raise
        update('tiered-pressure-pairs-complete',active=None,completed=completed,failed=failed)


if __name__=='__main__':main()
