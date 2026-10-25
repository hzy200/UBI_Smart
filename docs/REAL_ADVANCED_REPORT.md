---
layer: L3
doc_id: real-advanced-report
title: real_advanced 套件评测报告（隐藏需求与多回归）
owner: 项目作者
status: snapshot
last_reviewed: 2026-10-25
review_cadence: real_advanced 任务或基线变动时
warning: |
  这是 9 个任务、3 次重复的快照；1 个任务翻转就是 11.1 个百分点。
  它说明“改变了信息条件后出现了区分度”，但不构成统计推断。
---

# real_advanced 套件评测报告

## 总览

| 指标 | 值 |
|---|---|
| 任务数 | 9（6 个隐藏需求 + 3 个多回归） |
| 模型 | `deepseek-chat` |
| 重复次数 | 3 |
| 逐轮通过率 | **9/9（100%）**、**9/9（100%）**、**8/9（88.9%）** |
| 平均通过率 | 96.3% |
| 波动 `spread` | **11.1 个百分点** |
| 翻转任务 | `real/spec/argparse/allow_abbrev` |
| 三轮 token | 输入 3,453,470 / 输出 55,997 |
| 缓存命中率 | 14.7%（486,784 hit / 2,837,044 miss） |
| 基线 | `tests/eval/baseline_real_advanced.json` |
| comparable | **`true`**（commit `ca14763`，工作区干净） |

## 任务与结果

| 任务 | 类别 | 三轮 | 工具调用 | 耗时 |
|---|---|---|---:|---:|
| `real/spec/statistics/median_even` | 隐藏需求 | 3/3 | 24 | 70.4s |
| `real/spec/fractions/negative_denominator` | 隐藏需求 | 3/3 | 30 | 89.3s |
| `real/spec/heapq/reverse_merge` | 隐藏需求 | 3/3 | 29 | 74.8s |
| `real/spec/textwrap/replace_whitespace` | 隐藏需求 | 3/3 | 29 | 108.8s |
| `real/spec/argparse/allow_abbrev` | 隐藏需求 | **2/3（flaky）** | 35 | 87.5s |
| `real/spec/heapq/nlargest_key` | 隐藏需求 | 3/3 | 31 | 100.7s |
| `real/multi/configparser/escaped_and_recursive` | 多回归 | 3/3 | 26 | 81.9s |
| `real/multi/argparse/type_and_default` | 多回归 | 3/3 | 49 | 140.5s |
| `real/multi/csv/restkey_and_restval` | 多回归 | 3/3 | 27 | 59.3s |

## 设计

- **隐藏需求**：可见测试全绿，隐藏测试才检查回归。模型不能靠失败测试
  定位，只能读提示词与真实模块。
- **多回归**：同一模块注入两处回归。可见测试只覆盖第一处；修好第一处后，
  隐藏测试仍会因第二处失败。
- 参考解始终是**未修改的标准库原文**；种子改动不超过 8 行。
- 判定仍为 stdlib unittest。

## 失败分析

`real/spec/argparse/allow_abbrev` 第三轮失败（1 步、7 次工具、8.3s、
未触发验证），前两轮通过，单独复跑也通过。失败不是任务不可满足：
参考解稳定通过，提示词明确要求长选项缩写。它反映的是模型在大文件里
**提前收手/没有真正改动**的不稳定性，属于能力信号。

上一版 6 任务快照里翻转的是 `real/spec/fractions/negative_denominator`；
这次它 3/3 通过。两次翻转都出现在 spec 类，说明不稳定的是“隐藏需求”
这一轴，而不是某一个固定任务。

## 结论

1. **隐藏需求轴出现稳定信号。** 9 任务 × 3 轮的 `spread` 为 11.1pp，
   两次不同的 spec 任务在跨轮次中翻转。
2. **多回归仍无区分度。** 3 个多回归任务 3/3 全过，9 次运行无一失败；
   模型能找到第二处回归。
3. **平均通过率 96.3%，仍偏高。** 9 个任务里只有 1 个在 1 轮失败；
   对当前模型来说，这套尺子只能分辨“明显不稳定”的情况。
4. 样本仍小：1 个任务翻转 = 11.1pp。要确认信号，继续增加 spec 任务
   数量，而不是提高单任务复杂度。

## 后续建议

- 把 spec 任务扩到 10–12 个，优先“只给症状、不指出具体 API”的版本；
- `--repeat 5` 观察同一任务的翻转率，而不是只看一轮 pass/fail；
- 多回归保留作覆盖，不再作为难度来源；
- 对翻转任务单独做稳定性分析（模型是否经常只读不写、提前收手）。

## 复现

```bash
conda activate agent
python tests/eval/runner.py --suite real_advanced --verify-seeds
python tests/eval/runner.py --suite real_advanced --repeat 3
```

LangSmith 数据集：`coding-agent-real-advanced`（9 条）。
