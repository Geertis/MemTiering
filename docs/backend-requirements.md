# 运行接口

PhaseTieing 运行在 Linux 6.10 定制 Guest 与修改后的 Cloud Hypervisor 上。
完整后端源码和补丁已纳入 `backend/`，构建步骤见 [build.md](build.md)。

## 公共要求

- cgroup v2：`memory.current`、`memory.stat`、`memory.numa_stat`、`memory.pressure`、
  `cpu.stat`、`memory.high`、`memory.reclaim` 及 swap 计量接口。
- Guest 中可写的页归还参数，包括 page-reporting order 与恢复屏障；VMM 实际接收归还页。
- Guest 内的控制器具有访问对应 sysfs、proc、cgroup 和 perf 采样所需的权限。
- fast/slow NUMA 节点与物理 DRAM/CXL 后端对应，预算符合实际 fast-node 容量。

## CXL

多 Sandbox 配额协调入口：`bin/PhaseTieing-controller coordinator`。
`configs/sandboxes.example.json` 包含 Sandbox ID、cgroup、weight 和 sensitivity。
当前内核最多支持 8 个 Sandbox。

控制器访问 `/sys/kernel/mm/demeter/sandboxes` 的 `nr_sandboxes`、`budget_pages` 以及
各 Sandbox 的 `cgroup`、`allocation_snapshot`、`residency`、`quota_pages`、`phase`、
`horizon_ms`、`stats` 等接口。该 sysfs 路径和 `CONFIG_DEMETER` 是兼容 ABI。
新编译模块使用 `PhaseTieing_placement.ko`，参数目录为
`/sys/module/PhaseTieing_placement/parameters`。

Runtime 与多 Sandbox 协调器的 Unix socket 使用一行 JSON 请求和一行 JSON 响应：

```json
{"sandbox":"sandbox-a","event":"start","kind":3}
```

`event` 为 `start`、`end` 或 `fallback`；`kind` 为 0（普通）、1（search）、2（build）、
3（test）。Runtime 必须在工具 fork/exec 前等待 start 响应，并使用响应的
`preferred_node` 设置分配偏好；工具结束后发送 end。参考实现位于
`tools/reference/replay_agentcgroup.py` 的 `PhaseRuntime`。

## 固定阶段配额实验

`bin/PhaseTieing-controller cxl` 和 `ssd` 运行 V67/V66 冻结的单 Sandbox 实验策略，
使用 `configs/sandbox.example.json` 及对应的 `*-6144-16.json`。
它们的消息以 `id` 标识工具调用，例如 `{"event":"start","id":1,"kind":3}`，
与多 Sandbox 协调器通过 `sandbox` 路由的协议分别使用。
CXL 实验入口还按 present pages 裁剪实际下层授予量，保留上层阶段目标。

## SSD

需要 Guest swap、DAMON paddr/DAMOS pageout、memcg 过滤和 young-bit 复查接口。
页面历史需要读取 `/proc/PID/maps`、`pagemap` 等。默认冻结策略将 WAIT 的额外 VM
文件缓存回收门槛设为 200 ms，单独记录该 Sandbox 用户页之外的缓存范围。
Guest 内回收与整个 VMM Host RSS 使用独立的测量口径。
