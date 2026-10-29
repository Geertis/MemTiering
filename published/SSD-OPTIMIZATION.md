# SSD 低收益原因与针对性优化

2026-10-26。依据原批最终审计的 38 条轨迹，另完成 3 条低收益轨迹、5 个配置、3 次重复，共 45 次新的独立 VM 测量及独立审计。

承接[组会思路与实现](261020组会-my.md)、[最新实验归纳](261020阶段历史与回收反馈优化.md)。原批与本轮分别计量，原文件及其历史结果保留。

**判断：当前差距主要由控制范围、固定内核底座和实际容量点共同造成，不能主要归因于 SSD 的性能保护过严，也不能解释为这些应用的数据必须长期驻留 DRAM。** 旧 V40 确有成本升高后增容／止损的路径；当前 V54 上层已经固定阶段配额，5%／10%／20% 是离线筛选条件，在线没有一个仍可简单放宽的“5%保护”。

## 1. 现有数据说明了什么

1. **配额执行并非普遍失败。** 38 条中有 34 条采到 ACTIVE，均达到 quota＋8 MiB 的执行口径；另外 4 条 ACTIVE 太短，没有采到该状态。WAIT 实现率最差为 99.776%。这里包含明确的执行容差，不能把实现率误写成逐时刻硬上限。主批 38 条的 DAMOS Sandbox 文件／匿名页回收下界均为 0；大多数沙箱额度由 `memory.high` 的原生回收实现，不能把当前容量收益归为 DAMON 逐页选择的独立效果。

2. **继续压受控用户页，通常没有足够的量来弥补 20 pp 差距。** 原批 SSD 剩余受控用户驻留均值的跨轨迹中位数约为 16.50 MiB，整 VMM DRAM 均值约为 715.27 MiB。固定时间权重，并乐观地把 Guest 用户驻留 1:1 当作可归还 Host DRAM，即使把剩余用户页全部消除，额外均值节省的中位量级也只有 **2.05 pp**。这是量级估算，不是严格的物理下界，也不是可执行的“零内存工作集”。

3. **有些低收益轨迹根本缺少可回收用户页。** Pydicom Haiku 的原批基线用户驻留约 0.08 MiB，实际工具服务总量约 13 ms；候选仍多付出约 12 MiB 的 Guest Python 控制器等匿名驻留。此类短轨迹不能代表完整 Pydicom 测试，也没有大量应用冷页可供 SSD 分层获利。另一方面，Redis Haiku 的服务占比达 93.4%，ACTIVE6144 基本不限制其约 170 MiB 的驻留，WAIT 优势自然很小。两类情况需要分别解释。

4. **CXL 总收益包含固定布局收益。** 原 38 条的 CXL 总均值节省中位数为 26.51%，同布局静态控制已有 25.18%；阶段动态相对全 DRAM 的增量中位数为 1.68 pp。SSD 的 6.73% 与 CXL 的 26.51% 不构成纯粹的相同容量执行器对照。V48 同一次 WAIT256→16 动作分别减少 CXL／SSD 用户页均值 59.80／57.70 MiB，释放量已经接近。

上层容量相同，表示给出相同的受控驻留目标；它不要求需求较小的轨迹填满配额，也不自动限制 Guest 内核、系统服务和 VMM。SSD 页面再次访问时需要回到 DRAM，CXL 页面可继续在慢层被 CPU 访问；相同目标也不保证相同实际驻留曲线或相同阶段时长。只有控制范围、布局、实际驻留和时间权重都一致，整 VM 节省才应接近。

## 2. 已实现的两项优化与一个容量探测

**减少固定底座。** 旧配置向 Guest 呈现 8192＋8192 MiB 原始内存，再各 balloon 2048 MiB，名义可用容量为 6144＋6144 MiB。新配置直接呈现 6144＋6144 MiB 原始内存，balloon 初值为 0；名义容量及通常的 ACTIVE6144／WAIT16 配额均保持相同。另以 `sysctl.vm.mem_profiling=never` 关闭本实验没有使用的通用分配剖析，保留 Demeter 的分配跟踪能力。

本机内核的 `struct page` 大小为 64 B。按原始 16 GiB 而非实际名义 12 GiB 建立这类元数据，会多付出约 64 MiB。新配置的启动保留量从约 375.11 降到 311.11 MiB，`page_ext` 从 64 降到 24 MiB；这些是不同计量层次的证据，不把所有日志值相加当作精确 Host 分类。真实收益以新测 `compact-dram` 静态控制的 Host RSS 为准。[内核分配剖析文档](https://www.kernel.org/doc/html/latest/mm/allocation-profiling.html)，[本机 6.10 实现](/r0/home/geerties/HW/Demeter/sosp25-demeter/kernel/demeter/lib/alloc_tag.c:183)。

本机 balloon 驱动通过分配并保留页面、向 VMM 归还数据背书来回收容量，不会撤销 Guest 为这些 PFN 建立的页元数据。因此“Host 已收回 4 GiB 数据页”不等于“Guest 不再承担这 4 GiB 的管理成本”。在当前 Guest swap 路径下，Guest 内核页元数据不能像普通匿名页一样换到 SSD；CXL 仍可直接承载并访问此类内存。这是相同 payload 配额无法保证整 VM DRAM 节省相同的重要原因，不能进一步推导成所有 Host 侧 SSD 换出实现都有相同底座下限。[冻结驱动实现](/r0/home/geerties/HW/Demeter/experiments/agentcgroup-demeter/phase/artifacts/vmm-v54-ssd-placement-readguard/balloon.c:256)。

**让短 WAIT 有机会归还额外缓存。** 对 Sandbox 以外 Guest 文件缓存，将进入回收的等待门槛及 DAMON 冷龄分别由 1000 ms 降至 200 ms。仍保留逐 folio young 检查、文件与匿名范围区分、恢复页保护、匿名换入冷却、扫描预算及工具 start 的停止屏障。这一路径覆盖 Sandbox 之外的 Guest 缓存，收益须按该范围解释；上层容量不变。

**实际探测更低容量。** 除 6144／16 外，对三个轨迹统一再测 ACTIVE64／WAIT4；两个介质可以使用同一个 `PhaseQuota`，没有按照 SSD 成本自动增容。更紧点允许出现真实超额或性能失败，并完整保留结果。5%／10%／20% 只在运行完成后判断。

旧布局的五次 Streamlink 先导中，仅加快额外缓存回收使均值节省从 4.19% 变为 5.08%，E2E 增长由 0.56% 变为 0.43%；ACTIVE128 未增加收益，其受控峰值本来就小于该目标。该组件对照每点只有一次，不替代下文三重复确认。

## 3. 三重复确认：总收益与阶段增量分开

每个重复块新测旧布局纯 DRAM，再测四个候选，轮转候选顺序。每个点均启动全新 VM／磁盘副本，保留 1×原始调用间等待与真实工具执行。按同块先求比值，再取三次中位数；“最差 E2E”保留最大值。所有工作量与此前最终有效基线及本块新基线核验，三次新基线签名一致。

延续原报告计量：物理 DRAM 为整 VMM 的 Host N0＋N1 RSS，约 250 ms 采样并按时间加权；峰值为采样峰值，积分覆盖各自实际 E2E。它不包含完整 Host 文件缓存与 KVM 账本。

下面“新 SSD”统一采用 6144／16。各百分比均相对同块旧布局全 DRAM；这张表的提升同时包含静态底座与阶段回收，不能全部称为阶段算法收益。

|轨迹|原 SSD 均值节省|新 SSD 均值节省|新 SSD 峰值节省|新 SSD 积分节省|新 SSD E2E 中位／最差|
|---|---:|---:|---:|---:|---:|
|Streamlink / Haiku|4.46%|21.38%|12.70%|20.83%|+0.694%／+0.720%|
|github3.py / Haiku|1.43%|20.88%|18.76%|19.68%|+0.865%／+1.510%|
|Pydicom / Haiku（轻量）|0.38%|14.68%|14.68%|14.46%|+0.117%／+0.259%|

原策略的 Pydicom / Haiku（轻量） 有一次均值节省 -1.65%，所以表中 0.38% 仅为中位数，未确认三次均为正收益。新组合的三个重复均为正，并满足 5% E2E 限制。

同一批数据进一步分解如下。阶段增量 pp 使用旧布局 DRAM 为分母；相对新布局的节省使用 `compact-dram` 为分母。各项独立取中位数，不能用两个中位数简单相减替代配对增量。

|轨迹|仅静态底座节省|阶段额外节省（旧分母 pp）|阶段相对新布局纯 DRAM 的均值节省|阶段相对新布局 E2E 增长|
|---|---:|---:|---:|---:|
|Streamlink / Haiku|13.37%|+9.23|+10.51%|+0.678%|
|github3.py / Haiku|12.59%|+8.54|+9.77%|+0.749%|
|Pydicom / Haiku（轻量）|15.79%|-1.62|-1.93%|+0.097%|

Pydicom 的这条轻量轨迹主要用于检查固定底座和控制开销，不能外推完整影像处理或测试的工作集。即使总收益为正，仍必须检查上述阶段增量是否为负。

![三条轨迹的物理均值节省与时延](assets/261026SSD低收益诊断与优化/confirmation.png)

图中误差线是三次实测范围，不是置信区间。[矢量 PDF](assets/261026SSD低收益诊断与优化/confirmation.pdf)。

## 4. 放宽到 10%／20% 是否有额外收益

|轨迹|64／4 均值节省|64／4 峰值节省|64／4 积分节省|64／4 E2E 中位／最差|工具服务增长|
|---|---:|---:|---:|---:|---:|
|Streamlink / Haiku|19.63%|14.15%|19.03%|+0.753%／+0.824%|+26.88%|
|github3.py / Haiku|25.25%|28.63%|20.39%|+6.134%／+11.771%|+15.85%|
|Pydicom / Haiku（轻量）|14.57%|14.43%|14.46%|+0.227%／+0.318%|+34.12%|

Streamlink 的前两块对照进一步说明了控制范围问题：64／4 相比 6144／16，受控用户驻留分别从约 12.65／14.60 MiB 降至 8.01／8.09 MiB，但 Guest Cached 从 49.00／38.71 MiB 升至 70.68／80.16 MiB，整 VMM DRAM 均值约多出 8～34 MiB；额外 Guest 文件缓存的 DAMOS 回收也更少。可以确认更低的受控驻留被其他占用抵消，不能仅凭目标更紧认定整 VM 回收更充分。这里并未单独证明每一种缓存变化的因果路径。[原始范围对照](assets/261026SSD低收益诊断与优化/streamlink-scope-comparison.json)。

下表只在本次两个容量点中选择：三次均值节省都为正、三次 E2E 都满足限制，再取均值节省中位数最大的点。它是有限已测点的离线选择，不是整个容量空间的边界。

|轨迹|≤5%|≤10%|≤20%|
|---|---:|---:|---:|
|Streamlink / Haiku|21.38%（6144／16）|21.38%（6144／16）|21.38%（6144／16）|
|github3.py / Haiku|20.88%（6144／16）|20.88%（6144／16）|25.25%（64／4）|
|Pydicom / Haiku（轻量）|14.68%（6144／16）|14.68%（6144／16）|14.68%（6144／16）|

E2E 低并不证明工具没有变慢，原始 WAIT 会稀释服务代价。放宽限制应通过实际更低驻留来利用，而不是额外制造 I/O 消耗时间预算。更紧点若未增加均值／积分节省，或超过允许增长，就保留为失败／退化点，不把其时延上涨称为优化。

## 5. 验证、实现与适用范围

45 次正式测量全部完成物理与语义核验；独立审计核对实际 VMM 原始／名义容量、固定 balloon、NVMe 直接 I/O swap、Host RSS、归还凭据、Guest 错误、每阶段配额、归档输入 SHA256、Host 无 swap，以及实际 Guest RAM 排除范围。没有用性能差的样本重跑替换。

正式矩阵共 46 次尝试，含 1 次 E2E 开始前的观测初始化失败；45 次有效测量单独统计。github3.py 第三块首次宽配额候选的 Host 观测流仅产生 init／end，没有 Guest 样本和回放记录，启动器退出 97；保留该次记录，用相同冻结输入及同块 DRAM 基线按新编号恢复。恢复选择由 `slot-selection.json` 记录，独立审计核对未替换任何已经完成 E2E 的测量。

新控制器与页面执行器另通过 19 项检查，覆盖恢复页保护、匿名回收冷却、采样历史范围、物理页身份、ACTIVE 停止屏障及新 WAIT 门槛。[检查日志](assets/261026SSD低收益诊断与优化/tests.log)。

此前另保留：一次旧 Numba golden 已退役导致的启动前拒绝；V65 五次有效先导；V66 初次先导的一个有效 DRAM 与一个 E2E 前验证脚本失败；修正后四次有效先导。旧 helper 将 Guest RAM 映射大小写死为 16 GiB，新 helper 对照实际 `vm.info` 校验 12／16 GiB，不改变原始证据。

正式实验使用两个互不重叠的 CPU lane，各自设置 24 GiB Host 计费上限并禁用 Host swap；仍共享物理 SSD，Host 压力与外部 VM 记录保留。现有补充队列和早期冻结策略均未修改。这是三个已知低收益形态的针对性验证，不代表原 38 条全量复测。

新的原始布局在本实验期间保持原有名义可用容量，但最大可呈现地址空间由 16 GiB 变为 12 GiB；需要未来扩到 16 GiB 的部署，应保留对应布局或通过内存热插拔增加容量。没有把 quota 说成整 VM 硬限制，也没有将这些单 VM 节省直接换算成可售实例数。新布局尚未重新测量 CXL，不能据本轮结果声称两介质已达到同一容量边界。

实现：[/phase/ssd_scope_v66/README.md](/r0/home/geerties/HW/Demeter/experiments/agentcgroup-demeter/phase/ssd_scope_v66/README.md)、[阶段控制器](/r0/home/geerties/HW/Demeter/experiments/agentcgroup-demeter/phase/ssd_scope_v66/adaptive_controller.py)、[SSD 页面执行](/r0/home/geerties/HW/Demeter/experiments/agentcgroup-demeter/phase/ssd_scope_v66/placement.py)、[验证启动器](/r0/home/geerties/HW/Demeter/experiments/agentcgroup-demeter/phase/ssd_scope_v66/study.py)。

证据：[原批 38 条诊断 CSV](assets/261026SSD低收益诊断与优化/diagnosis-38.csv)、[诊断与分类](assets/261026SSD低收益诊断与优化/diagnosis.json)、[新批配对明细](assets/261026SSD低收益诊断与优化/confirmation.json)、[新批汇总 CSV](assets/261026SSD低收益诊断与优化/confirmation.csv)、[独立审计](assets/261026SSD低收益诊断与优化/independent-audit.json)、[先导完整结果](assets/261026SSD低收益诊断与优化/pilots.json)。

[主要源码差异](assets/261026SSD低收益诊断与优化/changes.diff)、[来源及输出 SHA256](assets/261026SSD低收益诊断与优化/provenance.json)。

复算报告、图表和审计：

```bash
cd /r0/home/geerties/HW/Demeter/experiments/agentcgroup-demeter
A=/r0/home/geerties/HW/report/assets/261026SSD低收益诊断与优化
python3 "$A/diagnose.py"
python3 "$A/summarize.py" --roots results/ssd-scope-v66-20261026/confirm-streamlink-pydicom results/ssd-scope-v66-20261026/confirm-github --output confirmation
python3 "$A/audit.py" --roots results/ssd-scope-v66-20261026/confirm-streamlink-pydicom results/ssd-scope-v66-20261026/confirm-github
.venv-capacity-analysis/bin/python "$A/plot.py"
python3 "$A/render.py"
```
