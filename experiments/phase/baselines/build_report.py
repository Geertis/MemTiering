#!/usr/bin/env python3
"""Build the report only from completed frozen cohorts and audited observations."""
import json
import statistics
from datetime import datetime,timezone,timedelta
from pathlib import Path

W=Path(__file__).resolve().parents[2];R=W/'results/baselines-20261009'
screen=json.loads((R/'screen/analysis.json').read_text())
realtime=json.loads((R/'realtime/analysis.json').read_text())
mixed=json.loads((R/'mixed/analysis.json').read_text())
whole=json.loads((R/'whole-ram-v3/analysis.json').read_text())
audit=json.loads((R/'final-audit.json').read_text())
quality=json.loads((R/'tool-quality-audit.json').read_text())
assert screen['finished']==84 and realtime['finished']==24 and mixed['finished_pairs']==8
assert whole['finished']==28
assert audit['pass']
date=datetime.now(timezone(timedelta(hours=8))).strftime('%y%m%d')
path=Path('/r0/home/geerties/HW/report')/f'{date}完整VMM成本与静态CXL基线补充实验.md'
f=lambda v:f'{v:.2f}'
link=lambda p,label:f'[{label}]({p})'
lines=[f'# {date} 完整 VMM 成本与静态 CXL 基线补充实验','',
    '本轮针对前一轮的两个评价缺口补充实验：以整个 VMM 的 Host NUMA RSS 评价 DDR 驻留；以启动前绑定的 DRAM/CXL 和静态分层作为基线，检验阶段策略的额外 DDR 与控制成本是否值得。旧报告的 reference 已含分层机制，不能作为全 DRAM 基线。本轮单独冻结协议，不与旧校准或 reference/full 混算。','',
    f'完成 4 台预检、84 台七轨迹三重复机制筛查、24 台原始时间尺度回放、8 对真实双 VMM（16 台）及 28 台整个 Guest RAM 映射对照，共 156 台 Guest。screen 基础核验 {screen["basic_valid"]}/84；1x 基础核验 {realtime["basic_valid"]}/24；双 VMM 相对核验 {mixed["valid_pairs"]}/8；整体 RAM 映射基础核验 {whole["basic_valid"]}/28。配置间回放一致不等于原 SWE 任务成功。','',
    '**主要结论：此前明显的 payload DDR 节约不能直接解释为整机同幅度节约；目前阶段策略也未在所有负载上超过简单静态分层。** Dask 在真正 DRAM 基线下的执行代价较大，必须继续处理；Pydicom 的 DDR 峰值取舍较好，但工具合计和长调用仍有显著代价。github3 还暴露出工具内部网络重试掩盖性能变化的问题。这轮结果支持收紧评价与选择策略，尚不足以宣称达到超售目标或时延验收。','',
    '用户于 10 月 10 日补充的优化目标是：**完整 DRAM 占用大幅下降，且整体时延没有明显改变。** 本文据此判断，20% 合计涨幅不视为理想结果，长 WAIT 下端到端看似不变也不替代工具服务和长调用的稳定性。','',
    '## 1. 实验设置与每组目的','',
    '|集合|配置|目的|并行设置|','|---|---|---|---|',
    '|pilot|Haiku × 四策略 × 1，10x，4 VM|验证启动前策略继承、匿名页实际落点和观测协议|每次一台|',
    '|screen|七轨迹 × 四策略 × 3，10x，84 VM|识别跨负载的完整 VMM DDR—工具执行取舍|每次一台|',
    '|realtime|Dask/Haiku × 四策略 × 3，1x，24 VM|确认长等待与再访问是否改变筛查结论|每次一台|',
    '|mixed|Dask+Haiku × 四策略 × 同时/偏移开始，8 对|测真实独立 VMM 的并行驻留与执行代价|每次两台，批次串行|','',
    '另加 whole-ram-v3：七轨迹 × 整个 Guest RAM 映射 DDR/CXL × 两个独立 VM，共 28 台，10x；串行在双 VMM 集合完成后运行。该组采用同一 Guest 双 NUMA 拓扑、镜像、容量、工具绑定及 reporting 设置，只改变 RAM 两个区域的 Host 映射。Guest RAM 全 CXL 并不意味着 Host 上的 VMM 程序私有内存及 KVM 元数据都使用 CXL。实际 vm.info 的两个 host_numa_node 均须通过计时前核验。','',
    'whole-ram-v3 两侧工具均绑定 Guest node0；此组 dram/cxl 是物理 Host 映射标签，不沿用 screen/realtime 的 Guest0/1 工具绑定区别。whole-ram 和 whole-ram-v2 均为未执行的早期规划，未计入结果或失败数。','',
    'Host 为 `6.10.0-demeterhost-loadfb-20261005`，Guest 为 `6.10.0-demeter`。screen/realtime/mixed 中，Guest DDR node 0 映射 Host node 0，Guest CXL node 1 映射 Host node 2。共同 raw DDR/CXL=8/8 GiB，可用=6/6 GiB，单 VM 4 vCPU；单 VM CPU 0–3，双 VM 第二台 CPU 4–7。提高单层可用容量是为了使完整数据 Pydicom 在任一绑定层均能运行，避免把层容量不足混入介质敏感度。这与旧 1280/4096 MiB 拓扑不同，不能跨轮归因其成本变化。','',
    'CPU 0–7 位于 Host node0；该节点到 DDR0/CXL2 的 NUMA distance 分别为 10/24，而 node1 到 CXL2 为 14。distance 是拓扑提示，不是实测延迟倍数。本轮比较的是固定 CPU0 与 DDR0/CXL2 放置的端到端效果，包含该拓扑路径，不能泛化为所有 CPU 插槽下的 CXL 固有代价。','',
    '所有配置采用相同 golden 镜像、原始命令、测试数据和 free-page reporting on/order 9；关闭交换、THP、自动 NUMA balancing 和内核 demotion。每个重复均用新 VM、独立 workload 副本和 root overlay。','',
    '本轮 capacity policy 固定为 static，balloon 的实际容量须全程保持共同配置。phase 动态调整的是 Sandbox 内 DRAM quota 和页面落点；没有启用随阶段伸缩的 Guest 总容量。因此 DDR 降低主要反映放置与 reporting 回收后的真实驻留，而非本轮已经验证了整套 balloon 扩缩容收益。','',
    '|策略|实际实施|','|---|---|',
    '|dram|在 Python 启动前 MPOL_BIND node0；没有 placement/PEBS/阶段控制器|',
    '|cxl|在 Python 启动前 MPOL_BIND node1；没有 placement/PEBS/阶段控制器|',
    '|static|启动前 MPOL_WEIGHTED_INTERLEAVE，DDR:CXL=1:4；无在线迁移|',
    '|phase|既有 v6b full 放置和控制器，四项优化开启；Dask/Pydicom/Numba/Xonsh q256，Streamlink/github3 q64|','',
    '**静态 1:4 是固定比例基线，尚不是相同实测 DDR 峰值的预算匹配基线。** 可以检查一种配置是否同时占用更少 DDR 且耗时更低；若二者各有所长，则须进一步做相同 DDR 成本的份额扫描，不能仅按同一 Guest 容量宣称预算公平。BIND 控制新分配，并未强制迁移 Guest OS 或既有共享缓存，所以 screen/realtime/mixed 的 cxl 仅表示工具新页绑定；whole-ram-v3 才将 Guest 系统 RAM 也映射到 CXL。实际匿名页及整个 VMM 各层驻留均保留。','',
    '10x 仅压缩原始工具开始偏移，命令执行时间不缩短。工具按记录顺序串行；Bash 内部并发保留；不重现所有原记录的工具重叠。1x 也不包含实时 LLM。双 VMM 同时开始是回放开始对齐，并非每个工具 ACTIVE 对齐；偏移组让 Haiku 在 Dask 开始后 180 s 启动，两个 VM 均提前准备就绪。','',
    '**1x 使用原始绝对开始偏移，不是保留原始“前一工具完成后再等待”的间隔。** 当本地命令比原命令快时，下一开始偏移不变，回放中的 WAIT 会变长；当命令较慢时，串行队列会赶不上偏移。因此 1x 是固定到达计划的确认实验，不能声称完全重现真实 Agent 的等待比例、端到端时延或错峰容量。后续应增加按原始完成到开始间隔回放的独立确认组。','',
    '各次运行的精确轨迹/配置次序、输入及源码 SHA：'+', '.join(link(R/n/'plan.json',n+' plan') for n in ['screen','realtime','mixed','whole-ram-v3'])+'。','',
    '## 2. 观测口径','',
    'Guest cgroup anon+file 仍用于解释放置，但主结果采用整个 VMM 在 Host DDR node0/1 上的 RSS。启动 observer 每 100 ms 保存 NUMA RSS、smaps_rollup 的 PSS/私有/共享数据、Host 节点内存及 PSI；另用约 250 ms 的 Guest 时钟流对齐回放窗口，分别统计窗口内峰值、积分均值和启动加执行峰值。采样峰值不保证捕获短于采样间隔的真实峰值。','',
    'RSS 包含 Guest backing 和 VMM 进程开销，但共享映射可能重复计数，部分 KVM/Host 内核成本及进程外 Host 文件缓存未完整计入。Host 节点内存变化另有记录，不能将其他进程或准备镜像产生的变化直接归因于 VMM。**本轮已经扩大至整个 VMM 的成本口径，仍不是所有系统物理成本的精确归属。**','',
    '同时报告工具纯执行时间和含阶段通知等控制步骤的服务时间；正文性能比采用后者，避免隐藏策略自己的控制开销。每 VM 的最大/p95/经验 p99 另存，但三次独立 VM 和少量工具不能估计生产业务 p99。SOW 的冷热分层阶段 p99 要求为 10%，最终集成阶段为 20%；表中工具合计比不能作为任一验收结果。','',
    '## 3. 七轨迹三重复结果，10x','',
    '下表为三个独立 VM 的中位数；比值为配置服务时间中位数/该轨迹 dram 服务时间中位数。所有原始独立重复及同轮配对范围另存，不把工具调用当作独立样本。','',
    '|轨迹|策略|有效相对对照|工具执行/服务 s|服务比 DRAM|每 VM 最长执行比 DRAM|VMM DDR 峰值/均值 MiB|VMM CXL 峰值 MiB|','|---|---|---|---:|---:|---:|---:|---:|']
for data in [screen,realtime]:
    if data is realtime:
        lines+=['','## 4. 原始时间尺度确认，1x','',
            '|轨迹|策略|有效相对对照|工具执行/服务 s|服务比 DRAM|每 VM 最长执行比 DRAM|VMM DDR 峰值/均值 MiB|VMM CXL 峰值 MiB|','|---|---|---|---:|---:|---:|---:|---:|']
    for s in data['summaries']:
        m=s['medians'];ratio=s['ratios_to_dram']['tool_service_sum_s']
        lines.append(f'|{s["trace"]}|{s["policy"]}|{s["valid"]}|{f(m["tool_execution_sum_s"])}/{f(m["tool_service_sum_s"])}|{ratio:.3f}|{s['ratios_to_dram']['tool_execution_max_s']:.3f}|{f(m["host_ddr_replay_peak_mib"])}/{f(m["host_ddr_replay_mean_mib"])}|{f(m["host_cxl_replay_peak_mib"])}|')
    lines+=['',link(R/('screen' if data is screen else 'realtime')/'analysis.json','完整分析 JSON')+'；'+link(R/('screen' if data is screen else 'realtime')/'trials.csv','独立 VM CSV')+'。',
        '',f'![完整 VMM 驻留与服务时间]({R/("screen" if data is screen else "realtime")/"whole-vmm-tradeoff.png"})','']
lines+=['## 5. 静态 CXL 是否已足够，阶段策略的额外 DDR 是否有用','',
    '先核对用户指出的统计口径：下表同样用 screen 三个 VM 的中位数，phase 相对本轮原生 dram 计算。payload 峰值与 VMM 峰值可能出现在不同时间，不能将二者直接相减后命名为 OS 固定成本。','',
    '|轨迹|payload DDR 峰值 DRAM→phase MiB|payload 峰值节约|VMM DDR 峰值 DRAM→phase MiB|VMM 峰值节约|',
    '|---|---:|---:|---:|---:|']
for trace in dict.fromkeys(s['trace'] for s in screen['summaries']):
    g={s['policy']:s for s in screen['summaries'] if s['trace']==trace}
    dm=g['dram']['medians'];pm=g['phase']['medians']
    lines.append(f'|{trace}|{f(dm["payload_ddr_peak_mib"])}→{f(pm["payload_ddr_peak_mib"])}|{100*(1-pm["payload_ddr_peak_mib"]/dm["payload_ddr_peak_mib"]):.1f}%|{f(dm["host_ddr_replay_peak_mib"])}→{f(pm["host_ddr_replay_peak_mib"])}|{100*(1-pm["host_ddr_replay_peak_mib"]/dm["host_ddr_replay_peak_mib"]):.1f}%|')
lines+=['',
    'Dask/Pydicom 的较大匿名工作集可以被分配到 CXL，完整 DDR 节约仍可见；Haiku、Streamlink、github3 这类小工作集则受 Guest 系统/缓存和 VMM 成本影响较大，payload 降幅不足以证明整机收益。共同 free-page reporting 将 Guest 已释放页归还 Host，是兑现驻留回收的共同机制；因为本轮所有组都开启 reporting，不能把它的贡献单独归于 phase。本轮也没有 allocation-only/placement-only 消融，尚不能给新页分配、阶段 quota 与后续迁移分摊百分比收益。','']
for data,label in [(screen,'10x'),(realtime,'1x')]:
    traces=list(dict.fromkeys(s['trace'] for s in data['summaries']))
    for trace in traces:
        g={s['policy']:s for s in data['summaries'] if s['trace']==trace}
        if not all(s['valid'] for s in g.values()):
            lines.append(f'- {label} {trace}：存在无效或不等价对照，不能用于策略优劣结论。');continue
        c=g['cxl'];phase=g['phase'];static=g['static'];dram=g['dram']
        cm=c['medians'];pm=phase['medians'];sm=static['medians'];dm=dram['medians']
        c_cost=100*(cm['tool_service_sum_s']/dm['tool_service_sum_s']-1)
        p_cost=100*(pm['tool_service_sum_s']/dm['tool_service_sum_s']-1)
        benefit=100*(1-pm['tool_service_sum_s']/cm['tool_service_sum_s'])
        extra=pm['host_ddr_replay_peak_mib']-cm['host_ddr_replay_peak_mib']
        dominated=sm['host_ddr_replay_peak_mib']<=pm['host_ddr_replay_peak_mib'] and sm['tool_service_sum_s']<=pm['tool_service_sum_s']
        lines.append(f'- {label} {trace}：cxl 服务增加 {c_cost:.1f}%，phase 增加 {p_cost:.1f}%；phase 比 cxl 多占用 DDR 峰值 {extra:.1f} MiB，服务时间减少 {benefit:.1f}%。'+
            ('static 在中位数上同时使用更少 DDR 和更少服务时间；阶段策略的优势未得到该对照支持。' if dominated else '两项成本需一起评估，不能只凭 payload 降幅选择阶段策略。'))
lines+=['',
    '这些是当前固定参数的观测，不证明所有 CXL 负载均可接受，也不证明阶段感知本身无价值。若简单 CXL/静态分层已满足业务质量要求，并在完整成本上优于阶段策略，应采用简单方案；动态机制须在真正敏感的工具或并行预算竞争中证明额外价值。','',
    '阶段策略的动作与控制成本另列如下，取三个 VM 中位数。allocation 计数为累计分配交接次数，不是唯一驻留页数；迁移/交换计数也不能直接转为净容量信用。控制步骤耗时为工具服务合计减去纯执行合计。','',
    '|集合/轨迹|fast/slow 分配计数|原生回退|promotions/demotions/exchanges|file demotions|worker CPU s|工具控制步骤 s|',
    '|---|---:|---:|---:|---:|---:|---:|']
for data,label in [(screen,'10x'),(realtime,'1x')]:
    for trace in dict.fromkeys(r['trace'] for r in data['trials']):
        g=[r for r in data['trials'] if r['trace']==trace and r['policy']=='phase' and r['complete']]
        val=lambda k:statistics.median(r['stats'].get(k,0) for r in g)
        overhead=statistics.median(r['tool_service_sum_s']-r['tool_execution_sum_s'] for r in g)
        lines.append(f'|{label}/{trace}|{val("allocation_fast"):.0f}/{val("allocation_slow"):.0f}|{val("allocation_native_fallback"):.0f}|{val("promotions"):.0f}/{val("demotions"):.0f}/{val("exchanges"):.0f}|{val("file_demotions"):.0f}|{val("worker_cpu_ns")/1e9:.3f}|{overhead:.3f}|')
lines+=['',
    '执行了候选决策和迁移不等于得到净收益。新页分配决定初始层级，后续迁移试图修正放置；与无迁移静态策略的对照才判断这个修正是否值得。worker CPU 也未包含所有 PEBS IRQ、分配 hook 和内核迁移成本，不能据此解释全部性能变化。','',
    '## 5.1 整个 Guest RAM 使用 CXL 的更强基线','',
    '该组内比较全 Guest RAM DDR 与全 Guest RAM CXL，均关闭阶段控制和迁移；各两次独立 VM。下面的比值仅在该组内计算，不能把它与前面 payload BIND 的绝对时长直接相除。','',
    '|轨迹|整个 RAM 映射|有效相对对照|工具服务 s|服务比本组 DDR|每 VM 最长执行比 DDR|VMM DDR 峰值 MiB|VMM CXL 峰值 MiB|',
    '|---|---|---|---:|---:|---:|---:|---:|']
for s in whole['summaries']:
    m=s['medians'];ratio=s['ratios_to_dram']['tool_service_sum_s']
    lines.append(f'|{s["trace"]}|{s["policy"]}|{s["valid"]}|{f(m["tool_service_sum_s"])}|{ratio:.3f}|{s['ratios_to_dram']['tool_execution_max_s']:.3f}|{f(m["host_ddr_replay_peak_mib"])}|{f(m["host_cxl_replay_peak_mib"])}|')
lines+=['',link(R/'whole-ram-v3/analysis.json','整体 RAM 映射分析')+'；'+link(R/'whole-ram-v3/trials.csv','独立 VM CSV')+'；'+link(R/'whole-ram-v3/whole-vmm-tradeoff.pdf','可导出折中图 PDF')+'。','',
    f'![整个 Guest RAM 映射的驻留与服务时间]({R/"whole-ram-v3/whole-vmm-tradeoff.png"})','',
    '此对照直接检验把 Guest OS 也置于 CXL 的收益与代价。DDR 节省须同时检查 CXL 容量及并发性能；减少 DDR 并不减少同等数量的总物理驻留。两次 VM 和压缩等待仍不构成生产 p99/密度验收。','',
    '### 对“全用 CXL 也能取得较大收益”的最终判断','',
    '猜想存在的问题是把很大的 DDR 节约直接等同于可接受的性能取舍。全 Guest RAM 映射的 DDR 驻留收益确实存在；同组原生 DDR 对照则显示若干轨迹存在显著执行代价。下面只引用通过相对核验的本轮独立对照，结论限定在本机 CPU/NUMA 放置和当前回放路径。','']
for trace in ['dask-5510','pydicom-2065','xonsh-haiku']:
    g={s['policy']:s for s in whole['summaries'] if s['trace']==trace}
    if not all(s['valid'] for s in g.values()):
        lines.append(f'- {trace}：相对核验未通过，不用于最终性能结论。');continue
    dm=g['dram']['medians'];cm=g['cxl']['medians'];ratios=g['cxl']['ratios_to_dram']
    lines.append(f'- {trace}：VMM DDR 峰值下降 {100*(1-cm["host_ddr_replay_peak_mib"]/dm["host_ddr_replay_peak_mib"]):.1f}%，工具服务合计增加 {100*(ratios["tool_service_sum_s"]-1):.1f}%，每 VM 最长执行增加 {100*(ratios["tool_execution_max_s"]-1):.1f}%。')
lines+=['',
    'github3 的低全调用涨幅受内部 DNS 重试影响，Streamlink 的测试失败路径也限制其解释；“所有 CXL/DRAM 涨幅都小于 20%”不受本轮支持。“大幅节约 DDR 且整体时延没有明显改变”尚未实现，当前 phase 也不能凭单一 DDR 指标被选为最佳折中。下一步应在相同完整成本下寻找稳定接近原生 DRAM 的服务时间，并验证成功路径、长调用和恢复排队时延。','',
    '## 6. 真实双 VMM 并行','',
    '|开始安排|策略|有效相对核验|两 VM 同时 DDR 峰值/均值 MiB|两 VM CXL 峰值 MiB|两轨迹服务合计比 DRAM|Dask/Haiku 各自服务比 DRAM|','|---|---|---|---:|---:|---:|---:|']
for b in mixed['batches']:
    individual=b.get('per_trace_service_ratios',{})
    lines.append(f'|{b["schedule"]}|{b["policy"]}|{b["valid"]}|{f(b["joint_ddr_peak_mib"])}/{f(b["joint_ddr_mean_mib"])}|{f(b["joint_cxl_peak_mib"])}|{b.get("service_ratio_to_dram",0):.3f}|{individual.get("dask-5510",0):.3f}/{individual.get("xonsh-haiku",0):.3f}|')
lines+=['',
    '在两种启动安排中，phase 的联合 DDR 积分均值仅小幅下降，服务合计仍显著增加；static 则同时拥有更低的联合 DDR 峰值/均值和更少的服务合计。这组没有支持当前阶段策略优于该静态基线。偏移开始未必降低实际 ACTIVE 重叠，且尚未放行的 VM 已有系统驻留，不能预设其一定具有更好的错峰收益。','',
    '联合 observer 对同一 Host 时间上的两 VMM 求和，而非相加两个不同时间的单 VM 峰值。联合窗口从首次放行到最后一条轨迹结束；已启动但尚未放行的 VM 成本计入，退出后按实际进程消失计零。每配置/安排只有一对新 VM，结果属于探索性证据。','',
    '**没有施加 Host DDR 节点物理硬限额，也没有跨 VM 的共享动态 quota 调度；两个 Guest 控制器仍各用 q256。** 因而本组检验真实并行与实际驻留，不能据此报告最大可承载 N、有效任务吞吐的密度提升或超售验收。','',
    link(R/'mixed/analysis.json','并行分析')+'；'+link(R/'mixed/plan.json','并行协议')+'。','',
    '## 7. 核验、失败和适用边界','',
    '核验启动前继承的内存策略、placement 模块实际存在/缺席、阶段控制器开关、BIND 下匿名页落点、完整 source_index、cgroup OOM、Guest 缺页重试和异常、Host observer 覆盖及协议 SHA。配置间比较返回状态、共同原始 HEAD 的最终代码 diff、Read/Grep/Glob 输出和可见测试计数。原始 Dask/Pydicom 测试范围差异仍未解决，不报告 SWE 成功率。','',
    f'最终 {len(audit["images"])} 个输入镜像前后 SHA 一致；四个正式协议和共同产物未漂移；记录范围内 Host OOM 为 {len(audit["host_oom_lines"])}，残留实验 VMM 为 {len(audit["remaining_experiment_vms"])}。见 '+link(R/'final-audit.json','最终核验')+'。','',
    'Pydicom 的共同 CXL 可用容量提高到 6 GiB，因此这一对照不能替代旧 4 GiB 容量压力下的 fallback/债务门控验证。不同 raw 拓扑造成的系统成本变化也不能记作阶段策略收益。','',
    '### 7.1 工具内部等待与失败路径','',
    'github3 的 source_index 54/56 是 betamax 安装的 DNS 重试失败；54 返回 1，56 经 shell 管道返回 0，错误被状态码掩盖。在 screen 的 DRAM 三重复中，含 DNS 失败的两次调用占工具纯执行总时长约 91.8%。这不是精确的网络等待时间分解，但足以说明“去掉工具间 WAIT”仍可能掩盖性能变化。原始全调用指标保留，不删除负结果。','',
    '以下为事后诊断：仅排除这两个 DNS 失败调用，观察其余调用相对同组 DRAM 的变化。余下路径仍包含 import/测试失败，不能将其命名为成功计算任务或当作新的正式主指标。','',
    '|github3 10x 集合/策略|含 DNS 失败调用占比|其余纯执行 s|其余纯执行比同组 DRAM|其余服务比同组 DRAM|',
    '|---|---:|---:|---:|---:|']
for s in quality['summaries']:
    if s['cohort'] in ['screen','whole-ram-v3'] and s['trace']=='github3-673':
        lines.append(f'|{s["cohort"]}/{s["policy"]}|{100*s["dns_call_fraction_median"]:.1f}%|{s["remaining_execution_median_s"]:.3f}|{s["remaining_execution_ratio_to_dram"]:.3f}|{s["remaining_service_ratio_to_dram"]:.3f}|')
lines+=['',
    'Streamlink 的 source_index 26 在测试收集阶段因 inspect.getargspec 缺失失败；30 因 FileFinder.find_module 缺失失败。29/35 的两个 URL 测试则完成并通过。因此该轨迹的整体低涨幅只代表当前短路径的混合，不能证明完整 Streamlink 工作负载对 CXL 不敏感。依赖/解释器修复应使用新的镜像、冻结新协议和新的 DRAM 基线，不能修复一侧后与本轮另一侧混算。','',
    '单 VM 与整体 RAM 映射集合的源索引、返回状态、可见输出证据及诊断比例见 '+link(R/'tool-quality-audit.json','工具路径审计')+'。这项审计基于保留输出尾部，不能保证找到所有错误。','',
    '## 8. 下一步','',
    '先修复并冻结成功工具路径，分别报告有用执行、工具内部等待、工具间等待和控制开销。纯 CXL 只在成功路径与长调用也满足时延目标的负载上作为候选；当前 github3 的低全调用涨幅不能作为准入证据。','',
    '然后围绕“相同实测 DDR 成本下，谁的时延更接近原生 DRAM”建立折中曲线。为敏感工具扫描静态 DDR 份额和阶段 quota，用完整 VMM 峰值与积分均值记录真实成本；展示所有配置的 Pareto 前沿，保留少量 DRAM 是否有效须由曲线确认，不能先假设动态方案最优。目标是时延变化处于独立重复的正常波动附近，而不是把允许 10%/20% 的验收上限用尽。具体“无明显变化”的业务容差和所需重复数应在确认实验前确定。','',
    '优化上优先检查短寿命进程的新页初始落点：对历史工具阶段中敏感、预期会复用的页提前保留 fast headroom，对低收益页及 WAIT 期间可恢复的数据使用 CXL；收益估计还须包含初次触页/清零代价，不能仅按后续复用次数判断短寿命页不值得 DRAM。并控制 PEBS、迁移和通知开销。采用 allocation-only、placement-only、完整策略等消融，判断阶段策略能否在相同成本下超过最好的静态份额。若不能，应保留静态方案及 DRAM 保护路径，不能靠扩大 quota 或隐藏工具代价宣称成功。','',
    '对于小 payload，进一步区分 Guest 系统页/缓存与工具热页。可增加 Guest 系统内存倾向 CXL、敏感工具新页保留 DRAM 的独立混合基线，检验能否降低目前的系统 DDR 驻留下限并保住工具性能。该方案尚未在本轮测量；全 Guest RAM CXL 与 payload BIND 的跨集合差异不能替代这个消融，也不能直接推断其时延收益。','',
    '随后连接跨 VMM 的真实资源账本与工具启动前屏障，兑现 Host DDR/CXL 预算并计入 OS、私有及内核成本；在可执行的固定物理池下扩展 2/4/8 个真实 VMM，验证错峰、随机偏移和集中恢复。以业务成功率、服务及恢复排队时延、有效任务吞吐和 OOM 确定可承载范围，不能用本轮单 VM 曲线或单对并行推算 1k/72 h 验收。','',
    '复现入口与实现说明：'+link(W/'phase/baselines/README.md','baselines README')+'；源码归档：'+link(R/'final-source-inputs','final-source-inputs')+'。','']
path.write_text('\n'.join(lines))
(R/'report-path.txt').write_text(str(path)+'\n')
print(path)
