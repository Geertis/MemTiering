6 GiB SSD-P 在轨迹释放前因“初始可授予预算为 0”的软件检查抛错退出，未发生 L1 OOM；已保留原始结果并以同资源 v9 回归延后初始化的修正。

# 异常组诊断

异常标签为 `v8-ssd-P-d6-n8-r1`，2026-10-30 20:16 左右结束。
`status.json` 中 `released=false`、`release_epoch=null`；所有 workload cgroup 的测量驻留为 0，尚无工具回放。
L1 内核日志没有 OOM，审计 `l1_oom=false`。

第一次取得全体 Guest 容量状态时，软件估计：

|项目|数值|
|---|---:|
|实际 managed DRAM|5901.45 MiB|
|非 payload 安全占用估计|5341.07 MiB|
|物理余量（含 clean file credit）|560.38 MiB|
|固定安全预留|1024 MiB|
|计算出的用户页预算|0 MiB|
|原初始化最低预算要求|8×16=128 MiB|

旧代码在首个预算小于 128 MiB 时直接 `raise RuntimeError('no payload DRAM budget after host overhead')`。
随后协调器的退出清理向 8 个实例发送终止，因此 `returncodes` 为 `-9`；不是 OOM killer 杀死 8 个业务，也不是 8 条轨迹各自失败。
原始错误、审计和驱动日志均保留，结果重新明确分类为 `startup-budget-check-rejected`。
该点不能用于宣布已达到 SSD 的 L1 OOM 容量边界。

## 6 GiB 已通过结果

|处理|正确性|L1 OOM|批次完成时间|
|---|---|---|---:|
|v8 CXL-P|8/8|无|1271.3 s|
|v8 CXL-F|8/8|无|1038.2 s|
|v8 SSD-P|轨迹未释放|无|无完整完成时间|
|v8 SSD-F|未运行|未测|未测|

两个 CXL 点均可承载当前 8 实例组合，但当前 P 的完成时间较长，不能据此宣称性能或承载量优于 F。

## v9 修正及回归

- 初始零预算记录为 `bootstrap-wait`，不抛异常，也不永久冻结为零 ceiling。
- 等全体 Sandbox 的 READY 屏障稳定，且物理预算满足最小入场额度，才提交初始 ceiling。
- 轨迹释放等待提交成功；持续不满足条件时单列启动入场受限，不记成业务失败或 OOM。
- 1 GiB 安全预留、物理信用结算、DRAM/冷层容量、N=8 和到达序列均保持不变。
- 原始瞬时占用是否会消退，由回归观测判断；代码不凭空授予内存，也不以缩小预留掩盖问题。

v9 为独立版本，仅回归 SSD P/F，标签为 `v9-ssd-P-d6-n8-r1`、`v9-ssd-F-d6-n8-r1`。
物理配置仍为 L1 36 vCPU / 6 GiB DRAM + 64 GiB 共享 SSD swap，每 Sandbox 4 vCPU / 12 GiB 名义内存、一条轨迹。
2026-10-30 20:34 已启动后台回归，完整源码测试 59 项和实测 runner 10 项通过。
当前状态以后台实时状态报告及 `PhaseTieing-concurrent-20261030-v9-startup-admission/status.json` 为准。

## v9 观测与 v10 继续优化

v9 已全体 READY，仍无 L1 OOM，但空 workload 时非 payload 占用约 5181 MiB、物理余量约 720 MiB，仍被固定 1024 MiB 预留挡住。
因此初始化时机并非唯一原因；在这个小宿主上，固定预留构成软件入场限制。
v9 在轨迹未释放的准备阶段结束并保留，分类为 `startup-resource-admission-blocked`，不记业务失败。

独立 v10 明确改变预留策略：实际 managed DRAM 的 1/16，向下取整到 16 MiB，最小 256 MiB、最大 1024 MiB。
当前 6 GiB 机器对应约 368 MiB 预留；P/F 固定使用相同预留和配对 payload ceiling。
这不是硬件配置变化；新旧预留版本的性能不混合比较，物理信用结算仍受实时 managed 容量和实际页归还约束。
READY 入场门控保留；只有实测安全预算满足要求才释放轨迹。

v10 已于 20:44–20:45 左右后台启动 SSD P/F 回归；完整测试 61 项、实测 runner 12 项通过。
当前实时状态改由 `PhaseTieing-concurrent-20261030-v10-scaled-reserve` 提供。启动与回放进度以实时记录为准，尚不能把准备完成当作容量验证通过。
