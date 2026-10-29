#!/usr/bin/env python3
"""Build a self-contained Chinese report from frozen, audited experiment data."""
import argparse,hashlib,json,statistics as st
from pathlib import Path
W=Path(__file__).resolve().parents[2]
R=W/'results/three-modes-20261010'
p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);args=p.parse_args()
load=lambda p:json.loads(p.read_text())
C=R/'confirmation-v13';D=load(C/'analysis.json');assert D['finished']==D['planned'], 'confirmation incomplete'
S={(s['trace'],s['strategy']):s for s in D['summaries']}
assert all(s['valid'] for s in S.values()),'noncomparable confirmation: retain and investigate'
M=load(C/'mechanisms.json')['trials'];P=load(R/'validated-pilot-v3/analysis.json')
Q=R/'pressure-v14';QD=load(Q/'analysis.json')
F=R/'reporting5-v15';FD=load(F/'analysis.json')
I=R/'signals-v16';ID=load(I/'analysis.json')
for extra in [QD,FD,ID]:
 assert extra['finished']==extra['planned'] and all(s['valid'] for s in extra['summaries']), 'supplement incomplete or noncomparable'
QS={(s['trace'],s['strategy']):s for s in QD['summaries']}
FS={(s['trace'],s['strategy']):s for s in FD['summaries']}
IS={(s['trace'],s['strategy']):s for s in ID['summaries']}
CGD=load(C/'resident-gap.json');FGD=load(F/'resident-gap.json')
CG=CGD['trials'];FG=FGD['trials']
CC=load(C/'credits.json')['trials'];QC=load(Q/'credits.json')['trials']
ST=load(C/'stages.json')['trials']
SIGN=load(Q/'phase-signal.json')['trials']
labels={'dask-5510':'Dask','pydicom-2065':'Pydicom','numba-9636':'Numba','xonsh-local':'Xonsh-local'}
traces=['dask-5510','numba-9636','pydicom-2065']
link=lambda path,name:f'[{name}]({path})'
f=lambda x:f'{x:.1f}'
change=lambda x:f'{100*(x-1):+.1f}%'
saving=lambda x:'≈100%' if 0 < x < 0.0001 else f'{100*(1-x):.1f}%'
lines=[]
def add(s=''):lines.append(s)
def table(head,rows):
 add('|'+ '|'.join(head)+'|');add('|'+ '|'.join(['---']*len(head))+'|')
 for row in rows:add('|'+ '|'.join(map(str,row))+'|')
 add()
add('# 三模式完整 CXL 与阶段收益验证')
add()
fulls=[S[t,'full4096']['ratios'] for t in traces]
cxls=[S[t,'cxl']['ratios'] for t in traces]
add('**本轮已把“全CXL”基线改为整个Guest RAM都映射实际CXL，并将可迁移VMM私有驻留迁到CXL。** 主确认中，全CXL工具服务合计相对全DRAM变化为 '+change(min(v['service_s'] for v in cxls))+'～'+change(max(v['service_s'] for v in cxls))+'；当前full4GiB策略的整VMM DDR峰值节约为 '+saving(max(v['ddr_peak_mib'] for v in fulls))+'～'+saving(min(v['ddr_peak_mib'] for v in fulls))+'，服务合计变化为 '+change(min(v['service_s'] for v in fulls))+'～'+change(max(v['service_s'] for v in fulls))+'。这建立了可测的容量/时延折中，尚未证明所有轨迹都同时大幅省DRAM且时延基本不变。')
add()
add('本轮根据 [261010VMM驻留与低时延折中分析.md](/r0/home/geerties/HW/report/261010VMM驻留与低时延折中分析.md) 继续实现与实验。主对比限定为**全 DRAM、全 CXL、我们的阶段分层**；没有加权 static 放置实验。评价对象为整个 VMM 的 Host DRAM 驻留与真实工具服务时间，特别检验页面未来收益模型是否在同额度下带来额外价值。')
add()
add('**对核心idea的当前判断：分层有容量/时延折中价值，但单页未来收益模型尚无稳定额外时延收益；阶段信息的主要独立预测优势也尚未证实。** 五重复确认中，关闭PEBS与热页提升、保留启动前放置与WAIT回收的配置更接近低时延目标。专项又发现反应式的大时延损失主要与有限分配信用有关，因此下一步应先修正真实驻留记账与安全回补，再检验预测和跨VM共享。')
add()
add('本文的正式确认数据来自 75 个新 VM：Dask、Numba、Pydicom × 三模式及分层内两项消融 × 各 5 次独立重复。四核心轨迹三模式的 12 个预检 VM 单列，未混入确认样本。其他早期拓扑、环境失败和错误配置全部保留，不能作为正式收益证据。')
add()
add('另外完成28个同拓扑额度/阶段信号消融VM、18个128KiB空闲页报告探索VM，以及发现反应式信用混杂后追加的12个启动放置专项VM；每点均为2个独立VM，单独报告，不扩大75个确认VM的样本数。四批及有效预检合计145个新VM。')
add()
add('## 1. 先说明三种模式与实验顺序')
add()
add('Host 内核为 `6.10.0-demeterhost-loadfb-20261005`。每次新建一个 VM，4 vCPU 绑定 Host CPU 0–3；同一时刻仅一个实验 VM 在执行，使用 lane0，没有模拟并行，也没有用这轮结果推算可承载实例数。DDR 为 Host node0/1，CXL 为 node2；实验 DDR 分配到 node0。当前 CPU 在 node0，NUMA 距离 DDR0=10、CXL2=24，CXL 距 node1 的距离为14。结论限定在当前固定 CPU/内存拓扑，不能当作所有 CXL 部署的普遍时延。')
add()
add('这里DDR指Host本地DRAM；CXL内存也可使用DRAM芯片，但经CXL连接，统计时按物理节点区分。本机可见CXL mem0–3、region10与decoder，dax0.0/dax10.0的target_node均为2；'+link(R/'hardware-memory-topology.json','硬件拓扑记录')+'与'+link(R/'host-environment.json','实验初始Host环境')+'保留了核验依据。')
add()
table(['模式','启动区：PXM1 / Linux Guest node1','CPU/工具区：PXM2 / Linux Guest node0','VMM 私有分配及回放前放置','阶段模块'],[
 ['全 DRAM','Host DDR0','Host DDR0','Host DDR0','不加载'],
 ['全 CXL','Host CXL2','Host CXL2','Host CXL2','不加载'],
 ['我们的分层','Host CXL2','Host DDR0','Host CXL2','加载，fast=Guest0 / slow=Guest1']])
add('两区各 8 GiB 原始容量，balloon 各保留 2 GiB，名义可用各 6 GiB；所有组容量固定，free-page reporting开启；主确认、压力与专项使用order9（2MiB），物理归还探索组统一使用order5（128KiB）。Guest swap、THP、自动 NUMA balance/demotion 关闭，保留相同根盘与工作负载黄金镜像。端点工具都绑定 Linux Guest node0；两区在端点分别映射同一种物理介质，工具区的 6 GiB 在本轮没有 OOM。观察器中的 `CAPACITY_POLICY=static` 只表示容量不变，**不是 static 放置组**。')
add()
add('端点manifest中的逻辑策略名`dram`表示绑定Guest node0；全CXL组同样使用它以保持Guest工具节点一致。物理介质由zone→Host节点映射决定：全CXL的两区均为Host2，所以不能根据这个逻辑名字判断是否用了DDR。')
add()
add('不能把 ACPI PXM 编号当成 Linux node 编号。CPU 归属移到 PXM2 后，Linux 将它命名为 node0；早期 v12 的两次分层运行沿用旧编号，物理 fast/slow 颠倒，已标记无效。正式运行前通过 SRAT、vm.info zone 映射推导实际 Guest→Host 节点关系，检查通过后才加载模块、回放工具。')
add()
add('每个 VM 从不可变镜像建立独立工作盘及根盘覆盖层。轨迹内部保留原始 source_index、工具顺序与命令；只回放有 Sandbox 副作用的支持工具，TodoWrite 等记录保留但标记跳过。正式组为10x：原始绝对开始偏移除以10，实际工具执行不加速；工具串行，运行赶不上计划时积累 schedule lag。长 WAIT 会缩短或被积压吃掉，所以不使用回放端到端时长替代工具时延。')
add()
plan=load(C/'plan.json')
first=[j for j in plan['jobs'] if j['repeat']==1]
add('第1重复的轨迹顺序为 '+ ' → '.join(dict.fromkeys(labels[j['trace']] for j in first))+'；每轨迹配置顺序为 DRAM → CXL → full4096 → heat4096 → reclaim4096。后续重复用固定随机种子打乱轨迹顺序，并逐轮轮转配置顺序，5种配置各占一次同组位置。每次重复都重启新 VM，不是在同一个 VM 内跑5遍。完整顺序、源码与镜像哈希在 '+link(C/'plan.json','冻结 plan.json')+'。')
add()
table(['集合','设置','目的'],[
 ['v11/v12与环境预检','不同 CPU/启动区拓扑、旧兼容层及失败配置，分别保留','排除物理节点错配和未执行有效测试的路径，不能混算额度曲线'],
 ['validated-pilot-v3','4轨迹 × DRAM/CXL/full4096 × 1独立VM = 12','冻结共同环境，核验完整 CXL 和选择4GiB保护额度'],
 ['confirmation-v13（协议14）','3轨迹 × DRAM/CXL/full4096/heat4096/reclaim4096 × 5 = 75','独立确认三模式折中，并检验未来收益模型与在线采样是否必要'],
 ['pressure-v14','Dask/Pydicom × DRAM/full4096/full2048/heat2048/reclaim2048/reactive4096/alloc4096 × 2 = 28','同批建立额度折中，并分离真实阶段信号、初始分配与回收贡献'],
 ['reporting5-v15','3轨迹 × DRAM/CXL/reclaim4096 × 2 = 18；各组reporting order5','检查Guest已释放页在Host残留，探索更细粒度物理归还的收益与时延'],
 ['signals-v16','Dask × DRAM/reactive4096/alloc4096/reactivealloc4096/eventpref4096/reactivepref4096 × 2 = 12；order9','分离真实阶段信息与有限匿名页信用更新的影响']])
add('确认目录沿用 `confirmation-v13` 名称，但冻结协议明确记录 version14，实际模块为 `vmm-v14-future-ablation`，不与旧版本混用。')
add()
add('## 2. 全 CXL 的物理核验与统计口径')
add()
cxl=[t for t in D['trials'] if t['strategy']=='cxl'];ddr=[t for t in D['trials'] if t['strategy']=='dram']
private_lo=min(t['ddr_peak_mib'] for t in cxl)*1024
private_hi=max(t['ddr_peak_mib'] for t in cxl)*1024
private_range=f'{private_lo:.0f}' if private_lo==private_hi else f'{private_lo:.0f}–{private_hi:.0f}'
add(f'正式全 CXL 的 {len(cxl)} 个 VM 在整个观察窗口内 **Guest RAM 的 DDR 驻留均为0**；全 DRAM 的 Guest RAM CXL 驻留也均为0。核验覆盖两区共16GiB的完整 memfd 映射，而不是只检查工具 cgroup。第一内存区跨 PCI 地址洞，可表现为3段映射，审计依据完整映射跨度而不是固定段数。')
add()
add(f'回放前按该实验 VMM 的 PID 查询并迁移非 Guest-RAM 的驻留页，Guest-RAM 映射明确排除。全 CXL 回放窗口内整个 VMM 的 DDR 峰值为 **{private_range} KiB**；这些残留仍计入成本，不能写成绝对0。共享/不可迁移 Host 映射可返回拒绝状态；启动到结束的 DDR 峰值仍可包含回放前的约3MiB Host 映射，迁移状态和耗时保存在每个 VM 的 `vmm-private-placement.json`。因此本轮“全CXL”是全部 Guest RAM 与可迁移 VMM 私有驻留放在CXL，剩余极少的 Host 映射明确列账。表中小于0.1MiB的驻留可能显示0.0MiB，接近100%的节约统一写为≈100%，不表示整个VMM绝对零DDR。')
add()
add('新分配策略不自动迁走已有驻留页，因此只看 numactl 启动参数不足以证明全CXL；本轮依据实际 /proc/PID/numa_maps 和 vm.info 核验。[Linux 6.10 NUMA 内存策略](https://www.kernel.org/doc/html/v6.10/admin-guide/mm/numa_memory_policy.html)')
add()
add('主容量指标为完整 VMM RSS 在 Host DDR node0/1 的回放窗口采样峰值及按时间积分的均值，分别解释同时承载的峰值压力和时间复用机会。Host每250ms采样，Guest payload每50ms；通过 Guest 单调时钟对齐窗口，覆盖率≥95%、间隔≤1s。启动到回放结束的峰值另存，不能把工具窗口小峰值当成启动容量上界。VMM RSS 包括共享映射，但不含全部 Host KVM/内核及磁盘页缓存成本，仍不能称作整个Host的完整物理成本。')
add()
add('工具服务时间包含阶段通知与控制步骤，同时报告纯执行合计和预先指定的同一长调用：Dask76、Numba9、Pydicom32、Xonsh21。+5% 合计与长调用只作为拟议筛选线，不是用户已约定的SLO。5个VM的配对重复块 bootstrap（10000次）区间是描述性不确定性，不能证明统计等价或生产p99。')
add()
add('## 3. 三模式独立确认结果')
add()
rows=[]
for trace in traces:
 b=S[trace,'dram']['medians']
 rows.append([labels[trace],f(b['ddr_peak_mib']),f(b['ddr_mean_mib']),f"{b['service_s']:.3f}",f"{b['selected_service_s']:.3f}"])
table(['轨迹','DRAM峰值 MiB','DRAM均值 MiB','工具服务合计 s','匹配长调用 s'],rows)
rows=[]
for trace in traces:
 for strategy in ['cxl','full4096']:
  s=S[trace,strategy];v=s['ratios']
  rows.append([labels[trace],'全CXL' if strategy=='cxl' else '分层full / q4GiB',saving(v['ddr_peak_mib']),saving(v['ddr_mean_mib']),change(v['service_s']),change(v['selected_service_s'])])
table(['轨迹','模式','DRAM峰值节约','DRAM均值节约','服务合计变化','匹配长调用变化'],rows)
add('全部数值为同配置5个独立VM中位数之比，预检数据没有混入。完整CXL证明容量收益确实存在，但仍有明显工具时延代价；“能把内存全部放CXL”不能直接推出“无需DRAM就能达到接近原生性能”。')
add()
table(['轨迹','DRAM服务合计观测范围 s','DRAM长调用观测范围 s'],[[labels[t],f"{S[t,'dram']['ranges']['service_s'][0]:.3f}–{S[t,'dram']['ranges']['service_s'][1]:.3f}",f"{S[t,'dram']['ranges']['selected_service_s'][0]:.3f}–{S[t,'dram']['ranges']['selected_service_s'][1]:.3f}"] for t in traces])
add('保留全部重复及原生波动；没有删除不利配置或以第一次DRAM值作为统一分母。比值点使用配置中位数之比，bootstrap按同一重复块重抽样，区间可能较宽。')
add()
rows=[]
for trace in traces:
 for strategy in ['full4096','reclaim4096']:
  s=S[trace,strategy];cis=s['bootstrap_95ci']
  interval=lambda k:'['+', '.join(change(v) for v in cis[k])+']'
  rows.append([labels[trace],strategy,change(s['ratios']['service_s']),interval('service_s'),change(s['ratios']['selected_service_s']),interval('selected_service_s'),'是' if s['within_5pct_screen'] else '否'])
table(['轨迹','分层配置','服务合计变化','描述性95%区间','长调用变化','描述性95%区间','中位数同时≤+5%'],rows)
add('筛查线附近不能只报“约5%”并自动准入；应检查长调用与区间上界。扩大DRAM保护可减小时延，但单实例峰值节约也会变小，尤其是Pydicom的大匿名页突发。当前被保护的ACTIVE工作集与整VMM成本必须一起计费。')
add()
add('![三模式完整VMM峰值与工具时延]('+str(C/'peak-tradeoff-main.png')+')')
add()
add('![三模式完整VMM均值与工具时延]('+str(C/'mean-tradeoff-main.png')+')')
add()
add(link(C/'trials.csv','逐VM数值')+'；'+link(C/'tools.csv','逐调用时间')+'；'+link(C/'peak-tradeoff.pdf','峰值图PDF')+'；'+link(C/'mean-tradeoff.pdf','均值图PDF')+'。图中绿色仅表示合计服务时间的+5%区间，不能替代长调用检查。')
add()
add('## 4. 当前实现与消融：未来收益估计是否有效')
add()
add('上层仍以 Sandbox 为单位，在工具启动前接收真实start事件并预授 headroom；非search工具的初始保护上限为4GiB，search沿用已完成阶段的增长历史。ACTIVE、WAIT分别改变quota，WAIT最低16MiB，只有观测到payload真实驻留下降才回收对应payload信用。仍保留单阶段有限匿名页分配信用；文件页、Guest系统页、VMM私有页和Host backing不等于该信用，不能把它当作整Host守恒账本。')
add()
add('q4GiB是控制器的payload额度，不代表整VMM RSS被硬限制在4GiB。新匿名页由有限信用约束；preferred-node的文件页分配、系统页和异步驻留反馈仍可能使实际快层成本超过这个数字。因此本文只按测到的整VMM DDR比较，不用quota值替代成本。')
add()
add('下层保留每个mm的地址、generation及映射epoch，汇聚同Sandbox匿名冷热候选，允许跨mm交换。收益近似为访问率×未来时域×80ns层间差×0.25关键度；时域由已完成阶段及同comm/kind的已完成mm生命周期估计，未知mm先验500ms，页面历史只在同mm/地址及有效映射内复用。迁移复制与交换分别估计实际耗时并设置获利门槛；80ns与0.25是当前固定近似输入，本轮没有证明其真实预测准确率。')
add()
add('该收益模型当前仅覆盖匿名候选；文件页采用WAIT中的clean/unmapped/unreferenced回收路径，没有对应文件热页的收益提升模型。阶段quota权重的性能敏感度仍为固定1，尚未在线学习。只有一个Sandbox控制槽与Guest payload账本，没有Host层多VM共享DRAM硬预算。')
add()
add('按初始复制代价20µs和固定有效层间差20ns计算，单页至少约1000次未来访问才能覆盖复制；500ms时域对应约2000次/秒，5ms剩余寿命则约20万次/秒。这是当前公式的门槛解释，不是测到的真实收益。复制代价会在线更新，失败操作也会抬高估计；当前单页copy估计混合提升/下沉，若出现高代价失败可能让后续准入更保守，下一轮需要单独校准成功代价与失败概率。')
add()
table(['配置','保留','关闭或替换','回答的问题'],[
 ['full4096','阶段通知、初始分配、PEBS、WAIT回收、收益排序/门槛','无','完整当前策略'],
 ['heat4096','相同阶段、quota、初始分配、采样与迁移预算/冷却','未来时域、生命周期与历史复用；收益门槛置0，按当前热度排序','未来收益模型作为整体是否优于只看当前热度'],
 ['reclaim4096','阶段通知、初始分配、50ms地址空间发现、WAIT/quota回收','PEBS创建及其采样/生命周期即时唤醒；无热页驱动提升','需要在线热页识别吗，还是分配与回收已经足够']])
add('reclaim并非关闭整个迁移模块，也不是纯allocation-only。它同时移除了采样及即时唤醒路径，不能把其所有时延差精确归因于PEBS IRQ。heat是未来收益特征和成本门槛的组合消融，不能单独声称生命周期或历史复用分别有效。相同quota也不代表相同实际DDR成本，以下同时列驻留与时延。')
add()
rows=[]
for trace in traces:
 base=S[trace,'full4096']['medians']
 for variant in ['heat4096','reclaim4096']:
  s=S[trace,variant];v=s['medians']
  rows.append([labels[trace],variant,f(v['ddr_peak_mib']),f(v['ddr_mean_mib']),change(v['service_s']/base['service_s']),change(v['selected_service_s']/base['selected_service_s'])])
table(['轨迹','消融','实际DDR峰值MiB','实际DDR均值MiB','服务相对full变化','长调用相对full变化'],rows)
rows=[]
for s in D['ours_comparisons']:
 if not s['valid']:continue
 cis=s['bootstrap_95ci']
 interval=lambda k:'['+', '.join(change(v) for v in cis[k])+']'
 rows.append([labels[s['trace']],s['strategy'],change(s['ratios']['service_s']),interval('service_s'),change(s['ratios']['selected_service_s']),interval('selected_service_s')])
table(['轨迹','消融相对full','服务变化','描述性95%区间','长调用变化','描述性95%区间'],rows)
add('heat相对full为正表示full的时间较短，但需要结合驻留差与区间判断；区间包含0时，不能宣称稳定时延优势。reclaim更快也不能自动证明未来收益模型无用：它同时改变采样与唤醒成本，须结合受限额度组判断需要在线提升的场景。')
add()
add('**4GiB确认组目前不支持“未来收益模型带来稳定额外时延收益”。** 三轨迹full与heat的服务中位数差均不足0.4%，配对区间均跨过0。Dask的full相对heat有约3.3%的DDR峰值下降，但均值略高，属于小幅容量折中；Numba/Pydicom未形成稳定的模型优势。reclaim相对full的服务合计分别短约2.8%、3.6%、3.7%；Numba付出约6%–7%的更高DDR成本，Pydicom同时有更低峰值、均值及服务时间。此处不能把所有收益都归给页面预测。')
add()
rows=[]
for trace in traces:
 for variant in ['full4096','heat4096','reclaim4096']:
  ts=[t for t in M if t['trace']==trace and t['strategy']==variant]
  med=lambda k:st.median(t[k] for t in ts)
  rows.append([labels[trace],variant,f"{med('active_decisions_with_hot')/max(1,med('active_decisions'))*100:.1f}%",int(med('promotions')),int(med('exchanges')),int(med('demotions')),int(med('file_demotions')),int(med('candidate_benefit_le_cost_checks')),f"{med('worker_cpu_s'):.3f}"])
table(['轨迹','配置','ACTIVE有热候选比例*','成功提升','成功交换','总下沉','其中文件下沉','收益≤复制成本检查次数','worker CPU s'],rows)
add('*决策与检查会反复考察相同页面，不是独立样本或唯一页数；比例为两项中位数之比。复制/交换次数不能换算为节省的工具时间。文件下沉在本轮主要发生于WAIT；这些成功次数不是已归还Host的容量信用。样本事件级诊断未启用，不能用“没有后续采样记录”证明迁移后没有复用。')
add()
add(link(C/'mechanisms.csv','逐VM机制计数')+'；'+link(C/'mechanisms.json','机制分析与限制')+'。')
add()
add('## 5. 四轨迹预检、共同环境与保真边界')
add()
rows=[]
for trace in ['dask-5510','numba-9636','pydicom-2065','xonsh-local']:
 for strategy in ['cxl','full4096']:
  s=next(s for s in P['summaries'] if s['trace']==trace and s['strategy']==strategy);v=s['ratios']
  rows.append([labels[trace],strategy,saving(v['ddr_peak_mib']),saving(v['ddr_mean_mib']),change(v['service_s']),change(v['selected_service_s'])])
table(['轨迹','单VM预检','峰值节约','均值节约','服务变化','长调用变化'],rows)
add('Xonsh source_index21 已恢复695个测试全部通过，不再以collected0的失败短路径衡量分层收益。Dask的legacy `pytest.warns(None)` 由共同兼容层恢复记录warning的旧行为，真实Warning类型检查仍交给原pytest，未修改测试断言；其长调用76仍为443 passed/1 failed，属于实际执行后的失败路径，不能宣称任务完整成功。Numba9完成构建；Pydicom32为337 passed/38 skipped。其他原始安装、只读前缀与脚本错误保留，工具合计含这些调用；匹配的长构建/测试另列。')
add()
add('对warns(None)采用旧版本实际实现的语义：None时记录但不要求出现某种warning。Xonsh收集器使用pathlib兼容当前接口。三种模式应用相同兼容层、相同镜像与命令，并核验退出状态、最终git diff/status、Read/Grep/Glob输出及可见测试数量。[pytest6.2实际实现](https://docs.pytest.org/en/6.2.x/_modules/_pytest/recwarn.html)，[pytest接口迁移说明](https://docs.pytest.org/en/stable/deprecations.html)')
add()
add('旧v11的启动CPU位于CXL区、q2GiB且共同环境不同；它的DRAM节约不能与新版q4GiB直接连接成额度曲线。旧payload峰值下降到25%与新版整VMM数值也不是同一口径。所有早期产物留在 '+link(R,'本轮结果目录')+'，错误节点配置和预检退出均与正式确认分开。')
add()
add('## 6. 同拓扑额度压力与阶段信号消融')
add()
rows=[]
for t in ['dask-5510','pydicom-2065']:
 for v in ['full4096','full2048','heat2048','reclaim2048','reactive4096','alloc4096']:
  s=QS[t,v];r=s['ratios'];m=s['medians']
  rows.append([labels[t],v,f(m['ddr_peak_mib']),saving(r['ddr_peak_mib']),saving(r['ddr_mean_mib']),change(r['service_s']),change(r['selected_service_s'])])
table(['轨迹','配置','DDR峰值MiB','峰值节约','均值节约','服务变化','长调用变化'],rows)
add('这28个VM使用与确认组相同的CPU/Guest节点映射、模块14与order9，但有自身的同批DRAM对照。每点仅2个VM，适合辨别较大的机制差异与选择下一轮范围，不能证明5%统计等价；不能把两批数据拼成n=7或把2GiB点与旧v11比较。')
add()
add('Pydicom q2GiB的三个点均将DDR峰值降到原生约50%，但长调用仍增加约18%–22%；full/heat差异很小，完整未来收益估计没有恢复接近DRAM的执行时间。Dask q2的full与q4实际峰值接近，额度减半不等于驻留减半。')
add()
add('纯初始分配alloc4GiB已取得Dask峰值17.3%/均值25.3%、Pydicom峰值8.5%/均值17.9%的节约，服务变化均约+2%。这说明启动区CXL映射与初始工具保护已经贡献了大部分基础收益；不能将full的全部节约归于未来收益排序或WAIT迁移。Dask full还有更多峰值节约，但也更慢；Pydicom full与alloc的驻留非常接近。以上为本批2VM探索证据。')
add()
add('`reactive4096`关闭真实工具阶段事件，控制器按50ms周期和调用请求时的CPU/内存增量推断ACTIVE，静默500ms后推断WAIT；保留当前初始分配与页管理机制。工具启动时继承当时返回的preferred node，后续阶段变化不会自动改写既有子进程的文件页分配策略，因此其差异也包含初始放置时机。它检验当前反应式实现相对真实事件的差别，不代表所有反应式SOTA的最优结果。`alloc4096`保留真实阶段通知与新页放置，但同时关闭PEBS、地址空间发现与下沉/提升，用来分离启动保护与WAIT回收；它不是第三种主模式之外的新主基线。')
add()
rows=[]
for t in ['dask-5510','pydicom-2065']:
 for v in ['full4096','reactive4096','alloc4096']:
  rs=[r for r in SIGN if r['trace']==t and r['strategy']==v]
  nodes=[r['selected_hint']['preferred_node'] for r in rs]
  rows.append([labels[t],v,f"{st.median(r['fast_preference_fraction'] for r in rs)*100:.1f}%",str(nodes)])
table(['轨迹','配置','工具开始时prefer-fast比例','两次匹配长调用的preferred Guest node'],rows)
add('此处0为DDR快区、1为CXL慢区；preferred是初始决策而非硬绑定，实际新页仍可能fallback。反应式消融保留通信接口，但同时去掉工具类型与真实边界信息，不能把差异全部归成一个50ms检测延迟，也不使用未来trace时间作为在线输入。两个配置的Dask匹配长调用实际都在启动时prefer-fast，说明61%时延差不能单凭该调用的启动节点解释。')
add()
table(['轨迹','配置','ACTIVE信用耗尽快照比例','耗尽且quota至少余20%比例'],[[labels[t],v,f"{st.median(r['zero_credit_snapshots']/max(1,r['active_snapshots']) for r in QC if r['trace']==t and r['strategy']==v)*100:.1f}%",f"{st.median(r['zero_credit_at_least20pct_quota_free_snapshots']/max(1,r['active_snapshots']) for r in QC if r['trace']==t and r['strategy']==v)*100:.1f}%"] for t in ['dask-5510','pydicom-2065'] for v in ['full4096','full2048','reactive4096']])
add('快照比例用于诊断单阶段有限信用，不是独立样本或可补授的释放凭据。累计分配与活跃驻留不同；不能依据暂时余量直接重复授予额度。')
add()
add('**Dask反应式对照存在重要实现混杂：** 约46%的ACTIVE快照中信用已为0，但quota仍至少空余20%。反应式将连续工具视作同一ACTIVE阶段时，不会像真实start/end那样刷新单阶段信用。因此+61%不是干净的“不能提前感知热点”证据；它还反映了当前信用规则与阶段划分的相互作用。')
add()
add('追加的12VM专项采用2×2设计：真实事件/反应式识别 × 开启/关闭匿名分配信用。四个配置都关闭PEBS与迁移，保留相同通信与MPOL_PREFERRED路径：alloc/reactivealloc保留有限信用，eventpref/reactivepref关闭匿名分配hook。另建同批DRAM与原reactive正对照。关闭信用的配置不施加匿名页硬quota，不能作为多VM共享预算方案；全部按真实DDR驻留核算，用于分离机制。')
add()
rows=[]
for v in ['dram','reactive4096','alloc4096','reactivealloc4096','eventpref4096','reactivepref4096']:
 s=IS['dask-5510',v];r=s['ratios'];m=s['medians']
 rows.append([v,f(m['ddr_peak_mib']),saving(r['ddr_peak_mib']),saving(r['ddr_mean_mib']),change(r['service_s']),change(r['selected_service_s'])])
table(['Dask专项配置','DDR峰值MiB','峰值节约','均值节约','服务变化','匹配长调用变化'],rows)
add('专项原reactive复现约+28.6%合计/+61.0%长调用；仅关闭PEBS和迁移的reactivealloc仍约+26.0%/+59.9%。在相同反应式识别下，再关闭有限匿名分配信用的reactivepref降到约+2.3%/+2.2%，同时DDR峰值较reactivealloc增加约4.7%、均值增加约22.9%。因此大时延差与信用限制直接相关，改善并非完全没有容量代价；关闭hook也不能代替安全的驻留额度回补。')
add()
add('无信用hook的eventpref为约+1.1%合计/+1.7%长调用，reactivepref为约+2.3%/+2.2%；eventpref的DDR峰值和均值分别比reactivepref高约6.5%和10.4%。当前Dask并未形成“显式阶段信息在相同真实DDR成本下明显优于反应式”的干净证据。真实边界对当前有限信用刷新有价值，但其大收益不能直接改称热点形成前预测收益。反应式仍在相同RPC请求时测量CPU/内存，不能视为完全没有运行时交互的SOTA。')
add()
add(link(I/'analysis.json','专项完整分析')+'；'+link(I/'plan.json','专项输入与顺序')+'；'+link(I/'phase-signal.json','实际初始放置提示')+'。专项每点2VM，结论限于当前Dask路径与实现，不是所有反应式管理算法的结论。')
add()
add(link(Q/'analysis.json','压力组完整分析')+'；'+link(Q/'plan.json','压力组顺序与输入哈希')+'；'+link(Q/'credits.json','分配信用诊断')+'。')
add()
add('## 7. WAIT驻留差额与物理归还探索')
add()
add('WAIT中的payload可已经很小，VMM DDR仍有数百MiB。为避免把它全归为系统活页，使用保守下界：`max(0, Guest-RAM DDR RSS − (快区raw − 已确认balloon − 快区MemFree))`。它假定已确认balloon页的Host backing已解除；Guest/Host快照异步，结果只用于诊断，不能发放跨VM释放信用，也不能单凭它证明reporting碎片化。')
add()
rows=[]
for t in traces:
 for v in ['dram','full4096','reclaim4096']:
  rs=[r for r in ST if r['trace']==t and r['strategy']==v]
  med=lambda k:st.median(r[k] for r in rs if r[k] is not None)
  rows.append([labels[t],v,f(med('service_ddr_mean_mib')),f(med('idle_ddr_mean_mib')),f(med('idle_covered_s'))])
table(['轨迹','配置','工具服务期间DDR均值 MiB','回放idle期间DDR均值 MiB','idle覆盖时间 s'],rows)
add('阶段表按50ms payload样本中的tool_index区分服务与调度idle，再积分250ms Host RSS；它不是精确每调用边界，工具内部等待仍在服务时间内。10x绝对到达可能吞掉WAIT，尤其Dask，因此不能把该idle量外推到原始等待尺度或并行错峰。')
add()
rows=[]
for t in traces:
 for name,gaps in [('order9确认组',CG),('order5探索组',FG)]:
  rs=[r for r in gaps if r['trace']==t and r['strategy']=='reclaim4096']
  rows.append([labels[t],name,f(st.median(r['final_snapshot']['ddr_resident_mib'] for r in rs)),f(st.median(r['final_snapshot']['payload_fast_mib'] for r in rs)),f(st.median(r['final_snapshot']['resident_free_lower_mib'] for r in rs))])
table(['轨迹','reclaim4GiB','最后WAIT Guest-RAM DDR MiB','payload快层 MiB','已free仍驻留下界 MiB'],rows)
rows=[]
for t in traces:
 for name,data in [('order9确认组',CGD),('order5探索组',FGD)]:
  rs=[r['replay_deltas'] for r in data['reporting_activity'] if r['trace']==t and r['strategy']=='reclaim4096']
  med=lambda k:st.median(r.get(k,0) for r in rs)
  rows.append([labels[t],name,int(med('reporting_batches')),f(med('reporting_fast_pages')/256),int(med('reporting_failures'))])
table(['轨迹','报告粒度','回放期间报告批次数','累计报告Guest快区 MiB','报告失败数'],rows)
add('累计报告量包含同一物理页多次分配/释放，不是唯一页数，也不等于永久释放容量；只有VMM实际驻留下降才用于容量结果。报告批次计数没有CPU耗时字段，不能据此精确分摊reporting开销。')
add()
rows=[]
for t in traces:
 for v in ['cxl','reclaim4096']:
  s=FS[t,v];r=s['ratios'];m=s['medians']
  rows.append([labels[t],v,f(m['ddr_peak_mib']),saving(r['ddr_peak_mib']),saving(r['ddr_mean_mib']),change(r['service_s']),change(r['selected_service_s'])])
table(['轨迹','order5模式','DDR峰值MiB','峰值节约','均值节约','服务变化','长调用变化'],rows)
add('order5将最小报告块从2MiB改为128KiB，三种模式全部一致修改，仍使用同一模块与静态balloon容量；不能只改分层组后沿用order9的原生分母。本批各点2个新VM，所有比值对本批DRAM计算；跨批order9/order5差异可能同时含时段波动，是探索证据，尚不是交错重复的独立归因确认。更细回收的通用收益也不能全部归给页面收益模型。')
add()
add('本批order5的reclaim4GiB：Dask峰值/均值节约约18.2%/28.8%，服务/长调用约+2.0%/+0.5%；Numba约22.7%/33.0%、+1.6%/+0.9%；Pydicom约8.6%/21.7%、+3.1%/+2.0%。Numba最后WAIT中已free仍驻留下界由order9的约471MiB降至约158MiB，但仍有约625MiB Guest-RAM DDR，payload快层只有约16MiB。说明细粒度归还改善了一部分差额，剩余活页/系统驻留仍需另行计费。')
add()
add('![order5三模式峰值与时延]('+str(F/'peak-tradeoff-main.png')+')')
add()
add(link(F/'analysis.json','order5完整分析')+'；'+link(F/'plan.json','order5顺序与哈希')+'；'+link(F/'resident-gap.json','order5驻留差额诊断')+'。')
add()
add('## 8. 对idea的判断与下一步')
add()
add('**阶段信号用于启动前放置与额度更新有明确实现价值，但“提前感知热点的独立性能优势”尚未证明。** 2×2专项发现，当前反应式的大时延损失主要与连续ACTIVE阶段中的有限分配信用相互作用；去掉信用后，两种阶段识别的时延均接近DRAM，而实际DDR成本有所不同。需要先修正驻留/分配账本、保留相同安全额度约束，再比较标签与周期推断。正式确认仍只有一个Sandbox控制槽，没有证实多个VM间共享DRAM份额。')
add()
add('**页面未来收益模型要以消融和真实成本证明，而不能以DRAM下降或候选数量证明。** full与heat检验的是模型整体；reclaim检验的是在线识别的必要性。若reclaim达到相近驻留和更短时延，应让当前负载采用阶段分配与WAIT回收，把昂贵页面识别留给有持续慢层热点、足够寿命和可观复用的阶段。若full优于heat而不优于reclaim，可以说明获利门槛避免了热度误迁移，但仍未证明生命周期/历史复用带来额外正收益。')
add()
add('按5重复确认，**当前更有证据的低时延基础是启动前保护与WAIT回收，单页未来收益排序尚不能成为已验证的主要收益来源**。reclaim4GiB三轨迹的中位数均满足拟议+5%合计/长调用筛选，但Pydicom合计区间上界仍超过5%，不能宣称已证明等价。模型在Dask保留数百次有门槛提升、在Numba/Pydicom阻止热度版的提升，却没有稳定改善工具时间；减少迁移次数只是机制证据，不是实得净收益。')
add()
add('**低时延与大幅峰值节约尚需分别验证。** 初次分配到CXL不支付迁移成本，却可能支付清零、首次触页与带宽代价；短生命周期页面即使随后复用少，也未必适合初始放CXL。应把birth placement与已有页migration分开估计：前者估计工具初次触页与有效访问的DRAM价值，后者扣除复制/交换、扫描、采样及迁移后真正归还Host的成本，再按有限DRAM的边际价值排序。')
add()
add('后续推进顺序：')
add()
add('1. 优先分离活跃驻留额度、在途分配预留与累计分配信用。记录真实释放/再用，区分同VM已有DDR backing的复用和可发给其他VM的Host实际归还信用；保留防重复授予与fallback。完成安全回补后，重新匹配真实DDR成本比较显式事件与纯周期推断，避免把账本缺口当成预测优势。不能直接把hook关掉作为共享池方案。')
add('   同时保留本轮低开销Pareto候选，独立确认接近+5%的配置；Pydicom的q2大幅峰值节约仍伴随明显长调用上涨，当前没有“所有轨迹DRAM大幅下降且时延不变”的证据。')
add('2. 将启动前保护与WAIT回收作为基础路径，下一轮把是否开启PEBS/迁移也纳入收益决策；当前只是离线消融开关，尚未实现自动准入。单独校准剩余生命周期、有效访问与实际复制代价，加入工具初次触页/带宽成本，不能把固定80ns与0.25视为已验证敏感度。')
add('3. 对order5若观测到更好的实际归还与低时延，做交错order9/order5确认并记录reporting次数/CPU与真实Host驻留回执；按整VMM成本收紧账本后才发放跨VM信用，不能以payload下沉次数替代归还容量。')
add('   同时区分剩余活页与已free backing：当前启动区位于CXL，但CPU/工具区位于DDR，运行期未绑定的系统分配仍可能落DDR。若活页底座仍显著，应在新共同环境中单列CPU归属CXL启动区、工具绑定DDR的匹配拓扑确认；不能拿旧v11直接充当新版对照。')
add('4. 对已通过低时延筛选的配置单独做1x完成到下一开始间隔回放，检查WAIT reclaim/resume；工具内等待仍要单列。然后构建真实2VM共享账本、错峰与集中恢复实验，再考虑4/8VM与超售密度。')
add()
add('## 9. 可复现产物')
add()
add('新模块提供显式Guest fast/slow节点、可真正关闭PEBS的开关，以及future-benefit/heat选择；非search阶段在fork/exec前预授headroom。模块编译、Guest加载、实际物理节点映射与独立回放均核验。构建补丁已接入kernel.mk，旧报告及冻结产物未重写。'+link(R/'final-audit.json','最终审计及输入/代码/报告SHA记录')+'核验了145个独立VM与根镜像一致性。')
add()
for path,name in [(W/'phase/three_modes/run_study_matrix.py','冻结矩阵运行器'),(W/'phase/three_modes/run_validated_single.sh','独立VM运行器'),(W/'phase/three_modes/host_monitor.py','完整VMM观察器'),(W/'phase/three_modes/verify_topology.py','Guest/Host物理节点核验'),(W/'phase/three_modes/relocate_private.py','VMM非Guest-RAM驻留放置'),(W/'phase/three_modes/analyze.py','结果审计与折中分析'),(W/'phase/three_modes/mechanisms.py','机制计数分析'),(W.parents[1]/'sosp25-demeter/patch/demeter-sandbox-vmm-tradeoff.patch','可重建模块补丁'),(C/'analysis.json','完整正式分析')]:add('- '+link(path,name))
add()
add('```bash\ncd /r0/home/geerties/HW/Demeter/experiments/agentcgroup-demeter\npython3 phase/three_modes/analyze.py --root results/three-modes-20261010/confirmation-v13\npython3 phase/three_modes/mechanisms.py --root results/three-modes-20261010/confirmation-v13\n.venv-capacity-analysis/bin/python phase/three_modes/plot.py --root results/three-modes-20261010/confirmation-v13\n```')
args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text('\n'.join(lines)+'\n')
print(args.output)
