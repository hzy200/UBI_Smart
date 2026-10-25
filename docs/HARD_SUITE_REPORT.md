---
layer: L3
doc_id: hard-suite-report
title: hard 套件评测报告（14 个难度任务）
owner: 项目作者
status: snapshot
last_reviewed: 2026-10-25
review_cadence: hard 套件任务或基线变动时
warning: |
  这是一次本机实跑的快照。hard 套件用于解决 default/full 已饱和的问题，
  不替代它们的历史基线。方法论见 ../tests/eval/README.md。
---

# hard 套件评测报告

## 总览

| 指标 | 值 |
|---|---|
| 任务数 | 14 |
| 模型 | `deepseek-chat` |
| 重复次数 | 2 |
| 逐轮通过率 | **14/14（100%）**、**14/14（100%）** |
| 波动 `spread` | 0.0 个百分点（本次样本） |
| 逐轮翻转 | 无 |
| 两轮 token | 输入 2,340,274 / 输出 41,554（合计 2,381,828） |
| 缓存命中率 | **18.9%**（431,488 hit / 1,850,141 miss） |
| 基线 | `tests/eval/baseline_hard.json` |
| comparable | **`true`**（commit `f062a78`，工作区干净） |

## 分类

| 类别 | 任务数 | 通过 |
|---|---:|---:|
| 并发/竞态 | 4 | 4 |
| 需求未覆盖 | 3 | 3 |
| 跨文件/多层 | 3 | 3 |
| 排障 | 2 | 2 |
| 性能/批量 | 2 | 2 |

## 与现有套件的区别

| 套件 | 上次结果 | 区分度 |
|---|---:|---|
| `full` capability | 139/139 | 饱和 |
| `default` | 29/29 ×2 | 饱和 |
| `hard` | **14/14 ×2** | **也已饱和** |

hard 套件的单轮 token 约 117 万，是 default 套件的量级之上、long 套件之下；
14 个任务可以在一轮内跑完，适合作为“改动后是否掉能力”的快速探针，
但**当前模型已经能全部做对**，无法再区分改动优劣。

> **重要更正**：上一版报告的 13/14 不是稳定的能力缺口。那两次失败主要来自
> 任务定义缺陷（负容量未写进提示词、未声明的 API 名）和 `plugin_priority`
> 的偶发实现错误。修正定义后，在干净工作区重跑两轮均为 14/14。
> 结论是：**hard 套件也需要继续加难度**，而不是“已有 1 个稳定难点”。

## 失败与修正记录

### `hard/performance/lru_cache`

首次两轮都失败，原因是隐藏测试检查 `LRUCache(-1)` 抛 `ValueError`，但任务
提示词没有写“负容量”。这是**任务定义缺陷**，不是模型能力问题。已把该要求
补进提示词，之后单任务复跑通过。

### `hard/concurrency/exactly_once_registry`

测试原本断言内部字段 `registry._loads == 1`。这会惩罚“实现正确但没有保留
该字段”的解法。已改为在测试里统计 factory 调用次数，只断言外部可观察行为。

### `hard/multilayer/currency_flow`

隐藏测试导入 `report.revenue_by_currency`，但提示词最初只写“按币种汇总”。
已把函数名与返回值形状写进提示词，避免未声明的 API 约定。

### `hard/multilayer/plugin_priority`

历史上有过失败（“低优先级不能覆盖高优先级”与 `ordered()` 排序），但单独
复跑通过，最终干净基线的两轮也都通过。它是**偶发实现错误**，不是稳定难点；
不能把上一版的 13/14 读成稳定能力缺口。

## 任务设计约束

- 判定仍用 stdlib `unittest`，不引入第三方依赖；
- 每个任务都有参考解，`--verify-seeds` 验证种子失败、参考解通过；
- 隐藏测试只检查提示词明确写出的行为，避免“猜 API 名”式假失败；
- 并发测试用 `threading.Barrier` / `asyncio.sleep` 固定交错窗口，不依赖随机；
- 性能测试用注入的 `Repo.calls` 计数，不依赖真实耗时。

## 复现

```bash
conda activate agent
python tests/eval/runner.py --suite hard --verify-seeds
python tests/eval/runner.py --suite hard --repeat 2
```

LangSmith 数据集：`coding-agent-hard`（14 条）。
