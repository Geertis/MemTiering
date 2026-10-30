#!/usr/bin/env python3
"""Render the human summary from the exported, version-separated records."""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import statistics


def render(folder):
    snapshot = json.loads((folder / 'snapshot.json').read_text())
    rows = json.loads((folder / 'matrix.json').read_text())
    groups = defaultdict(list)
    for row in rows:
        groups[row['version'], row['dram_gib'], row['medium']].append(row)

    def cell(values, policy):
        chosen = [v for v in values if v['strategy'] == policy]
        passed = [v for v in chosen if v['outcome'] == 'passed']
        if len(passed) == len(chosen):
            durations = [v['batch_s'] for v in passed]
            time = f'{statistics.mean(durations):.1f}'
            if len(durations) > 1:
                time += f' ± {statistics.stdev(durations):.1f}'
            return f'{time} s；{len(passed)*8}/{len(chosen)*8} 正确'
        if any(v.get('l1_oom') for v in chosen):
            return 'L1 OOM，整组未完成'
        return '运行中' if any(v['outcome'] == 'running' for v in chosen) else '未运行'

    table = ['|版本|DRAM|冷层|每策略重复|P 批次完成|F 批次完成|',
             '|---|---:|---|---:|---|---|']
    for (version, dram, medium), values in groups.items():
        table.append(f'|{version}|{dram} GiB|{medium.upper()}|{len(values)//2}|{cell(values,"P")}|{cell(values,"F")}|')
    pending = sum(v['outcome'] == 'not-run' for v in rows)
    running = sum(v['outcome'] == 'running' for v in rows)
    failed = sum(v['outcome'] not in ['passed', 'not-run', 'running'] for v in rows)
    text = f'''固定 8 个并发 Sandbox、每个 4 vCPU / 12 GiB 名义内存，共享 L1 的 36 vCPU、7–12 GiB DRAM 与 64 GiB CXL 或 SSD，以下按实际冻结版本分别给出容量探索结果。

# 并发实验结果快照

截取时间：**{snapshot['as_of']}**。28 个计划点中，**{snapshot['completed_cohorts']} 组通过、{snapshot['complete_instances']} 个轨迹实例正确完成**；
{failed} 组资源失败，{running} 组运行中，{pending} 组未运行。
通过组均无 L1 OOM、8 个 Guest 确认全局配额，且观测到实际使用冷层；运行中不计成功。
不同版本不合并估计性能；单次探索不作为稳定性或显著性结论。

## 容量与完成时间

P=PhaseTieing；F=同介质均分对照。v4/v5 的 F 是不可变均分额度，v6/v7 的 F 共用动态安全预留。
时间为均值（3 次时附样本标准差）或单次测量。`batch_s` 从组释放到最后 lane.done，
包含等待、Guest 结果序列化/收尾，不含 VM 启动及屏障后的宿主归档传输。
`trace_makespan_s` 与真实工具 `service_sum_s` 另见 CSV，不能只用包含长 WAIT 的批次时间评价工具性能。

''' + '\n'.join(table) + '''

v6 SSD P/F 的 ceiling 分别为 1552/1488 MiB，存在 64 MiB 差异，不能将全部时间差归因于阶段策略。
v7 修复配对 ceiling：CXL 为 1456/1456 MiB，SSD 首个 P ceiling 为 512 MiB；F 的实测值以完成后回执为准。
ceiling 是受控 payload 的全组上限，不是 L1 DRAM 容量；宿主、VMM 和其他开销另外占用 DRAM。

## 12 GiB 三重复：资源节约与时间代价

以下为每策略三组指标的算术均值，内存公式见 [复现说明](../../docs/concurrent-reproduction.md)。

|冷层|平均 DRAM：P / F|DRAM 均值变化|采样峰值均值：P / F|DRAM·时间变化|批次时间变化|
|---|---|---:|---|---:|---:|
|CXL|5.309 / 6.030 GiB|−11.94%|10.397 / 8.541 GiB|−11.00%|+1.07%|
|SSD|5.654 / 6.490 GiB|−12.89%|9.988 / 8.560 GiB|−1.05%|+13.59%|

SSD 的 trace makespan 为 P 1056.2 / F 999.4 s（+5.68%），工具服务时长之和为
P 1952.6 / F 2017.1 s（−3.20%）。结果序列化仍在 workload cgroup 内，
P 的长收尾可能与 WAIT 小额度下的开销有关；这是待验证解释，不是已隔离验证的因果结论。
平均 DRAM 降低同时伴随更高峰值，不能把均值下降直接换算成可承载实例数提升。
完整重复值见 [资源摘要](v4-complete-resource-summary.json)。

## OOM、修复与解释边界

v5 的 8 GiB CXL-P 在释放约 208.6 秒后出现 L1 DRAM 节点 OOM，属于
`CONSTRAINT_MEMORY_POLICY / nodemask=0`，VMM 被杀；当时 DRAM 空闲约 25 MiB，
CXL 仍有约 26.2 GiB 空闲。这不是执行超时，也不是 8 条轨迹各自独立产生业务错误。
启动时固定预算没有覆盖运行时非 payload 增长、managed 与名义容量差异及无法即时回收的 slab。
见 [内核证据](oom-evidence.json) 与 [诊断](v5-failure-diagnosis.json)。

v6 引入动态 managed 容量/安全预留和真实页归还结算，同资源 8 GiB 四组均完成一次回归；
v7 再修复配对 ceiling 后探索 7 GiB。通过低 DRAM 点证明本输入组合可以执行，尚未找到最终容量边界。
两策略均通过的点不能证明 PhaseTieing 的承载量高于 F。

当前没有可报告的正式完整宿主口径超售倍数：分母按约定只算 DRAM，CXL/SSD 比值单列；
完整离线峰值画像的缓存和窗口口径仍待冻结。17.492 GiB 的 VMM RSS 峰值之和只是诊断需求代理。
此前 cgroup+swap 的重算候选见 [offline-host-profile-candidate.json](offline-host-profile-candidate.json)，
含大量文件缓存，不直接作为正式分子；导出时剔除了已否决的 CXL+DRAM 总内存分母比值，并保留源文件哈希。

嵌套 PEBS 不可用，冻结实验禁用采样；采样驱动 promote/exchange 尚未得到本轮实证覆盖。
同步恢复、更高 N、边界三重复、完整采样对照和正式吞吐/承载量曲线均尚待完成。
工具/轨迹/整组没有执行时间上限；基础设施保护和人工中断必须单列。

## 数据与复现

- [设置与执行步骤](../../docs/concurrent-reproduction.md)：L0/L1/L2、输入、构建、配对与计量规则。
- [matrix.csv](matrix.csv) / [matrix.json](matrix.json)：28 点的版本、资源、策略、状态、正确性、时长、额度和冷层回执。
- [instances.json](instances.json)：已完整实例的 E2E、工具服务时间、DRAM/冷层/交换等；不含仍运行实例的部分结果。
- `audits/`：逐组业务签名、资源审计、机制证据；`evidence_hashes` 指向原测量文件的 SHA256。
- `protocols/`、`source-manifests/`：原冻结协议、原机路径及源文件哈希；发布源码另见仓库 `release-manifest.json`。
- [snapshot.json](snapshot.json)：版本状态与截取时间，优先于旧报告中的静态待办描述。

Git 不包含系统/工作盘、私钥、预编译内核和原始长日志。公开 trace 可按固定提交获取并校验；
精确运行仍需外部冻结资产，现有缺口在复现说明中列出。发布摘要不是声称所有资产均可公开下载。
历史串行实验另见 [v67](../v67/)，不与当前并发容量数据混算。
'''
    (folder / 'README.md').write_text(text)


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('folder', type=Path)
    render(p.parse_args().folder)
