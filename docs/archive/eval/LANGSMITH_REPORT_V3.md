---
layer: L3
doc_id: langsmith-report-v3
title: LangSmith 全功能评测报告（v3，158 条，修正后）
owner: 项目作者
status: snapshot
last_reviewed: 2026-10-25
review_cadence: 每次跑全量评测后
warning: |
  这是 L3 审批修复后的全量快照。它与 v2（157 条）的差异主要来自
  harness 修正与套件扩展，不能直接当作模型能力提升。
---

# LangSmith 全功能评测报告（v3）

## 总览

| 指标 | 值 |
|---|---|
| 数据集 | `coding-agent-full-v3`（158 条） |
| 实验 | `coding-agent-full-v3-fixed-7a6830ab` |
| 模型 | `deepseek-chat` |
| 通过 | **158 / 158 = 100%** |
| 首次通过 | **158 / 158**（无重跑） |
| 实验墙钟 | 31m37s（并发 2） |
| 任务耗时合计 | 3,781.1s |
| 基线 | `9a6995b`（含 L3 审批修复） |

## 用例构成

| 类型 | 数量 | 通过 |
|---|---:|---:|
| capability | 139 | 139 |
| agent | 2 | 2 |
| mechanism | 17 | 17 |

### capability 按档位

| 档位 | 通过 / 总数 |
|---|---:|
| basic | 110 / 110 |
| deep | 17 / 17 |
| long | 12 / 12 |

### capability 按类别

| 类别 | 通过 / 总数 |
|---|---:|
| single_file | 63 / 63 |
| cross_file | 35 / 35 |
| debug | 29 / 29 |
| spec | 12 / 12 |

## 智能体级路径

| case | 工作流 | worker / 子智能体 | 合并 |
|---|---|---|---|
| `agent/orchestrated_implement` | `orchestrated_implement` | 4 worker | 2/2 |
| `agent/subagent_explore` | direct + delegate | 子智能体 2，最大同批 2 | 不适用 |

只读编排任务（`long/discount_*`、`long/audit_trail`、`long/parallel_modules`）
共 6 个，worker 数 4–5，均完成判定。

## Token 与缓存

| 项 | tokens |
|---|---:|
| 主 agent input | 14,686,333 |
| 主 agent output | 217,963 |
| worker input | 717,080 |
| worker output | 74,989 |
| **合计** | **15,696,365** |
| 缓存 hit / miss | 3,165,274 / 11,750,324 |
| 缓存命中率 | **21.2%** |

主 agent input：p50 = 68,110，p95 = 150,967。

## 耗时 / Token Top 8

| 任务 | 耗时 | 主 token | worker token |
|---|---:|---:|---:|
| `long/discount_spec` | 178s | 1,241,064 | 133,527 |
| `long/discount_pipeline` | 121s | 762,946 | 130,840 |
| `long/discount_and_audit` | 120s | 761,902 | 128,945 |
| `long/discount_partial` | 111s | 441,349 | 133,978 |
| `agent/subagent_explore` | 108s | 687,595 | 0 |
| `long/audit_trail` | 79s | 280,041 | 100,601 |
| `long/parallel_modules` | 68s | 200,873 | 46,442 |
| `agent/orchestrated_implement` | 47s | — | — |

## 与 v2（157 条）对比

| 指标 | v2 | v3（修正后） |
|---|---:|---:|
| 通过率 | 157/157 | **158/158** |
| 首次通过 | 156/157（1 次重跑） | **158/158** |
| 总 token | 15,552,129 | 15,696,365 |
| 缓存命中率 | 18.75%（审计估算） | **21.2%** |
| 墙钟 | 30m08s | 31m37s |

v3 多了 `agent/delegate_wiring` 机制探针，capability 集合不变；token 与耗时
基本持平。**通过率仍为 100%，说明这套全量能力题对当前模型没有区分度。**

## 结论与限制

1. **全量套件是覆盖/回归基线，不是能力区分器。** 158/158 已饱和；后续看
   `hard`、`real`、`real_advanced` 的逐任务表现。
2. **L3 审批修复没有改变本套件的结果。** 本套件 0 次审批请求；受影响的是
   `real_advanced`（修复前被误判为“提前收手”）。修复已使评测更公平。
3. **成本仍集中在 long 档。** `long/discount_spec` 单任务 1.24M 主 token，
   是 p50 的 18 倍；成本优化应继续围绕长程任务。
4. **子智能体与写编排都被真实触发。** 子智能体 2 个/最大同批 2；写编排
   4 worker、2 次合并。

## 复现

```bash
conda activate agent
python tests/eval/langsmith_suite.py --run --kinds all \
  --dataset coding-agent-full-v3 --case-retries 1 --concurrency 2
```
