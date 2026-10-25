---
layer: L3
doc_id: real-advanced-report
title: real_advanced 套件评测报告（隐藏需求与多回归）
owner: 项目作者
status: snapshot
last_reviewed: 2026-10-25
review_cadence: real_advanced 任务或基线变动时
warning: |
  这是 6 个任务、2 次重复的快照；1 个任务翻转就是 16.7 个百分点，
  不能据此做统计推断。它只说明“改变了信息条件后出现了区分度”。
---

# real_advanced 套件评测报告

## 总览

| 指标 | 值 |
|---|---|
| 任务数 | 6（3 个隐藏需求 + 3 个多回归） |
| 模型 | `deepseek-chat` |
| 重复次数 | 2 |
| 逐轮通过率 | **5/6（83.3%）**、**6/6（100%）** |
| 平均通过率 | 91.7% |
| 波动 `spread` | **16.7 个百分点** |
| 翻转任务 | `real/spec/fractions/negative_denominator` |
| 两轮 token | 输入 1,171,191 / 输出 18,465 |
| 缓存命中率 | 8.7%（96,512 hit / 1,017,834 miss） |
| 基线 | `tests/eval/baseline_real_advanced.json` |
| comparable | **`true`**（commit `5c971d5`，工作区干净） |

## 任务与结果

| 任务 | 类别 | 两轮 | 工具调用 | 耗时 |
|---|---|---|---:|---:|
| `real/spec/statistics/median_even` | 隐藏需求 | 2/2 | 14 | 40.1s |
| `real/spec/fractions/negative_denominator` | 隐藏需求 | **1/2（flaky）** | 11 | 30.7s |
| `real/spec/heapq/reverse_merge` | 隐藏需求 | 2/2 | 21 | 48.9s |
| `real/multi/configparser/escaped_and_recursive` | 多回归 | 2/2 | 16 | 41.3s |
| `real/multi/argparse/type_and_default` | 多回归 | 2/2 | 24 | 67.5s |
| `real/multi/csv/restkey_and_restval` | 多回归 | 2/2 | 18 | 45.4s |

## 设计

- **隐藏需求**：可见测试全绿，隐藏测试才检查回归。模型不能靠失败测试
  定位，只能读提示词与真实模块。
- **多回归**：同一模块注入两处回归。可见测试只覆盖第一处；修好第一处后，
  隐藏测试仍会因第二处失败。
- 参考解始终是**未修改的标准库原文**；种子改动不超过 8 行。
- 判定仍为 stdlib unittest。

## 失败分析

`real/spec/fractions/negative_denominator` 第一轮失败、第二轮通过；
单独复跑也通过。失败不是任务不可满足：参考解稳定通过，提示词明确要求
负号归一到分子。它反映的是模型在大文件里改动的**不稳定性**，属于能力信号。

## 结论

1. **改变信息条件有效。** 6 个任务里出现了 16.7pp 的 spread 和 1 个翻转，
   这是 default/full/hard/real 五轮以来第一次出现非零区分度。
2. **多回归没有拉开差距。** 3 个多回归任务两轮全过；模型能找到第二处。
3. **隐藏需求更接近难点。** 唯一翻转出现在 spec 类；下一轮应增加 spec 类
   数量，而不是继续加多回归。
4. 样本仍小：6 个任务、2 次重复。要确认 16.7pp 不是单次噪声，至少
   `--repeat 3`，并把 spec 任务扩到 6–8 个。

## 复现

```bash
conda activate agent
python tests/eval/runner.py --suite real_advanced --verify-seeds
python tests/eval/runner.py --suite real_advanced --repeat 3
```

LangSmith 数据集：`coding-agent-real-advanced`（6 条）。
