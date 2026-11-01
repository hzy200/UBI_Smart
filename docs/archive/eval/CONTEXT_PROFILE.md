---
layer: L3
doc_id: context-profile
title: 上下文成本画像（采样）
owner: 项目作者
status: snapshot
last_reviewed: 2026-10-25
review_cadence: 上下文注入/裁剪策略变动时
warning: |
  这是两个任务的采样，不是统计结论。它只回答“token 大概花在哪”，
  不能用来证明某次优化的收益；优化必须用固定任务集 A/B。
---

# 上下文成本画像（采样）

## 新增指标

`tests/eval/harness.py::context_profile_of` 从最终 checkpoint 提取：

| 字段 | 含义 |
|---|---|
| `repo_map_chars` | 注入任务图的 repo map 字符数 |
| `repo_summary_chars` | survey 写入的仓库画像总字符数 |
| `project_instructions_chars` | 项目指令（AGENTS.md 等）字符数 |
| `session_brief_chars` | 上一轮会话约束摘要字符数 |
| `worker_context_chars` | 交给主 agent 的 worker 压缩汇总 |
| `worker_summary_chars` / `worker_findings_chars` | worker 结果原文规模 |
| `planned_tasks` | 任务图规模 |
| `context_trim` | 最近一次 act/respond 的裁剪报告（字符/估算 token） |

这些字段进入 `TaskResult.context_profile`、baseline 逐任务行与 LangSmith
`metrics`；`--repeat k` 时取**裁剪前 token 最大**的那一轮，代表最坏情况。

## 采样 1：hard 小任务

`hard/concurrency/token_bucket`，通过，主 token 62,245 / 877：

```text
repo_map_chars            0
repo_summary_chars        2
context_trim.before_tokens  2,441
context_trim.trimmed        0
```

小任务不走编排，没有 repo map / worker 上下文；单次上下文约 2.4k token。

## 采样 2：long 任务

`long/discount_pipeline`，通过，`parallel_readonly`，5 worker：

```text
主 token                637,232 in / 6,633 out
worker token            123,250 in / 12,726 out
repo_map_chars          2,369
repo_summary_chars      2,632
worker_context_chars    6,989
worker_summary_chars    12,057
worker_findings_chars   1,280
planned_tasks           4
context_trim            52,317 chars → 42,807 chars
                        (16,195 → 13,908 tokens, trimmed 13)
```

## 缓存命中率

评测现在从 `CostReport` 采集 provider 前缀缓存命中/未命中，并写入
baseline 与 LangSmith。下面是从 2026-10-25 审计日志统计的历史数据：

| 组 | run 数 | 命中率 |
|---|---:|---:|
| full / 其他 | 165 | 15.5% |
| long | 18 | **28.1%** |
| hard | 63 | 11.6% |
| 合计 | 246 | 18.75% |

long 任务的命中率明显更高，说明重复上下文已经在被 provider 缓存。
单次 `hard/concurrency/token_bucket` 复跑：命中 4,992 / 未命中 59,637（7.7%）。

干净工作区重跑的 hard 基线（commit `f062a78`）：
**18.9%**（431,488 hit / 1,850,141 miss，28 次 run）。history 的 11.6%
包含任务定义缺陷与失败重跑，不能直接当作当前套件的命中率。

## 结论

1. **单次上下文不是主要成本。** 最后一次 respond 的上下文约 16k token，
   而主 agent 总共消耗了 637k input —— 成本主要来自**多轮重复携带上下文**，
   不是某一项注入过大。
2. **repo_map 截断的收益有限。** 这个 38 文件仓库的 repo_map 只有 2.4k 字符，
   即使砍半也只影响每轮约 1k token，而轮次有 7–8 轮。
3. **worker 成本约占 16%。** 主 agent 637k vs worker 123k；只读 worker 的
   摘要约 12k 字符，不是主要来源。
4. 最值得验证的优化顺序：
   - **prompt caching / 稳定前缀复用**（直接攻击重复上下文）；
   - 减少 LLM 轮次（提前综合、减少无效探索）；
   - worker 使用更便宜模型（约占 16%，收益上限有限）；
   - repo_map / worker 摘要截断（收益最小，最后做）。

## 下一步 A/B 建议

> 已经做过一轮组合 A/B，结论是**不采纳**：token −6%，但
> `long/discount_spec` 从 2/2 变成 0/2，墙钟 +14.6%。详见
> [CONTEXT_AB_REPORT.md](CONTEXT_AB_REPORT.md)。

固定 6 个 long + 6 个 hard 任务，两臂各 2 次：

- 通过率必须不下降；
- 比较主/worker/total token 与墙钟；
- 任何小于 `spread` 的差异不能当作收益。

本轮只加指标，不改行为。
