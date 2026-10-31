# ADR-0026：implement 补丁按连通分量增量合并

- **Status**：accepted
- **Date**：2026-10-30
- **Deciders**：项目作者
- **Owner**：项目作者
- **Related**：[RFC-0006](../rfc/0006-incremental-implement-merge.md)

## Context

并发 implement worker 在各自 worktree 中产出补丁，宿主在主工作区顺序合并。
原实现只在**没有任何 implement 在飞**时 flush 合并队列（`scheduler_run.py`）。
一个长跑 worker 会让同批已完成补丁停在 `merging`，并连带阻塞 `review`/`test`
依赖方。

`merge_implement_patches_atomic` 已经按共享文件把补丁分成连通分量，每个分量
独立留快照、顺序 apply、失败只回滚本分量。因此"整批等待"是调度层的保守选择，
不是合并层的能力限制。

## Decision

新增开关 `AGENT_ORCHESTRATOR_INCREMENTAL_MERGE`（默认 `false`）。开启后：

1. 已完成 implement 的补丁路径在 flush 前预解析并缓存；
2. pending 记录先按共享文件分组为连通分量；
3. 若某个分量的全部路径与**所有在飞 implement** 的 write_scope/已知路径都不
   冲突，则该分量立即 flush；
4. 任一 pending 记录拿不到真实路径 → 本轮回退整批 flush；
5. `allow_overlap=true` 时永不增量（3-way 需要共享文件同时可见）；
6. 收尾仍强制 flush 全部；取消/deadline 仍整批丢弃。

保持不变式：

- 同一连通分量仍是一个事务；
- 合并前留快照，失败只回滚本分量；
- 每次 flush 前写 merge journal；
- 不新增写权限、不移动 HEAD、不改变审批与路径守卫。

## Consequences

### 正面

- 慢 worker 不再阻塞不相关补丁及其下游；
- 复用现成的分量原子性与快照语义，不需要重写合并算法；
- 默认关闭，出问题可立即回退到旧行为。

### 代价与风险

- 每条补丁可能多一次 `git apply --numstat` 预解析（WSL 进程启动开销）；
- 路径未知时回退整批，增量收益在该场景消失；
- flush 次数变多，审计/日志噪声增加；
- 主工作区变脏的时间点提前，但此时编排尚未交给主 agent，不影响审批边界。

## Alternatives

| 方案 | 优点 | 代价 | 结论 |
|---|---|---|---|
| 维持整批等待 | 最简单 | 队头阻塞 | 否 |
| 每条补丁完成即合并 | 延迟最低 | 共享文件需 3-way，语义变化大 | 否 |
| 后台合并线程 | 调度主循环干净 | 生命周期/恢复/并发读写复杂 | 否 |

## Rollout

1. 开关关闭时行为与旧实现等价；
2. 灰度：长任务集对比合并等待时间、下游启动时间、合并失败率；
3. 无退化后再考虑默认开启。
