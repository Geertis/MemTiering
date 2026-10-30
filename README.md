# PhaseTieing

面向 Agent Sandbox 的阶段感知两级 CXL 分层内存管理实现。系统在工具执行前利用
Runtime 事件感知资源阶段，在多个 Sandbox 之间分配 DRAM，在每个 Sandbox 内按
页面未来收益决定 DRAM/CXL 放置，使有限 DRAM 可以随执行阶段在实例之间复用。

## 已实现的机制

|设计|实现入口|
|---|---|
|工具 start/end、search/test/build 阶段识别，fork/exec 前同步阶段与分配偏好|[`tools/reference/replay_agentcgroup.py`](tools/reference/replay_agentcgroup.py)、[`experiments/trajectory_launcher.py`](experiments/trajectory_launcher.py)|
|ACTIVE/WAIT、实际驻留、内存压力、性能敏感度、启动 headroom 与多 Sandbox DRAM 配额协调|[`runtime/cxl/cxl_controller.py`](runtime/cxl/cxl_controller.py)|
|保留独立 mm 和虚拟范围，汇聚 Sandbox 级候选，跨进程冷热配对|[`backend/kernel-overrides/mm/PhaseTieing/sandbox.c`](backend/kernel-overrides/mm/PhaseTieing/sandbox.c)|
|PEBS 访问强度、阶段复用历史、子进程寿命与迁移/交换成本共同决定未来收益|同上：`sb_benefit`、`sb_candidates`、`sb_move`、`sb_exchange`|
|新分配页的层级选择、配额记账和迁移后记账|[`backend/kernel-overrides/mm/PhaseTieing_alloc.c`](backend/kernel-overrides/mm/PhaseTieing_alloc.c)|
|WAIT 页归还、ACTIVE 恢复屏障、Host 实际页归还回执|[`backend/patches/`](backend/patches)、[`backend/vmm/`](backend/vmm)|
|SSD 扩展、DAMON/DAMOS 冷页回收和有界页面复用历史|[`runtime/ssd/`](runtime/ssd)|

完整说明见 [设计与代码对应](docs/architecture.md)、[构建方法](docs/build.md) 和
[运行接口](docs/backend-requirements.md)。本次整理的检查结果见 [发布验证](docs/verification.md)。

2026-10-30 已同步真实冷层并发实验实现，包括跨 VMM 配额池、运行时安全预留、
配对 ceiling 修复与无执行时限回放。复现从
[实验设置与步骤](docs/concurrent-reproduction.md) 开始；
[结果包](published/concurrent-20261030/README.md) 附逐组矩阵、逐实例指标和 OOM 证据。
7 GiB SSD 均分对照按用户指示单列为严重超时（6/8 条轨迹完成、无 L1 OOM）；
6 GiB CXL 两组均 8/8 通过；SSD 的启动入场受限和 v10 控制器故障分别保留，后者的策略性能无效。
v11 修复 DAMON 生命周期和控制器健康监督后，在 6 GiB SSD 点记录到真实 L1 OOM。
v12 的 7 GiB SSD-P 首重复在 21.6 分钟完成；F 在新规则采用后于 55.1 分钟停止，6/8 完成、无 L1 OOM。
当前 v13 沿用该内存实现，先在 36 vCPU / 7 GiB DRAM / 64 GiB SSD、N=8 完成三个 P/F 配对，再复测 6 GiB。
F 达同重复 P 批次时间两倍仍未完成则记性能超时、强制停止并继续；P 和单工具无限时。
两策略匹配预留和 ceiling，当前实现与测量限制见复现文档。

## 源码检查

需要 Python 3.12、Bash。运行既有测试、Python/Bash 语法、文件命名和发布清单检查：

```bash
bash bin/check-source
bin/PhaseTieing-controller coordinator --help
bin/PhaseTieing-controller cxl --help
bin/PhaseTieing-controller ssd --help
```

## 运行入口

多 Sandbox CXL 协调器运行于已配置后端的 Guest 中，`budget-mib` 应符合实际 fast
NUMA 节点容量；配置文件内的 cgroup 需要预先存在：

```bash
bin/PhaseTieing-controller coordinator \
  --config configs/sandboxes.example.json \
  --budget-mib 4096 --minimum-mib 16 --startup-headroom-mib 128 \
  --fast-node 0 --slow-node 1 \
  --socket /tmp/PhaseTieing.sock --log /tmp/PhaseTieing-controller.jsonl
```

`coordinator` 是具备压力与历史反馈的多 Sandbox 配额控制器。
`cxl` / `ssd` 入口保留 V67/V66 实验使用的单 Sandbox 固定阶段配额配置；
ACTIVE 6144 MiB、WAIT 16 MiB 是该实验配置，不代表系统仅支持固定配额。
跨 VMM 的实测适配位于 `runtime/host/global_pool.py`；配置化实验入口为
`python3 tools/concurrent.py --help`，详见 `experiments/concurrent/`。

## 仓库内容

- `runtime/`、`configs/`、`bin/`：阶段控制器、配置和入口。
- `backend/`：Linux 6.10 补丁序列、最终内核源码覆盖、Guest 配置和 Cloud Hypervisor 源码。
- `experiments/`：当前实验调度、快照源码、回放和测量工具。
- `evaluation/reference/`：之前整理的实验参考源码。
- `tests/`：配额守恒、事件顺序、阶段时长、页归还屏障、SSD 放置与页面历史测试。
- `published/`：已有实验报告、汇总 CSV 和图表；不表示本次上传重新执行了性能实验。
- `release-manifest.json`：本次发布文件的 SHA256 与字节数。

上传内容与重命名规则见 [发布说明](docs/upload.md)。项目文件与目录使用 `PhaseTieing`；
兼容 ABI、上游来源链接、版权声明和实验原始记录保留其技术名称，见 [NOTICE](NOTICE.md)。
