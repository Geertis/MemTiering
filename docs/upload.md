# 发布范围与命名

上传目标沿用已配置的 `git@github.com:Geertis/MemTiering.git`，项目名称为 PhaseTieing。
发布工作树独立于原实验目录，原目录中的运行代码和数据保留原状。

2026-10-30 增补并发实验 v4–v8 冻结源码、最新跨 VMM 控制器、配置化复现工具、
输入获取/校验、矩阵和逐版本结果。`snapshots/` 保留测量来源内容以便追溯；
移机使用配置化入口，不直接运行含原机路径的快照。
公开轨迹按固定上游提交提取，冻结磁盘和预编译制品仅发布身份/哈希，外部资产缺口在复现文档明确列出。

本次纳入 Runtime 控制器、配置、测试、Linux 补丁与最终源码覆盖、修改后的完整
Cloud Hypervisor 源码、实验调度/回放工具，以及已有实验的报告和汇总图表。
不提交虚拟磁盘、预编译内核/模块、轨迹全集、工具链、缓存、日志或本机 SSH 凭据。

所有发布文件名/目录名中出现的 `demeter`（忽略大小写）均替换为 `PhaseTieing`。
`docs/filename-renames.json` 记录源目录到发布目录的路径对应。
相应调整 include、补丁目标路径、Kbuild 对象/模块名和运行脚本的模块路径。

兼容 ABI（sysfs、CONFIG、内核 C 符号和命名空间）、上游来源 URL、版权作者和
已有实验的原始记录保持其技术含义。历史 `SHA256SUMS*.json` 描述测量时的原始
制品，发布后源码完整性以根目录 `release-manifest.json` 为准。

`bin/check-source` 运行已有测试及发布检查，GitHub Actions 对 push/PR 执行相同检查。
内核补丁另通过 `tools/prepare_backend.py` 从校验后的 Linux 6.10 归档重建验证。
