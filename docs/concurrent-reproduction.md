固定 N=8，每个 Sandbox 配置 4 vCPU / 12 GiB 名义内存并回放一条轨迹，整批共享 L1 的 36 vCPU、逐档压缩的 12→10→8→7→6 GiB DRAM 与 64 GiB CXL 或 SSD 冷层，在相同到达序列下配对比较 PhaseTieing 与均分配额，并以 L1 OOM 判断容量边界。

# 当前实验设置与复现方法

本文件更新至 2026-10-31 的真实冷层容量探索。结果按 v4–v13 分开发布：
12 GiB 已完成三重复；更低 DRAM 档位为单次探索。它们不是同一冻结版本的一条完整容量曲线。
旧 96 GiB 全 DRAM 测量仅用于回放正确性、执行时间和离线占用参考。

## 1. 层级与资源

```text
L0 裸金属：真实 DRAM / CXL / NVMe
└─ L1 私有受限宿主 VM：36 vCPU，共享 D GiB DRAM + 冷层
   ├─ Runtime / 全局配额池 / 观测 / VMM 辅助开销 / 文件缓存
   └─ L2 Sandbox 0..7：各 4 vCPU / 12 GiB 名义内存 / 独立工作盘 / 一条轨迹
```

| 项目 | 当前配置 |
|---|---|
| L0 | 双路 Intel Xeon 6530P，64 物理核 / 128 逻辑 CPU；DRAM NUMA 0/1 各约 128 GiB；真实 CXL NUMA 2 约 256 GiB；NVMe |
| L0 软件 | Linux 6.10 定制内核，QEMU 8.2.2，Python 3.12.3；完整机器读数见 [environment.json](../experiments/concurrent/environment.json) |
| L1 CPU | 36 vCPU：0–31 用于 L2 执行，32–35 用于控制/观测；36 是 CPU 数，不是 Sandbox 数 |
| L0 CPU 映射 | L1 0–31→L0 32–63，L1 32–35→L0 24–27；外层 QEMU 辅助线程→L0 28/29 |
| L1 DRAM | D=12、10、8、7、6 GiB；只绑定 L0 DRAM node 1，预分配且锁页；含宿主系统和 VMM 开销，不能全部当成 workload quota |
| CXL 组 | L1 额外映射真实 node 2 的 64 GiB，共享给全体 L2；L1 看到 fast node 0 / cold node 1 |
| SSD 组 | L1 没有 CXL RAM；NVMe 上共 64 GiB 的 L2 swap，N=8 时每实例 8 GiB；承载文件系统盘为 72 GiB |
| L1 swap | 两组均关闭，仅在实验新建的私有 L1 中执行 `swapoff` |
| L2 | 4 vCPU；两个各 6 GiB 的名义内存区；内核、系统与 CPU 靠 fast 区；启用 free page reporting |
| L2 CPU 隔离 | lane i 使用 L1 CPU `[4i,4i+3]`，8 个池互不重叠；此布局只支持 N≤8，不能直接把 N 改为 12/16 |
| L2 的 12 GiB 含义 | CXL 组为名义 6 GiB DRAM + 6 GiB CXL；SSD 组两个区都由 L1 DRAM 支撑，8 GiB swap 另计；12 GiB 都不是物理 DRAM 保证 |

8 个 L2 的 96 GiB 名义地址空间由有限的共享物理层承载，通过 balloon/reporting、放置和冷层回收减少实际 DRAM 占用。
CXL/SSD 冷层容量是**全组共享 64 GiB**，不是每 Sandbox 各 64 GiB。CXL 是物理内存，SSD 是存储，两者不能当作等价 RAM 容量。
同一介质的 P/F 配对保持 CPU、DRAM、冷层容量、工作盘和到达序列一致；未声明物理机与其他用户全局隔离。

## 2. 工作负载与回放

采用 AgentCgroup 公开输入的固定提交
[`551ca1689b7d2d30db6b8a8613414750103cc68b`](https://github.com/eunomia-bpf/agentcgroup/tree/551ca1689b7d2d30db6b8a8613414750103cc68b)。

| 轨迹 | 每轨迹工具调用数 | N=8 时副本数 | 实验环境差异 |
|---|---:|---:|---|
| dask #5510 attempt 1 | 78 | 2 | pytest 兼容适配 |
| numba #9636 attempt 1 | 98 | 2 | 使用镜像自带 testbed 环境 |
| pydicom #2065 attempt 1 | 68 | 2 | 冻结数据修复镜像 |
| streamlink #3485 attempt 1 | 59 | 2 | readguard 制品 |

每个 Sandbox 只有一条完整轨迹；相同轨迹的两个副本使用独立可写工作盘，不共享执行产物。
回放真实工具命令，按记录的初始间隔和工具间等待以 1× 速度运行。
所有 L2 启动、环境设置及 READY 屏障完成后，按固定 10 秒窗口内的到达偏移释放。
三次重复的 seed 分别为 20261029、20261030、20261031；精确 lane/case/offset 以
[arrivals.json](../experiments/concurrent/arrivals.json) 为准。N=4 的旧参考序列也保留在文件中，不计入本轮冷层矩阵。
当前已完成的是混合异步到达；同步恢复压力不是已完成结果。

工具返回非零不自动判失败：轨迹中的预期错误必须按原观测签名核对。
审计同时检查工具数量、状态/业务输出、代码最终状态与补丁签名、缺失结果、存活 VMM 数、L1/L2 OOM、配额回执及冷层实际使用。

**P 与单个工具不设置执行时限。v13 的 F 使用同配对 P 批次时长 2 倍截止。** `REPLAY_TIMEOUT_S=0`，回放显式使用
`--disable-command-timeouts`；原输入内的 API timeout 作为元数据保留，不用于把较慢执行判成失败。
外层 VM SSH 建连 240 秒、L2 READY/释放屏障 1800 秒、故障取证和清理等待属于基础设施保护，
不能计成 workload 超时失败。准备失败、手工中断和资源保护退出须单列 censored/infrastructure。
`REPLAY_EXPORT_TIMEOUT_S` 是继承环境字段，当前无执行路径据此截止；不要据其名字解释轨迹结果。
正常完成的 L2 留在宿主上直到整组完成或 F 达配对截止，统一释放，避免提前销毁改变驻留密度。

F 参考必须是同介质、DRAM、N、seed、slots 与重复编号下已正确完成且无 OOM 的 P。
阈值为 `2 × (P.all_done_epoch − P.release_epoch)`；F 以释放后的单调时钟计时，完成屏障优先于截止检查，启动和归档不参与计时。
达到阈值仍未完成则记录 `control-relative-timeout`、保存截止时内核/资源/控制器状态与已完成结果，再强制清理所属 VM 并继续。
P 失败时不运行其 F；控制器故障、业务错误和 OOM 优先按各自原因停止诊断。
未完成 F 不填写 `batch_s`，保留阈值、实际停止用时及参考身份；不能当作 P 失败或伪造有限完成时间。

## 3. 策略与矩阵

上层池在 L1 汇总各 Sandbox 的实际 fast 驻留和阶段需求，复用 `waterfill/settle_grants`，
只有观察到真实页归还才允许转授配额。授予值经 telemetry 通道传入 Guest，并由单 Sandbox 控制器裁剪有效目标。
P 的局部需求为 ACTIVE 6144 MiB、WAIT 16 MiB，最终受全局授予值约束。
F 复用同一内核、VMM、回放、冷层和观测路径，局部 ACTIVE/WAIT 需求相同，用均分策略。
F 不是未经修改的 Linux 或论文原始机制。

| 冻结版本 | L1 DRAM | 介质×策略 | 重复 | 计划组数 | 状态与用途 |
|---|---:|---|---:|---:|---|
| v4 | 12 GiB | CXL/SSD × P/F | 3 | 12 | 全部完成；固定初始预算、F 为不变均分配额 |
| v5 | 10 GiB | CXL/SSD × P/F | 1 | 4 | 全部完成；同 v4 策略的加压探索 |
| v5 | 8 GiB | CXL/SSD × P/F | 1 | 4 | CXL-P 发生 L1 OOM，后 3 组未执行 |
| v6 | 8 GiB | CXL/SSD × P/F | 1 | 4 | 动态安全预留修复后全部完成；SSD P/F ceiling 有轻微差异 |
| v7 | 7 GiB | CXL/SSD × P/F | 1 | 4 | 匹配 ceiling 修复后的继续加压；最新状态见结果快照 |
| v8 | 6 GiB | CXL/SSD × P/F | 1 | 4 | 沿用 v7 策略，降低共享 DRAM；独立新配置与标签 |
| v9 | 6 GiB | SSD × P/F | 1 | 2 | 延后初始预算提交与 READY 入场门控；同资源回归 |
| v10 | 6 GiB | SSD × P/F | 1 | 2 | 在相同硬件上改用有界比例预留；P/F 固定相同预留及 ceiling |
| v11 | 6 GiB | SSD × P/F | 1 | 2 | DAMON 生命周期容错、控制器进程/心跳和容量状态新鲜度回归 |
| v12 | 7 GiB | SSD × P/F | 3 | 6 | 首 P 通过；首 F 用户采用新截止规则后停止，其余四组移交新版本 |
| v13 | 7→6 GiB | SSD × P/F | 3 | 12 | 7 GiB P 三次全通过、F 三次有效两倍截止；6 GiB 首 P 因 VMM SIGSYS 未通过，其余停止 |

累计 56 个计划点（包括历史版本未运行的计划）。禁止将未运行点或仍运行点当作成功，也不能跨版本平均成“三重复”。
最新通用入口使用 v16 的 I/O worker 上限与完整物理审计窗口、v13 配对截止及 v11/v12 内存和控制器健康策略；重跑 12/10/8 GiB 将产生新结果，不会重现旧版本的策略语义。
需要审查旧行为时使用对应 `snapshots/v*/` 和发布协议，不应将旧 OOM 隐藏为最新版本失败。

v6/v7 以 fast 节点实际 managed 容量计算安全预算，扣除运行时非 payload 占用并保留 1 GiB：
dirty/writeback/buffer/slab 不当作可立即支出的额度，只认可 clean 非 shmem 文件缓存。
预算缩减立即执行；增加每 tick 最多 64 MiB，并继续依据实际物理回执结算。
此时 F 是**共用动态安全预留下的阶段无关均分**，与 v4/v5 的不可变配额不同。
v7 保持 P/F 同一 ceiling，避免 F 的较高启动开销永久压低 ceiling；实际 budget 仍可因物理压力低于 ceiling。

嵌套环境中 PEBS 不可用，冻结配置明确为 `SB_SAMPLING=0`；采样、采样驱动 promotion/exchange 为零。
现有数据验证的是阶段配额、分配、降层、回收/reporting 和实际容量路径；
不能据此声称已实证完整的 PEBS 驱动页面未来收益算法，也没有有效的采样原始机制对照。
源码中的下层设计与这一运行覆盖范围分别看待。

7 GiB SSD-F 按用户明确指示记录为严重超时并停止：观测 11652.7 秒，6 条轨迹到达完成屏障、2 条 Numba 未完成，终止时 8 个 VMM 存活、L1 OOM 计数为 0。
该结果属于 F 对照的性能退化，不登记为 PhaseTieing P 失败；观察时间是完成时间下界，不能填入已完成组的 `batch_s`。
`terminations/` 保留用户终止决定；原冻结协议和“不启用自动执行截止”的设置保持原样。
归档时通过单独 16 GiB 恢复 VM 只读恢复工作盘输出，恢复资源不参与测量，且没有重跑工作负载。

v8 的 6 GiB SSD-P 在轨迹尚未释放时，首个可授予预算为 0，触发初始化 RuntimeError，驱动随后清理实例。
该点无 L1 OOM，分类为 `startup-budget-check-rejected`，不作为业务失败或 OOM 容量边界。
v9 固定 1 GiB 预留不变，暂时预算不足时记录零授予并等待；全体 READY 且真实物理额度满足 N×16 MiB 后才提交初始 ceiling，轨迹也等待入场成功后释放。
持续不满足条件时作为启动入场受限单列，不能谎报成功承载。SSD 两策略使用相同规则和配对 ceiling。

v9 在全体 READY 后，空 workload 仍只有约 720 MiB 物理余量，固定 1 GiB 预留持续阻止入场，因此保留为启动诊断，不记业务失败。
独立 v10 的 `reserve_policy=scaled`：预留 managed DRAM 的 1/16，向下取整至 16 MiB，范围 256–1024 MiB；当前 6 GiB 点约 368 MiB。
P/F 固定相同实际预留和 ceiling，物理信用约束不变。旧版 `fixed` 模式仍为 1024 MiB，跨预留版本不能混合性能结果。

v10 SSD-P 后发现 slot-5 控制器在 DAMON 停止请求上遭遇 EPERM 并退出，宿主仍使用旧容量状态，故该点为 `infrastructure-controller-exited`。
六条完整业务结果保留，但策略性能无效；无完整组完成时间，不计为健康容量通过。
v11 不改变 v10 的预留、ceiling 或硬件，单独增加确认已关闭 DAMON 的幂等处理、PID+启动时间验证和心跳检查。
5 秒以上的控制器容量状态不用于新物理信用；进程退出或 30 秒心跳停滞触发控制基础设施故障诊断，不是工作负载执行时限。

v11 在全部控制器存活、心跳新鲜时，于释放 449.135 秒后发生真实 L1 全局 OOM，内核杀死 VMM，保留为资源失败。
v12 的 7 GiB SSD 与 v11 使用相同实现。首个 P 批次 1296.434 秒、8/8 正确；首 F 的新截止阈值为 2592.869 秒。
用户提出规则时 F 已超阈值，实际停止用时 3307.923 秒，6/8 正确、无 L1 OOM，标记 `cutoff_adopted_late=true`。
原协议和源码不倒改。v13 使用全新标签重跑 7 GiB 三配对后，再在同一冻结实现下复测 6 GiB；旧 6 GiB 真实 OOM 保留。

v13 在 7 GiB SSD 下完成三次 P/F：P 批次分别 1280.801、1262.510、1262.065 秒，24/24 正确；F 各 6/8 正确且在配对两倍时长处停止。
6 GiB 首 P 在释放 728.095 秒后 slot-0 VMM 消失，vm.log 出现 `Possible seccomp violation`；本次 L1 OOM 计数为 0。
这是 `infrastructure-vmm-sigsys`，不能计通过，也不能直接视为本次 OOM 容量边界。
原工作盘保留，独立 v14 只用 strace 定位 SIGSYS，seccomp 保持开启，诊断不计正式性能。

## 4. 容量、性能和内存口径

主容量条件是整组运行期间 **L1 无 OOM**；同时要求 N 个 VMM 存活、8 条轨迹业务审计通过。
L2 OOM、VMM 丢失、业务签名变化、输入/部署问题分别报告；它们不等同于 L1 容量边界。
任一异常停止后续点，保存原始证据并诊断，修复后使用新版本、新标签回归。
已通过的点仅证明这些输入和到达序列可运行，不代表已找到最大承载量。

采用论文“所有提交 workflow 的离线完整宿主峰值之和 / 可用宿主容量”的思路，
本课题单独研究 DRAM 超售，明确将分母改为共享 DRAM：

\[
\rho_{DRAM}=\frac{\sum_{j\in\text{全部提交实例}}M^{offline,host}_{j,peak}}{D_{shared,DRAM}}
\]

CXL 不计入此分母，CXL:DRAM 或 SSD:DRAM 另报；当前 64/D 分别为 5.33、6.40、8.00、9.14、10.67。
N=8 时提交数和驻留数相同；以后有队列时分子包括全部提交实例，而非只算正在驻留的实例。
17.492 GiB 是八实例 **VMM RSS 峰值之和**，只可给出诊断需求比 1.458、1.749、2.187、2.499，
不能直接命名为完整宿主口径的正式超售比。8×12/D 只是名义内存配置比，也不是正式超售比。
目前完整宿主离线画像的窗口和缓存口径尚未最终冻结，因此结果包不宣称已经确定正式数值。

已有 `memory.current + memory.swap.current` 候选重算包括大量镜像文件缓存，N=8 峰值之和约 50.517 GiB；
该数仅保留为画像候选，不替换正式分子。参考论文为 TenonAgent（所给 `paper_eurosys.pdf` §5.4，
原实验是单一 DRAM 宿主）；DRAM-only 分母是本研究明确的分层扩展口径。

发布数据同时保留三类时间：

- `batch_s`：cohort release 到最后 lane.done，包含到达错峰、等待、Guest 回放结果序列化和收尾；不含 VM 冷启动和屏障后的宿主打包/传输。
- `trace_makespan_s`：各 lane 的回放 E2E 加计划到达偏移的最大值，排除回放结果序列化后的收尾。
- `service_sum_s` / 逐实例 `service_s`：真实工具服务时间，避免长 WAIT 掩盖性能变化。`execution_s` 等原测量字段保留在逐实例表。

逐工具 `PHASETIEING_STEP` 时序和资源流保存在原始日志，Git 发布包以逐组/逐实例汇总及原始文件哈希为主。
内存按实际时间积分，分别报告 DRAM 均值、采样峰值、GiB·s 和冷层驻留。
v4 历史资源摘要从 whole-L1 占用中扣除文件缓存、Buffers、SReclaimable，但保留 shmem/VMM RAM；
它是该版本的已测指标。v6/v7 安全控制器对“可即时支出”的回收额度更保守，两者不能混为同一计量公式。

## 5. 外部制品与构建

源码已包括运行控制器、Linux 补丁和修改后的 VMM；按 [构建方法](build.md) 编译。
实验适配保留 `/sys/kernel/mm/demeter`、CONFIG 和符号 ABI，但文件名统一为 PhaseTieing。
使用发布源码新编模块时 `module_abi=published`；如使用原测量二进制，仅重命名 .ko 文件，
模块内部名字仍旧，需 `module_abi=measured-legacy`。两者不可混用。

每个外部 artifact 子目录（4 个名称见配置工具）需要：

```text
vmlinux.bin                 # .state/kernel-build/arch/x86/boot/compressed/vmlinux.bin
PhaseTieing_placement.ko
PhaseTieing_balloon.ko
policy_exec                 # 静态编译 artifact 中的 policy_exec.c
rg                          # 可在 Guest 运行的 ripgrep 二进制
```

`policy_exec` 可用 `cc -static -O2 -o policy_exec policy_exec.c`；`randomize_pages` 可由
`backend/randomize_pages.c` 编译。VMM 使用 `backend/vmm` 的 musl release。
四个目录的文本脚本由工具从源码填充，不应另拿旧 controller/replayer 覆盖。
原测量二进制哈希见 [measured-binaries.json](../experiments/concurrent/measured-binaries.json)；
重编后哈希自然不同，应保留新 `staged-assets.json` 而非伪装成原二进制。

还需准备以下不进 Git 的外部资产：

| 资产 | 必要条件 |
|---|---|
| L1 base + kernel/initrd | 本实验为 Ubuntu 24.04 类环境、root=/dev/vda1；36 vCPU、cgroup v2、可用嵌套 KVM；配置的用户 SSH 和免交互 sudo 可用 |
| L1 工具 | Python 3.12、QEMU/qemu-img、numactl、tmux、dnsmasq、iproute2、curl、e2fsprogs、openssh、tar/gzip、systemd、ext4 |
| L2 base | 扁平 qcow2，实际测量使用 root=/dev/vda2 的 Guest，用户 clear、sudo、SSH、virtio block/net/balloon、cgroup v2；与内核/模块匹配 |
| 4 个工作盘 | 独立冻结 ext4 镜像，SHA256 必须匹配 `inputs.json`；包含修复数据、conda/testbed 和测试依赖 |
| SSH | 自己生成并安装到 L1/L2 的专用 key；仓库不含私钥 |
| 存储 | 独立 256 GiB 稀疏 data 盘、72 GiB 稀疏冷盘；需足够实际空间容纳镜像副本、工作盘、日志，整组低于 8 GiB 剩余空间会资源保护退出 |

工作盘的 Docker tag 是来源线索，**仅拉取 tag 不会得到逐字节相同的修复镜像**。
这些冻结工作盘、L1/L2 系统盘及预编译内核/VMM 尚未公开下载；因此本仓库是源码、配置、校验信息及测量摘要发布，
不是零外部依赖的完整 AE 镜像包。取得四个冻结工作盘前可审查和生成计划，不能声称已完成精确输入复现。
替代/重建镜像必须用新版本清单标注并重新标定业务签名。

## 6. 执行步骤

以下命令在仓库根目录执行。先检查源码并获取公开输入；获取工具固定 commit 并校验全部 8 个文件，拒绝覆盖不匹配内容：

```bash
bash bin/check-source
python3 tools/fetch_concurrent_inputs.py --output "$PWD/.state/concurrent-inputs"
# 已有 agentcgroup checkout 时也可添加 --checkout /absolute/path/to/agentcgroup
```

复制 `experiments/concurrent/site.example.json` 为自己的配置，例如 `.state/site.json`。
填写绝对 `lab_root`（新的专用目录，不能已存在）、CPU/NUMA 映射、独占 SSH 端口和全部 assets。
`assets.traces` 指向刚生成的 `.state/concurrent-inputs/traces`。
L0 必须提供嵌套 KVM、允许实验 CPU/NUMA 映射、sudo 无交互、足够 locked memory；
不要把其他人的 VM/磁盘填入自己的实验配置。L1/L2 base 应先 `qemu-img convert` 为无外部 backing 的扁平 qcow2。

```bash
# 只输出计划，不访问硬件，不启动 VM。
python3 tools/concurrent.py plan --config .state/site.json > .state/planned-matrix.json
# 新建自有目录，改写原机路径，生成和检查脚本，不复制大镜像、不启动 VM。
python3 tools/concurrent.py prepare --config .state/site.json
# 校验 trace/工作盘哈希，复制外部资产，写新的 asset/source 清单，不启动 VM。
python3 tools/concurrent.py stage-assets --config .state/site.json
# 显式启动临时 16 GiB L1，将输入预置到私有 data 盘，再退出；不测 workload。
python3 tools/concurrent.py bootstrap --config .state/site.json
# 正式依矩阵逐组启动受限 L1；8 个 L2 并发。F 性能超时继续，OOM/控制器/业务异常即停。
python3 tools/concurrent.py run --config .state/site.json
```

`dram_gib` 可设 `[12,10,8,7,6]`、`repetitions` 可设 `[1,2,3]`，生成当前策略的 60 个配对点；
当前样例按顺序为 7→6 GiB、SSD 各三重复，共 12 点；`media` 设为 `["cxl","ssd"]` 可计划两介质，但当前 v13 计划范围只有 SSD。
样例显式设 `reserve_policy=scaled` 并要求控制器健康。设为 `fixed` 可使用固定 1 GiB 规则，但会产生另一种策略配置。
三重复不能挪用旧 v4 的结果填入。
更改 N 或加入同步恢复要重新定义 CPU 池、输入副本数、64 GiB 冷层分配与到达协议；当前入口会拒绝 N≠8。
自动运行只对 F 使用配对 P×2 批次截止，作为删失性能结果并继续；P 与单工具无限时。人工终止需记录 censored 原因，不能重启覆盖标签。

输出位于 `<lab_root>/experiments/agentcgroup-PhaseTieing/results/<run_name>/`：
`matrix.json`、`protocol.json`、`source-manifest.json`、`staged-assets.json`、`status.json`，
以及每组的 `audit.json`、`mechanism-audit.json`、L1 资源/配额流和每 lane 的回放结果。
F 超时还保存 `control-timeout.json`、`cutoff-resources.json`、`cutoff-controller-health.json`、`cutoff-l1-dmesg.log`。
截止前只有限读取 Guest 内核日志、控制器末尾 128 KiB 以及完整 `result.json`，不强制导出整个工作树，以减少取证本身的换页。
取证或健康检查不全会停下诊断，不会冒充有效性能超时继续。
异常时 L1 VM 和磁盘保留用于诊断；按该组 `outer.json` 中的身份检查后，通过生成的
`outer.py stop --label <label>` 清理所属 VM。不要按模糊进程名批量 kill。

本次发布实际验证了：原公开输入固定提交与 8 文件哈希、离线矩阵、生成脚本语法和导入依赖、
真实 Bash 调用禁用记录 timeout、全局配额/物理回执和 OOM 修复回归单测。
配置化入口没有在第二台机器上完成整批运行；结果来自原测量 runner，二者在发布包中明确区分。

## 7. 已测结果与数据追溯

见 [结果汇总](../published/concurrent-20261030/README.md)、
[matrix.csv](../published/concurrent-20261030/matrix.csv) 和逐组 `audits/`。
`snapshot.json` 提供截取时间；`protocols/` 和 `source-manifests/` 保留每版协议和原始源文件哈希。
原始完整日志在测量机保留，发布摘要附有其 SHA256；Git 不包含 VM 磁盘、原始长日志和凭据。
源码、测量制品与可复现资产的不同身份不能用同一 SHA 混写。


## 8. SIGSYS 诊断与物理窗口修正

v14 仅跟踪 slot-0；释放 7190.052 秒后首次丢失 slot-3，slot-3/4 日志有 seccomp 提示。
4/8 完整结果通过、L1 OOM 计数为 0；未捕获被拦调用。此诊断不计正式性能，不声称 6 GiB 已成功或容量必然不足。
v15 使用独立 L1 tracefs 实例的 `force_sig_seccomp` 探针，覆盖全部 VMM，记录 syscall/TID，无 ptrace。
过滤规则和内存算法不变，先自测捕获 syscall=39，再启动专属 P6/r1 复现。

v14 原物理审计只覆盖完整轨迹的较早窗口，导致 minimum_live_vmms=8 漏掉后期 VMM 丢失。
v15 改为 cohort release 至全部完成、性能截止或清理前失败快照的完整区间；退出清理不计入业务窗口。
原审计文件不倒改；失败身份由当时的资源快照补充，首次丢失为 slot-3。

源码见 `experiments/concurrent/diagnostics/` 与 `snapshots/v15/`；
[诊断证据与限制](../published/diagnostics-20261031-sigsys/README.md) 单独发布，不混入正式并发矩阵。


## 9. v15 OOM 与 worker 上限回归

v15 在释放 240.050 秒后发生 L1 全局 OOM，内核杀死 slot-5；本次没有 SIGSYS，探针无丢失。
最后可用 slot-5 样本中 io_uring worker 达 5243 个；故障快照中 slot-0 总线程为 6647。
工作负载的受控总预算已降至 0，非 payload 占用仍约 5529 MiB；该值也包括 Guest 系统等开销。

新 VMM 在 `RawFileAsync::new` 中、首次提交之前，通过 IORING_REGISTER_IOWQ_MAX_WORKERS
将 bounded/unbounded 限额各设为 8；错误直接作为创建失败返回，不静默回到未限制状态。
此限额遵循内核 I/O 工作队列/NUMA 作用域，不等同于整个 VMM 最多 16 个线程。
异步 I/O、队列深度、seccomp、内存分层策略、36 vCPU / 6 GiB / 64 GiB SSD 及各 L2 的 4 vCPU / 12 GiB 配置保持一致。

v16 先做 P6/r1/N8 的系统修复回归，保留全 L1 SIGSYS 探针，记录实际 VMM/worker 线程数和 OOM。
通过单次回归后仍需同一新 VMM 下的 P/F 配对和重复；不能混合旧 VMM 的时延计算策略收益。
旧 v19 二进制与原始失败点保留，公开仓库只提交源码与二进制哈希。

通用入口已切换到 v16 源码。站点的 `cloud_hypervisor` 外部制品需使用包含 worker 上限的新构建；
已测新制品身份见 `published/diagnostics-20261031-sigsys/v20-vmm-build-proof.json`，旧 measured-binaries 清单继续描述历史 v19。
L1 还需可写 tracefs 和 CONFIG_KPROBES/force_sig_seccomp，以保留已自测的全实例 SIGSYS 取证。
默认通用矩阵仍为 7→6 GiB 各三重复 P/F，测量机当前的 v16 单次 P6 修复回归不填充这些新重复。
