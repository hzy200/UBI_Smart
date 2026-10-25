---
layer: L3
doc_id: blind-suite-report
title: blind 套件评测报告（无可见测试）
owner: 项目作者
status: snapshot
last_reviewed: 2026-10-25
review_cadence: blind 套件任务或基线变动时
warning: |
  5 个任务、3 次重复；通过率已饱和，但工具调用与失败验证次数显著上升。
  不能用单次通过率判断 agent 的“自验证能力”，要看逐任务 effort。
---

# blind 套件评测报告

## 总览

| 指标 | 值 |
|---|---|
| 任务数 | 5（完全没有可见测试） |
| 模型 | `deepseek-chat` |
| 重复次数 | 3 |
| 逐轮通过率 | **5/5**、**5/5**、**5/5（100%）** |
| 波动 `spread` | 0.0 个百分点 |
| 翻转任务 | 无 |
| 三轮 token | 输入 2,746,981 / 输出 57,401 |
| 缓存命中率 | 6.9%（183,936 hit / 2,488,905 miss） |
| L3 审批 | 1 次 |
| 基线 | `tests/eval/baseline_blind.json` |
| comparable | **`true`**（commit `e1a2610`，工作区干净） |

## 任务与 effort

| 任务 | 三轮 | 工具调用 | 失败验证 | 耗时 |
|---|---|---|---:|---:|---:|
| `blind/configparser/duplicate_section` | 3/3 | 36 | **21** | 165.1s |
| `blind/statistics/mode_first` | 3/3 | 22 | **19** | 135.4s |
| `blind/fractions/round_half_even` | 3/3 | 23 | **15** | 133.0s |
| `blind/csv/restval_missing` | 3/3 | 21 | **17** | 118.6s |
| `blind/heapq/nsmallest_key` | 3/3 | 27 | **10** | 118.8s |

（工具调用与验证次数是三轮合计；单轮约 7–12 次工具、3–7 次失败验证。）

## 设计

- 完全没有可见测试；工作区只有源码与 `pytest.ini`；
- 提示词只给症状和可复现例子，不点名 API；
- 隐藏测试在判定时写入，参考解是标准库原文；
- 模型必须自己定位、自己验证；它写进工作区的临时测试会在判分前被删除。

## 结论

1. **盲测没有降低通过率。** 5 个任务、3 轮全部通过；模型能仅凭症状定位回归。
2. **但 effort 明显上升。** 每个任务单轮 7–12 次工具、3–7 次失败验证；
   `configparser` 三轮累计 21 次失败验证，说明验证/修复脊柱被大量使用。
3. **“信息更少”提高的是成本，不是失败率。** 对当前模型，减少提示仍然
   不足以让它做不出来；它会把成本转移到更多探索和验证上。
4. 要继续提升难度，必须换轴：跨服务/多进程、更长依赖链、需要在多个约束
   之间做设计取舍，而不是继续减少提示信息。

## 与 real_advanced 的对比

| 套件 | 任务形态 | 通过率 | 单任务 effort |
|---|---|---:|---|
| `real_advanced` | 可见测试全绿（spec） | 14/14 ×2 | 2 轮合计约 13–27 次工具 |
| `blind` | 无可见测试 | 5/5 ×3 | 3 轮合计 21–36 次工具、10–21 次失败验证 |

盲测的单轮工具调用约为 spec 任务的 2–3 倍；这是当前唯一稳定的“难度信号”。

## 复现

```bash
conda activate agent
python tests/eval/runner.py --suite blind --verify-seeds
python tests/eval/runner.py --suite blind --repeat 3
```

LangSmith 数据集：`coding-agent-blind`（5 条）。
