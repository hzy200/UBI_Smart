---
layer: L3
doc_id: real-advanced-report
title: real_advanced 套件评测报告（隐藏需求与多回归）
owner: 项目作者
status: snapshot
last_reviewed: 2026-10-25
review_cadence: real_advanced 任务或基线变动时
warning: |
  这是 14 个任务、2 次重复的快照，另对两个翻转任务做了 5 次重复。
  总体通过率相同不代表结论相同：本轮的翻转成员与上一轮不同。
---

# real_advanced 套件评测报告

## 总览

| 指标 | 值 |
|---|---|
| 任务数 | 14（11 个隐藏需求 + 3 个多回归） |
| 模型 | `deepseek-chat` |
| 全量重复 | 2 |
| 逐轮通过率 | **13/14（92.9%）**、**13/14（92.9%）** |
| 平均通过率 | 92.9% |
| `spread` | 0.0 个百分点（两轮总数相同） |
| 翻转任务 | `real/spec/fractions/limit_denominator`、`real/spec/shlex/escaped_space` |
| 两轮 token | 输入 3,409,843 / 输出 51,813 |
| 缓存命中率 | 10.6%（346,368 hit / 2,909,257 miss） |
| 基线 | `tests/eval/baseline_real_advanced.json` |
| comparable | **`true`**（commit `61cda1d`，工作区干净） |

> **关键：通过率相同 ≠ 结果相同。** 两轮都是 13/14，但失败成员不同：
> 第 1 轮是 `fractions/limit_denominator`，第 2 轮是 `shlex/escaped_space`。
> 只看 pass_rate 会得出“没有变化”的错误结论。

## 任务与结果

| 任务 | 类别 | 两轮 | 工具调用 | 耗时 |
|---|---|---|---:|---:|
| `real/spec/statistics/median_even` | 隐藏需求 | 2/2 | 14 | 40.1s |
| `real/spec/fractions/negative_denominator` | 隐藏需求 | 2/2 | 16 | 47.9s |
| `real/spec/heapq/reverse_merge` | 隐藏需求 | 2/2 | 20 | 49.6s |
| `real/spec/textwrap/replace_whitespace` | 隐藏需求 | 2/2 | 20 | 47.1s |
| `real/spec/argparse/allow_abbrev` | 隐藏需求 | 2/2 | 26 | 61.7s |
| `real/spec/heapq/nlargest_key` | 隐藏需求 | 2/2 | 20 | 41.0s |
| `real/spec/statistics/variance_xbar` | 隐藏需求 | 2/2 | 21 | 58.0s |
| `real/spec/fractions/limit_denominator` | 隐藏需求 | **1/2（flaky）** | 22 | 45.2s |
| `real/spec/argparse/dest_hyphen` | 隐藏需求 | 2/2 | 27 | 52.7s |
| `real/spec/shlex/escaped_space` | 隐藏需求 | **1/2（flaky）** | 15 | 35.8s |
| `real/spec/csv/extrasaction` | 隐藏需求 | 2/2 | 17 | 41.5s |
| `real/multi/configparser/escaped_and_recursive` | 多回归 | 2/2 | 18 | 56.3s |
| `real/multi/argparse/type_and_default` | 多回归 | 2/2 | 19 | 54.7s |
| `real/multi/csv/restkey_and_restval` | 多回归 | 2/2 | 18 | 39.7s |

## 翻转任务 repeat 5

| 任务 | 通过 | 失败形态 |
|---|---:|---|
| `real/spec/fractions/limit_denominator` | **3/5（60%）** | 两次都是 1 步、5–16s、未触发验证 |
| `real/spec/shlex/escaped_space` | **4/5（80%）** | 一次 1 步、11.2s、未触发验证 |

失败不是“改错代码”，而是**提前收手**：没有写文件、没有跑测试，
直接给出回答。可见测试全绿时，模型有时会把“没有失败”当成“没有任务”。

## 提示词变化

本轮把 spec 提示词改为**症状式**，不再点名具体 API：

- 只给现象（“偶数样本的中位数偏大”“负号跑到分母上”“降序合并没有保持降序”），
  不写 `median`、`Fraction`、`merge` 等函数名；
- 保留可复现的输入/输出例子，避免任务变成猜谜；
- 多回归任务的提示词保持显式，因为它只作为覆盖，不作为难度来源。

## 结论

1. **信息更少确实更难。** 新增的 `variance_xbar`、`limit_denominator`、
   `dest_hyphen`、`escaped_space`、`extrasaction` 中，两个在 5 次重复里
   出现失败；失败形态集中在“只读不写/提前收手”。
2. **聚合通过率会掩盖成员翻转。** 两轮 13/14、spread 0，但翻转任务有 2 个。
   后续报告必须同时看 `flipped` 与逐任务 repeat，而不是只看总通过率。
3. **多回归仍无区分度。** 3 个任务 2/2 全过；保留作覆盖即可。
4. **spec 任务的平均通过率仍高（约 93%）**，但单任务稳定性已经降到
   60–80%，足以用来观察改动是否影响“是否愿意调查”。

## 后续建议

- 继续扩 spec 到 14–16 个，并优先保留“可见测试全绿”的形态；
- 对翻转任务用 `--repeat 5` 作为稳定性门槛，而不是单次 pass/fail；
- 单独分析“1 步、未验证、无写入”的失败：这更像 planner/act 的提前收手，
  而不是代码理解失败；
- 多回归保留作覆盖，不再投入难度设计。

## 复现

```bash
conda activate agent
python tests/eval/runner.py --suite real_advanced --verify-seeds
python tests/eval/runner.py --suite real_advanced --repeat 2
python tests/eval/runner.py --suite real_advanced \
  --only real/spec/fractions/limit_denominator --repeat 5 --force
```

LangSmith 数据集：`coding-agent-real-advanced`（14 条）。
