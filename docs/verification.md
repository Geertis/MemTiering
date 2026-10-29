# 本次发布验证

2026-10-29 在原物理机上执行：

|检查|结果|
|---|---|
|既有配额、事件顺序、horizon、reporting barrier、SSD 放置与页面历史测试|46 项通过|
|Linux 6.10 归档 SHA256 及全部 23 个重命名后补丁|校验通过，全部应用成功|
|重命名模块编译与 MODPOST|`PhaseTieing_placement.ko`、`PhaseTieing_balloon.ko` 成功生成|
|Python/Bash 语法与发布路径检查|通过|
|发布文件 SHA256 清单|见根目录 `release-manifest.json`|

模块编译使用原已配置的 Linux 6.10 构建目录及 Clang 18，在独立验证目录中编译
发布源码，显式加入重命名后的分配器头文件路径。编译包含已有的函数声明、未使用
调试函数和 balloon 栈帧等警告；本次整理保持原算法和内核 ABI。

本次验证覆盖源码、补丁重建和模块构建，未重新执行性能实验或加载新模块。
`published/` 的性能结果来自报告中说明的既有测量。
