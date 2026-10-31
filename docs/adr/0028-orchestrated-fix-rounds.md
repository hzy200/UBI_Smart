# ADR-0028：verify 失败后的有界编排修复轮

- **Status**：accepted
- **Date**：2026-10-30
- **Deciders**：项目作者
- **Owner**：项目作者
- **Related**：[RFC-0008](../rfc/0008-orchestrated-fix-rounds.md)

## Context

orchestrator 只在 `START` 进入。它合并过 implement 补丁后，主图 verify
失败只能走主 agent 的 repair/replan，无法再次派发 implement worker。
`pending_verification` 在 verify 中被清零，失败出口也没有"是否来自编排"的信息。

顺序写波次（ADR-0027）已经提供 worktree 基线注入，使重入 implement 在技术上
可行：修复 worker 能看到第一轮已合并的改动。

## Decision

新增配置 `AGENT_ORCHESTRATOR_FIX_ROUNDS`（默认 `0` = 关闭）。开启后：

1. verify 在消费 `pending_verification` 时，如果结果是 `failed`，写
   `orchestrator_fix_eligible=True`；`error` 不算。
2. `route_after_verify` 在 failed 且 eligible、未取消、轮数未超限时进入
   `orchestrator_fix`，否则保持原有 repair/replan/respond。
3. `orchestrator_fix` 节点：
   - 递增 `orchestrator_fix_rounds_used`；
   - 用 verify 摘要 + 原始请求生成"只修失败项"的请求；
   - 清空上一轮任务图/状态，保留 `worker_results`（token 预算与审计）；
   - 强制 `workflow=orchestrated_implement`，跳过规则/模型选择。
4. `plan_tasks` 在 fix 模式下允许再次生成 implement（即使已有 merged 记录），
   任务 id 加 `fx{round}-` 前缀，避免与上一轮结果撞 id。
5. 修复合并后 `route_after_synthesize` 直接回到 verify，不重新规划整个请求。
6. 修复 worker 即使 stage=0 也走 worktree 基线注入。

保持不变量：write_scope、审批、快照、分量原子合并、取消语义不变。

## Consequences

### 正面

- 编排写任务失败后有一次结构化修复机会，而不是退回单 agent 重做；
- 轮数 + 递归上限双重约束，不会无限循环；
- 默认关闭，出问题可立即回退。

### 代价与风险

- 图新增一条 `verify → orchestrator_fix → orchestrator → verify` 的回边，
  任何拓扑改动都必须同步 `estimate_recursion_limit`；
- 修复轮保留 `worker_results` 以维持 token 预算，fan-in 会包含历史记录；
  任务 id 前缀用于避免"只看到旧结果"；
- 修复轮仍会重跑 survey，产生少量固定成本；
- 只做"有界重试"，不做失败签名聚类或跨轮学习。

## Alternatives

| 方案 | 优点 | 代价 | 结论 |
|---|---|---|---|
| 新增有界修复轮 | 复用 orchestrator | 新增回边与状态 | 选 |
| 在 repair 内直接调 worker | 不新增图边 | 调度逻辑污染 repair | 否 |
| 主 agent 自己修 | 零改动 | 失去 worker 并行/基线 | 现状 |
| 无上限重入 | 可能修好 | token 循环 | 否 |
