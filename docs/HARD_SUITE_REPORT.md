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
| 逐轮通过率 | **13/14（92.9%）**、**13/14（92.9%）** |
| 波动 `spread` | 0.0 个百分点（本次样本） |
| 逐轮翻转 | 无 |
| 两轮 token | 输入 2,298,839 / 输出 42,243（合计 2,341,082） |
| 缓存命中率（审计统计） | 11.6%（hard 组 63 次 run） |
| 基线 | `tests/eval/baseline_hard.json` |
| comparable | `false`（工作区有未提交改动） |

## 分类

| 类别 | 任务数 | 通过 |
|---|---:|---:|
| 并发/竞态 | 4 | 4 |
| 需求未覆盖 | 3 | 3 |
| 跨文件/多层 | 3 | 2 |
| 排障 | 2 | 2 |
| 性能/批量 | 2 | 2 |

## 与现有套件的区别

| 套件 | 上次结果 | 区分度 |
|---|---:|---|
| `full` capability | 139/139 | 饱和 |
| `default` | 29/29 ×2 | 饱和 |
| `hard` | 13/14 ×2 | **仍有 1 个稳定难点** |

hard 套件的单轮 token 约 117 万，是 default 套件的量级之上、long 套件之下；
14 个任务可以在一轮内跑完，适合作为“改动后是否掉能力”的快速难度探针。

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

两轮基线都失败，但单独复跑一次通过。失败点是“低优先级不能覆盖高优先级”
与 `ordered()` 的排序规则；说明模型对这段规则**不稳定**，不是任务不可满足。
这条任务保留了，正是 hard 套件需要的区分度。

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
