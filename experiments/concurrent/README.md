固定 8 个并发 Sandbox（每个 4 vCPU / 12 GiB 名义内存、一条完整轨迹），让它们共享 L1 的 36 vCPU、6/7/8/10/12 GiB DRAM 与 64 GiB CXL 或 SSD 冷层，以同到达序列比较阶段感知配额与均分配额，记录 OOM、正确性、工具时延和实际内存占用。

# 并发容量实验复现入口

- [完整设置、执行步骤与计量规则](../../docs/concurrent-reproduction.md)
- [已探索结果与限制](../../published/concurrent-20261030/README.md)
- [逐组矩阵 CSV](../../published/concurrent-20261030/matrix.csv)、[逐实例指标](../../published/concurrent-20261030/instances.json)
- [站点配置模板](site.example.json)、[公开输入与哈希](inputs.json)、[到达序列](arrivals.json)
- [测量时二进制身份](measured-binaries.json)、[L0 软件/硬件信息](environment.json)

`snapshots/v4..v13/` 是测量时的调度源码，保留原机绝对路径用于追溯；
`source-provenance.json` 记录其来源和哈希。它们不是移机运行入口。
`tools/concurrent.py` 根据站点配置生成独立实验目录；`templates/` 仅替换部署和矩阵适配，
采用 v13 的调度、配对截止与审计逻辑，内存和控制器健康策略沿用 v11/v12。
当前默认先做 7 GiB 三个 P/F 配对，再复测 6 GiB 三个配对。只有 F 使用同配对 P 批次时间 2 倍截止；P 和单工具无限时。原机绝对路径和兼容 ABI 中保留的旧名称
不代表发布文件名；发布文件及目录统一使用 PhaseTieing。

先在源码根目录生成计划（不需要 VM、镜像或 root）：

```bash
python3 tools/concurrent.py plan --config experiments/concurrent/site.example.json
```

准备自己的站点配置和外部制品后，依次执行 `prepare`、`stage-assets`、`bootstrap`、`run`，
详见完整步骤。`prepare` 和 `stage-assets` 不启动 VM；`bootstrap`、`run` 会启动私有 L1 VM。
新入口已通过离线生成、Python/Bash 语法、模块依赖和配对矩阵检查，尚未在第二台机器上完成整批硬件复测。
