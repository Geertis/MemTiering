# 构建后端

发布包包含内核补丁与最终源码覆盖，以及修改后的 Cloud Hypervisor 36.0 源码。
构建不会安装或替换宿主机内核。Linux 压缩包和生成产物保存在 Git 忽略的 `.state/`。

## Linux 6.10 与内核模块

需要 Python 3.12、GNU patch、make，以及 Linux 构建依赖。已测工具链为
Clang/LLVM 18；内核配置见 `backend/guest.config`。

```bash
python3 tools/prepare_backend.py
mkdir -p .state/kernel-build
cp backend/guest.config .state/kernel-build/.config
make -C .state/kernel/linux-6.10 O="$PWD/.state/kernel-build" LLVM=-18 olddefconfig
make -C .state/kernel/linux-6.10 O="$PWD/.state/kernel-build" LLVM=-18 -j8 bzImage modules
```

已有 Linux 归档时可传 `--archive /path/to/linux-6.10.tar.gz`，仍会校验 SHA256。
`--destination` 可指定新的生成目录；脚本拒绝覆盖已有目录。

准备脚本依次应用 `backend/patches/series` 的 23 个补丁，再覆盖
`backend/kernel-overrides/`，后者是最终模块/内核修改快照。
生成树包含 `mm/PhaseTieing/`、`mm/PhaseTieing_alloc.c` 与
`include/linux/PhaseTieing_alloc.h`，Makefile/include/补丁路径已同步调整。

输出模块为：

```text
.state/kernel-build/mm/PhaseTieing/PhaseTieing_placement.ko
.state/kernel-build/mm/PhaseTieing/PhaseTieing_balloon.ko
```

新编译模块的模块参数目录相应为 `/sys/module/PhaseTieing_placement/parameters`
及 `/sys/module/PhaseTieing_balloon/parameters`。`CONFIG_DEMETER`、内核导出符号命名空间
和 `/sys/kernel/mm/demeter/` 保持兼容 ABI；项目文件名的修改不改变这些接口。

## Cloud Hypervisor

`backend/vmm/` 已包含修改后的完整 VMM 源码，无需重复应用 VMM 补丁。
需要 Rust 1.76 工具链及上游 Cloud Hypervisor 36.0 的系统构建依赖。

```bash
cd backend/vmm
cargo build --release --target x86_64-unknown-linux-musl
```

VMM 的基础修改与 Host 回执修改分别记录在 `backend/patches/vmm-base.patch` 和
`backend/patches/vmm-return-receipts.patch`，用于审阅来源。

## 镜像与实验输入

`backend/downloads.json` 记录 Linux 归档和上游 Guest 镜像分片的 URL/SHA256。
镜像、工具链、模块二进制和 workload 数据不进入源码提交。
完整实验还需要准备 Guest 系统盘、工作负载镜像/轨迹、Guest SSH、NUMA/网络和隔离配置，
其原有实验入口及环境假设见 [实验说明](../evaluation/README.md)。
