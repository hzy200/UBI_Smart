---
layer: L3
doc_id: langsmith-report-v2
title: LangSmith 全功能评测报告（v2，157 条）
owner: 项目作者
status: snapshot
last_reviewed: 2026-10-25
review_cadence: 每次跑全量评测后
warning: |
  这是**一次性快照**，不是持续更新的事实源。新结果请另起一份，不要覆盖 ——
  历史数字是对比基线。评测方法论见 ../tests/eval/README.md。
---

# LangSmith 全功能评测报告（v2）

## 总览

| 指标 | 值 |
|---|---|
| 数据集 | `coding-agent-full-v2`（157 条） |
| 实验 | `coding-agent-full-v2-a02ae03e` |
| 模型 | `deepseek-chat` |
| 通过 | **157 / 157 = 100%** |
| 实验墙钟 | 30m08s（并发 2） |
| 任务 agent 耗时合计 | 3,508.6s |
| 失败 | 0 |
| 重跑后通过 | 1（`full/validation/username`，第 2 次通过） |

> **勘误（2026-10-25）**：本快照中只读编排（explore/review）worker 的
> `worker_tool_calls` 被记为 0，原因是
> `src/coding_agent/orchestrator/workers.py` 组装 `WorkerResult` 时漏传
> `artifact.tool_calls`。这是**指标漏记，不是 worker 没有调用工具**。
> 后续运行已修复；本快照里的只读 worker 工具调用数应视为“缺失”，
> 其余 token / 耗时 / 状态 / 合并数字不受影响。
>
> **后续（2026-10-25）**：`agent/parallel_readonly` 与 capability 的
> `long/discount_and_audit` 重复，已删除；只读编排的断言改为在 capability
> 结果上检查 workflow 与 worker 数；随后新增 `agent/subagent_explore` 与
> `agent/delegate_wiring`。当前套件为 **158 条**
> （139 capability + 2 agent + 17 mechanism），数据集 `coding-agent-full-v3`。
> 本报告保留 157 条的历史快照。
>
> 另设 `coding-agent-hard`（14 条）用于难度套件，默认不并入全量；
> 结果见 [HARD_SUITE_REPORT.md](HARD_SUITE_REPORT.md)。

## 子智能体覆盖（2026-10-25，Round 3）

历史快照里 `subagents = 0`。新增两条覆盖：

- `agent/delegate_wiring`（确定性探针）：fake runner 接管 `delegate_explore`，
  断言委派计数、摘要回填、`tool_calls` 透传；不调用模型。
- `agent/subagent_explore`（真实模型）：`long/discount_pipeline`，
  关闭编排、`delegate_min_files=0`，提示词要求同时派 2 个子智能体。

两次独立实跑：

| 运行 | 判定 | 子智能体 | 最大同批 | 状态 | 子智能体 token | 主 token | 耗时 |
|---|---|---|---:|---|---|---:|---:|
| 1 | 通过 | 2 | 2 | success 1 / partial 1 | 84,081 / 8,819 | 688,942 / 16,370 | 110.6s |
| 2 | 通过 | 2 | 2 | partial 2 | 108,861 / 8,171 | 656,067 / 15,341 | 105.4s |

两次都首次通过、都在同一轮同时派出 2 个子智能体，说明委派路径与并发批次
可被稳定触发。成本约 79 万 token/次，全量套件会增加一个长程用例的成本。

## 用例构成

| 类型 | 数量 | 通过 |
|---|---:|---:|
| `capability` | 139 | 139 |
| `agent` | 2 | 2 |
| `mechanism` | 16 | 16 |

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

## 智能体级用例

| case | 任务 | 工作流 | 任务图 / worker | 合并 | 耗时 | 判定 |
|---|---|---|---:|---:|---:|---|
| `agent/orchestrated_implement` | `long/parallel_modules` | `orchestrated_implement` | 3 / 3（最大同批 2；implement×2 + test×1） | 2/2，冲突 0 | 20.3s | 通过 |
| `agent/parallel_readonly` | `long/discount_and_audit` | `parallel_readonly` | 4 / 5（最大同批 4；explore×2 + test×2 + review×1） | 不适用 | 131.2s | 通过 |

写编排用例覆盖了：规则选择 → 任务图 → git worktree 隔离 → implement worker →
补丁合并 → 判定。只读并行用例覆盖了 explore/test/review worker 的并发与失败重试。

## 机制探针（16/16）

```text
security/secret_redaction      security/write_policy
security/allow_rules           security/unregistered_tool
security/path_guard            audit/hash_chain
workspace/archive_safety       protocol/hmac
steering/persistence           router/cost_and_capability
codeintel/import_graph         billing/config
shell/policy                   lsp/framing
shell/persistent               cost/run_usage
```

## 机制指标（capability + agent）

| 指标 | 值 |
|---|---|
| 验证 | 156 ok / 62 failed（137 个任务触发过） |
| 修复 | 43 次 |
| 重规划 | 45 次 |
| 使用 worker 的任务 | 8 |
| worker 总数 | 36 |
| implement 合并 | 2 |
| 子智能体 | 0 |
| 工作流（capability） | direct 133 / parallel_readonly 6 |

## Token 成本

| 项 | tokens |
|---|---:|
| 主 agent input | 14,313,231 |
| 主 agent output | 212,372 |
| worker input | 941,096 |
| worker output | 85,430 |
| **合计** | **15,552,129** |

未配置 provider 单价，本报告只统计 token，不计算美元成本。

## 耗时 Top 10

| 任务 | 耗时 |
|---|---:|
| `long/discount_spec` | 149.9s |
| `agent/parallel_readonly` | 131.2s |
| `long/discount_and_audit` | 119.5s |
| `long/discount_pipeline` | 111.9s |
| `long/audit_trail` | 101.9s |
| `long/discount_partial` | 90.9s |
| `long/parallel_modules` | 51.2s |
| `deep/field_propagation` | 38.4s |
| `full/debug/import_cycle` | 33.1s |
| `deep/config_precedence` | 33.1s |

## 覆盖矩阵

| 能力 | 覆盖方式 |
|---|---|
| 单文件 / 跨文件 / 排障 / spec | 139 个 capability 任务 |
| 自动验证、修复、重规划 | 验证 156 ok、修复 43、重规划 45 |
| 只读编排（explore/test/review） | 6 个 capability 长程任务 + `agent/parallel_readonly` |
| 写编排（worktree + 合并） | `agent/orchestrated_implement` |
| Git 交付 | 评测工作区初始化 git；长程任务含 `git_add` / `git_commit` |
| 安全策略（脱敏/写保护/allow rules/路径守卫/未登记工具） | mechanism 探针 |
| 审计哈希链 | `audit/hash_chain` |
| 归档安全（zip-slip） | `workspace/archive_safety` |
| 内部 HMAC | `protocol/hmac` |
| 路由 / 计费 / 用量 / 常驻 shell / LSP 分帧 | mechanism 探针 |

## 已知缺口

- **子智能体委派（delegate_explore）本轮 0 次**。能力任务由主 agent 或编排 worker
  完成，模型没有主动委派；需要单独的、明确要求并行探索的任务才能稳定触发。
- **人工审批暂停/恢复、快照回滚、checkpoint 续跑**不在本套件中：评测 harness
  使用 `approval_mode="approve"` 自动放行，避免无人值守时挂起。这三类由
  `scripts/phase_g_e2e.py` 在 A3+A4 全链路上真实验收。
- **A2/A3 服务工作区导入/上传/回滚**属于系统级 E2E，由 Phase G 脚本覆盖，
  LangSmith 套件只测智能体执行面。
- 100% 说明当前模型在这把尺子上已饱和；后续要恢复区分度，需要更难的任务，
  单次运行或小幅差异不能作为能力变化的证据。
