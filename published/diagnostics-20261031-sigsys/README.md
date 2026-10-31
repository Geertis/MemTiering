固定 N=8、L1 36 vCPU / 6 GiB DRAM + 64 GiB SSD，诊断 VMM 的 SIGSYS 退出；本目录的测量不计正式性能或成功超售点。

# SIGSYS 诊断与改动

v14 已在 2026-10-31 06:20 停止。释放轨迹后 7190.052 秒出现 VMM 丢失，4/8 条完整结果通过，L1 OOM 计数为 0。
第一次异常快照中 slot-3 已无 VMM，slot-4 当时仍在；两者最后的 vm.log 都出现 `Possible seccomp violation`。
原诊断只跟踪 slot-0，未捕获这两个 slot 的系统调用号，因此不能据此修改 seccomp 允许列表。
此运行持续约两小时，结束原因是 VMM 丢失，不是 P 或工具墙钟截止；不将带 strace 的时延计入正式比较。

v15 改为在独占 L1 的独立 tracefs 实例中探测 `force_sig_seccomp`，记录 syscall、TID 和原始事件；覆盖全部 VMM，无 ptrace。
每 CPU 16 KiB ring buffer，仅在 SIGSYS 路径触发；保存每 CPU 丢失/覆盖计数，退出时删除所属探针和实例。
保持 seccomp 过滤和内存/控制器算法原样，先据具体系统调用确定应修复的路径。
自测通过：测试进程自己安装一个只拦截 getpid 的过滤器，实际捕获 syscall=39，探针退出和清理成功；这不是 VMM 根因的自测。

同时修正 v15 的物理资源审计窗口：从 cohort release 覆盖至全部完成屏障、性能截止或清理前失败快照。
此前 v14 原审计的 minimum_live_vmms=8 只来自已完成轨迹的早期窗口，遗漏了两小时后的异常；原审计保留，以失败快照补充解释。

|版本|用途|当前结论|
|---|---|---|
|v14|slot-0 strace / P6 r1|失败，首次丢失 slot-3；未捕获被拦 syscall，4/8 完成，无 L1 OOM|
|v15|全 L1 内核信号探针 / P6 r1|独立后台诊断，运行中不计通过；源文件及协议已冻结|

相关源码：`experiments/concurrent/diagnostics/`、`experiments/concurrent/snapshots/v15/`。
正式并发结果仍见 `published/concurrent-20261030/`；v14/v15 不混入其正式矩阵。
移机的通用入口仍使用 v13；本诊断源码用于审查和在具备相同专属 L1/tracefs 条件下复现，未声称在第二台机器完成运行。
