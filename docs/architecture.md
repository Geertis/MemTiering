# 设计与代码对应

PhaseTieing 已实现 Runtime 阶段信号、Sandbox 间 DRAM 配额协调和 Sandbox 内
future-benefit-aware 页放置。以下按执行路径列出代码，便于直接审阅设计与实现。

## 1. 在热点形成前识别阶段

`tools/reference/replay_agentcgroup.py` 中的 `phase_kind` 将当前工具/命令识别为
普通、search、test、build 四类。`PhaseRuntime.notify` 通过 Unix socket 发送
`start` / `end` / `fallback` 事件；回放器在实际执行工具前等待 start 响应，
再设置分配偏好并 fork/exec。工具间等待对应 WAIT，下一次 start 对应恢复 ACTIVE。
`experiments/trajectory_launcher.py` 提供同一进程树的运行和子进程管理。

`runtime/cxl/cxl_controller.py` 的 `Controller.event`、`set_phase` 和 `rebalance`
将事件同步到内核。ACTIVE 先确定实际驻留和配额，再唤醒下层；WAIT 先停止晋升，
再撤销配额。未提供 Runtime 信号时也保留 CPU 进度/内存增长推断路径，供回退和消融使用。

内核 `sandbox.c` 按 cgroup 枚举任务，监听进程生命周期，注册和清理 PEBS 采样。
Runtime 命令类别和子进程生命周期共同影响阶段处理，无需等待访问统计积累成完整热点。

## 2. 上层：跨 Sandbox 的 DRAM 时间复用

`runtime/cxl/cxl_controller.py` 支持 1–8 个 Sandbox。`State` 保存阶段、当前驻留、
敏感度、压力和已完成阶段的时长/匿名页增长/文件页增长历史。

- `State.headroom`：按已完成的阶段增长和工具类别预留启动空间，限制文件缓存带来的过量预留。
- `State.horizon_ms`：从已经完成的同类阶段学习保守剩余时长；不读取轨迹未来时间。
- `Controller.measure`：读取 cgroup NUMA 驻留、CPU 进度和 memory PSI，并同步分配提交水位。
- `Controller.rebalance`：按 ACTIVE/WAIT、需求、权重、敏感度、慢层比例和压力计算份额。
- `waterfill`：在需求上限内进行带权分配，保证整数页预算守恒。
- `settle_grants`：撤销先于授予；只有实际离开 DRAM 的页面才释放可再分配额度。

因此 WAIT Sandbox 的配额下降不立即虚构可用 DRAM；待冷页实际迁出后，其份额才可
用于其他 ACTIVE Sandbox。配额是受控用户页的放置目标，整个 VM 的 Host RSS 还包括
内核、VMM 私有内存和其他开销，实验采用单独的 Host 驻留计量。

## 3. 下层：独立地址空间与统一候选池

`backend/kernel-overrides/mm/PhaseTieing/sandbox.c` 是 CXL 下层实现。

`sb_mm` 保存 `mm_struct`、generation、mapping epoch、MMU notifier、页/区域访问
历史及地址游标。虚拟地址与热度始终归属于各自的 mm，exec/退出和映射变化会触发
相应失效。PEBS 回调只入队样本，由有界 worker 解析和处理。

`sb_samples`、`sb_space_rate`、`sb_page_rate` 更新访问率与置信度；`sb_candidates`
将各进程候选汇聚到 `sandbox.hot` / `sandbox.cold`。有界堆保留扫描范围内最有用的
候选，支持跨 mm 配对。`sb_validate` 在执行前重新核验映射、页、NUMA 节点和归属。

## 4. 下层：未来收益与迁移门槛

`sb_benefit` 将当前/历史访问率、阶段剩余窗口、同类已完成进程的寿命经验、
DRAM/CXL 延迟差和关键路径系数合成纳秒单位的预计收益：

```text
预计访问次数 = 访问率 × 有效剩余窗口
预计收益 = 预计访问次数 × 延迟差 × 关键路径系数
晋升条件：hot 收益 > 迁移成本，且 DRAM 配额允许
交换条件：hot 收益 > cold 收益 + 交换成本
```

其中有效窗口受当前阶段和进程剩余寿命限制；`history[kind]` 提供历史阶段复用依据。
`sb_profile` 与退出处理只使用完整寿命观察训练经验，控制器拆除不构成真实退出样本。
`sb_move`、`sb_exchange` 用实际耗时更新成本，冷却期与 backoff 避免重复搬运。
`sandbox_future_benefit`、`sandbox_cross_mm`、`sandbox_placement_enabled` 等参数支持消融。

`backend/kernel-overrides/mm/PhaseTieing_alloc.c` 把新页分配、迁移、释放和驻留反馈接入
同一配额账本，控制 fast-node 分配额度，避免只对已存在页面进行事后迁移。

## 5. 页归还与实验配置

`ReportingGate`、page-reporting 补丁和 Cloud Hypervisor balloon 改动覆盖 WAIT 小块
空闲页归还、ACTIVE 恢复屏障及 Host 回执。Guest 内配额变化和 Host 实际释放分别记账。

`runtime/ssd` 是同一阶段机制的 SSD 扩展，使用 DAMON/DAMOS、cgroup 过滤、young-bit
复查、swap readback 冷却和有界复用历史选择回收页。

V66 SSD/V67 CXL 的全量实验使用固定 ACTIVE/WAIT 配额和明确的采样/放置开关。
例如 `experiments/phase/all_traces_v67/execution.py` 的该批配置关闭 PEBS 采样；
这是冻结实验配置，与 `coordinator` 的多 Sandbox 配额机制及内核完整收益机制分别记录。
已有报告在 `published/`，具体口径和覆盖范围以报告为准。
