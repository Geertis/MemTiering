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
