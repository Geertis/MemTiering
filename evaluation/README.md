# 实验源码与结果

2026-10-30 的当前并发容量实验从 [concurrent](../experiments/concurrent/README.md)
进入：包含 L0/L1/L2 设置、28 点历史矩阵、配置化 v7 入口、固定公开 trace 提取工具、
逐版结果及失败证据。完整复现步骤见 [concurrent-reproduction](../docs/concurrent-reproduction.md)，
已探索结果见 [并发结果包](../published/concurrent-20261030/README.md)。
下文描述此前的串行全量实验，不与当前容量探索混算。

`experiments/phase/all_traces_v67/` 包含当前已测全量实验的调度、隔离、语义检查、
审计和单次运行入口；其余 `experiments/phase/` 目录提供依赖工具和冻结源码快照。
`reference/` 保留此前整理的参考副本。正常审阅从 `experiments/` 和 `runtime/` 开始。

`runtime/ssd` 对应 V66 compact SSD，`runtime/cxl/adaptive_controller.py` 对应 V67
compact CXL 的固定阶段配额配置。多 Sandbox 上层完整策略位于
`runtime/cxl/cxl_controller.py`，CXL 下层收益判断位于
`backend/kernel-overrides/mm/PhaseTieing/sandbox.c`。

运行原有实验入口需要设置 `PHASE_TIEING_ROOT` 为仓库绝对路径，另行准备每个冻结
制品目录中的内核/VMM/模块二进制、系统盘、工作负载镜像和 trace 输入。
当前脚本保留原实验的 NUMA、网络、Guest 用户、资源隔离及部分报告路径假设，移机时
需要按实际环境调整；本次上传不启动实验队列或修改宿主机服务。

测量配置包含同一轨迹、同块新测的 DRAM、compact-dram、stock SSD、静态 CXL、
动态 SSD 与动态 CXL，三次重复。固定底座收益与阶段策略收益分别解释。
计量包含 E2E、工具服务时长、VMM Host 驻留的时间加权均值、采样峰值和时间积分。

`published/EXPERIMENTS.md`、`published/SSD-OPTIMIZATION.md` 及 `published/v67/`
保存已有报告和结果汇总。它们是历史测量结果，覆盖和结论以报告说明为准。
