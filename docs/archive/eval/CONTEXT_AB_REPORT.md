---
layer: L3
doc_id: context-ab-report
title: 长程任务上下文/worker 成本 A/B（未采纳）
owner: 项目作者
status: snapshot
last_reviewed: 2026-10-25
review_cadence: 上下文或 worker 模型默认值变动时
warning: |
  这是 2 个 long 任务、2 次重复的 A/B，样本小、单任务方差大。
  结论是“不采纳该组合”，不是“所有上下文优化都无效”。
---

# 长程任务上下文/worker 成本 A/B

## 结论

**不采纳**当前的组合优化：

- token 总量只下降约 6%，且落在单任务波动范围内；
- `long/discount_spec` 从 2/2 变成 **0/2**，隐藏测试失败；
- 墙钟从 501s 变成 574s（+14.6%）。

默认配置保持不变。下一步若要继续，应**单独**验证 worker 模型（不带上下文截断），
并先补 provider 单价，否则只能比 token，不能比真实成本。

## 实验设置

| 臂 | 设置 |
|---|---|
| baseline | 当前默认：worker 继承主模型；repo_map 4000；worker 上下文 8000 |
| optimized | worker 用 `deepseek-flash`；repo_map 1500；worker 上下文 3000；单 worker 报告 2000；文件读取上限 200KB |

任务：`long/discount_spec`、`long/discount_pipeline`，各 2 次。
脚本：`scripts/ab_context.py`；原始数据：`tests/eval/ab_context_2026-10-25.json`。

## 结果

| 臂 | 通过 | main input | worker input | 合计 input | 墙钟 |
|---|---:|---:|---:|---:|---:|
| baseline | **4/4** | 3,005,959 | 544,958 | 3,550,917 | 501.0s |
| optimized | **2/4** | 2,708,572 | 629,441 | 3,338,013 | 574.0s |

差值：input **−6.0%**，墙钟 **+14.6%**，通过数 **−2**。

### 逐任务

| 任务 | 臂 | 通过 | main input | worker input |
|---|---|---|---:|---:|
| `long/discount_spec` | baseline | 2/2 | 991,569 / 572,410 | 145,809 / 129,666 |
| `long/discount_spec` | optimized | **0/2** | 569,493 / 835,036 | 187,609 / 172,454 |
| `long/discount_pipeline` | baseline | 2/2 | 760,720 / 681,260 | 115,175 / 154,308 |
| `long/discount_pipeline` | optimized | 2/2 | 732,418 / 571,625 | 133,639 / 135,739 |

## 失败分析

两次 `discount_spec` 失败都停在同一个隐藏测试：

```text
test_approval_reads_the_stored_discount
AssertionError: 4000 != 3600
```

`discount_spec` 的隐藏需求在审批接口：订单实际记住的折扣必须被审批端读到。
上下文截断（repo_map 1500 / worker 上下文 3000 / 报告 2000）很可能让主 agent
丢失了“审批层也要贯通”的信息。`discount_pipeline` 的可见需求更直接，所以未受影响。

## 为什么不能只看 token

- baseline 自身的两次差异是 991k vs 572k（−42%），远大于两臂的 −6%；
- 单次或少量重复不足以证明“省了 token”；
- 没有 provider 单价配置，`cost_usd` 恒为 0，无法比较 `deepseek-flash` 的
  真实美元成本。

因此这里的决定性证据是**通过率回归**，而不是 token 百分比。

## 复现

```bash
conda activate agent
AGENT_WSL_DISTRO=CodingAgent python scripts/ab_context.py \
  --tasks long/discount_spec,long/discount_pipeline --repeats 2 \
  --json tests/eval/ab_context.json
```

## 后续建议

1. 保持当前默认值；
2. 若继续降本，先做 **worker 模型单独 A/B**（不改上下文），并配置 provider 单价；
3. 上下文截断应逐项验证，不要一次改 repo_map、worker 上下文、报告上限和文件读取；
4. 任何优化都必须同时看通过率、token、墙钟，且重复次数至少 2。
