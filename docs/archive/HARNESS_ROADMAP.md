---
layer: L3
doc_id: harness-roadmap
title: Harness 路线图与阶段验收（归档）
owner: 项目作者
status: snapshot
last_reviewed: 2026-11-01
review_cadence: 只读历史快照
---

# Harness 路线图与阶段验收（归档）

> 从 `docs/HARNESS.md` 移出的历史路线图与 3A 验收清单；当前实现见
> [../HARNESS.md](../HARNESS.md)。

## 14. 路线图

### Phase 3A：动态工作流选择 + 最小只读 worker（已实现）

**目标**：证明「根据提示词和仓库内容选择工作流 → 并发只读 worker → 汇总 → 主 agent」这条链路。

**已交付**：

- `direct` / `parallel_readonly` 两个模板；
- `survey`（复用 RepoProfile + 测试命令探测）；
- `select_workflow`（规则优先 + LLM 兜底）；
- `plan_tasks`（2–6 个 explore/test 任务，校验/降级）；
- `scheduler ⇄ steering_dispatch`（事件驱动并发、依赖即时解锁、失败隔离、
  checkpoint 边界、动态只读提议）；
- `synthesize` → 现有 `planner`；`worker_context` 注入 planner/act；
- `WorkflowSelected` / `TaskGraphCreated` / `WorkerStarted` / `WorkerFinished` /
  `OrchestrationFinished` 事件与审计；
- `AGENT_ORCHESTRATOR_*` 配置；小仓库仍走 `direct`。

**历史烟雾**（`long/discount_and_audit`，单轮、不可比）：通过 1/1，
`parallel_readonly`，5 任务 / 4 worker（最大同批 4）。当前评测口径与验证记录见
[tests/eval/README](../../tests/eval/README.md)。

### Phase 3B：显式任务 DAG + 只读 worker 扩展（已实现）

**已交付**：

- `depends_on` 调度、依赖失败传播；默认 `partial` 不满足依赖；
- **`depends_mode=any`**：`review`/`test` 可以只消费依赖的产出（success/partial），
  不再因为实现任务未通过完整验证而永远 blocked；failed/blocked/cancelled 仍阻塞；
- 任务级失败重试（`AGENT_ORCHESTRATOR_TASK_RETRIES`，默认 1）；
- `review` worker：复用只读工具循环 + 审查提示，返回 `[blocking]/[warning]` 发现；
- fan-in 只看每个任务的**最后一次**尝试，并报告多 worker 关注同一文件；
- `WorkerFinished.attempt`、审计 `attempt`、eval `worker_retries` 指标。

**烟雾**：`long/discount_and_audit` 通过；4 任务 / 4 worker，最大同批 3，
test worker 失败后重试 1 次，最终自动验证通过。review 任务在该轮因依赖 partial
结果被阻塞，review worker 本身由单测覆盖。

**仍缺**：任务级预算/超时细分、review 与实现后的自动回灌（3C/后续）。

### Phase 3C：并发写 worker（已实现，默认关闭）

**已交付**：

- `implement` role；任务图必须给出 `write_scope`，不同 implement 的 scope
  不能重叠，implement 之间不能互相依赖；
- 每个 implement worker 在独立 `git worktree` 中运行，工具只作用于该 worktree；
- 完成后导出 binary patch，scheduler 在主工作区**顺序** `git apply`；
  先 `--check`，失败则不改主工作区并标 failed；
- 写模式前置条件：`AGENT_ORCHESTRATOR_WRITE_ENABLED=true`、会话 `--write`、
  工作区是**git 仓库根**且干净；`.agent/` 与 `__pycache__/` 不计入脏改动；
- 合并前先 `git apply --check`，再为补丁涉及的主工作区文件留快照；
  `worker_results` 记录 `snapshot_ids`，`agent undo` 可回滚合并；
- 同一批 implement 补丁按**共享文件的连通分量**分批，每个分量各自作为一个
  事务：全部 check/快照后顺序 apply；任一失败只反向撤销本分量，必要时恢复
  快照，其它分量不受影响，避免半合并状态；
- worker 内部工具调用逐条写审计（含 `agent_id`/`task_id`），eval 记录
  `worker_tool_calls` / `worker_merged`；
- 任务图校验失败时，写模式退化为**一个** implement 任务：优先覆盖源码顶层目录，
  扁平仓库退到顶层文件；两者都没有时显式报告降级，不静默消失；
- worktree/patch 文件名在截断后附加短哈希，避免长任务 id 共用路径；
- implement 的 git/worktree/快照/补丁操作都走 `asyncio.to_thread`，scheduler 的
  合并也如此，避免同步 `wsl.exe` 调用阻塞其它 worker 与事件流。

**烟雾实测 1**（`long/discount_and_audit`，写编排开启）：

| 项 | 值 |
|---|---|
| 结果 | **通过 1/1** |
| 工作流 | `orchestrated_implement` |
| 任务 / worker | 1 / 1（planner 写范围重叠，退化为单 implement） |
| worker 状态 | success |
| 合并 | **1 个 patch 已合并** |
| worker 内部工具调用 | **33** |
| 主图工具调用 | 1 |
| input / output tokens | 9,103 / 244 |
| 墙钟 | 54.1s |

**烟雾实测 2（多写并发）**（`long/parallel_modules`，两个独立文件）：

| 项 | 值 |
|---|---|
| 结果 | **通过 1/1** |
| 工作流 | `orchestrated_implement` |
| 任务 / worker | 3 / **2**（两个 implement 同批） |
| 最大同批 | **2** |
| worker 状态 | partial×2（各自验证时对方功能尚未合并） |
| 合并 | **2 个 patch 已合并** |
| worker 内部工具调用 | 12 |
| 主图工具调用 | 5 |
| input / output tokens | 34,107 / 1,085 |
| 墙钟 | 39.6s |

两个 implement worker 在各自 worktree 中并发修改 `app/alpha.py` /
`app/beta.py`，宿主顺序合并后主 agent 最终验证 7 passed。依赖 partial worker
的 test 任务被标 `blocked` —— 这是 Phase 3B 依赖规则的预期行为。

这轮还修掉一个并发缺陷：worktree 与 patch 都在主工作区 `.agent/` 下，第二个
worker 的脏检查会把它们误判为用户改动。`implement._git_ready` 现在用与补丁
收集相同的 pathspec 排除 `.agent/` 与 `__pycache__/`，并有真实 WSL 回归测试
`test_concurrent_implement_workers_ignore_agent_state`。

**已知限制**：

- 并发写要求任务之间 write_scope 不重叠。`long/discount_and_audit` 的两条功能线
  共享 `app/service.py`，planner 的写范围会重叠，因此该任务实际退化为单个
  implement worker；多写并发由 `long/parallel_modules` 覆盖。
- 共享文件的多 worker 合并（3-way rebase）未实现；当前通过 scope 校验避免。
- 自动验证失败的 implement patch 仍可能以 `partial` 合并，由主 agent 的最终
  verify/repair 收口。

### Phase 3D：实时用户干预（已实现）

- [x] `SteeringController` 按 thread 隔离；runtime `steer()` 入队并写审计；
- [x] pause 用 `interrupt()` + checkpoint 挂起，`resume_steering()` 恢复；
- [x] cancel 协作式停止 worker；未合并 patch 丢弃，已合并改动保留；
- [x] `/add` 追加只读任务；`/replan` 保留已完成任务并重建剩余任务图；
- [x] `SteeringApplied` / `WorkflowRevised` 事件与 `kind=steering` 审计；
- [x] TUI 命令、暂停状态与新提示；无头 TUI 测试覆盖；
- [x] `ruff`、CI 子集、全量 unit、WSL 集成通过。

### Phase 4：评测与调优

**首切片已实现**：

- [x] worker token、total token、worker_seconds、worker_parallelism；
- [x] worker 角色分布、合并尝试/冲突/成功率；
- [x] `compare.py`：噪声带守卫 + 逐任务翻转 + `noise_suspect`；
- [x] `steering_bench.py`：pause/resume/cancel 延迟（wait/apply 分开）；
- [x] default 套件 `--repeat 2` 基线（见 tests/eval/README）；
- [x] A/B/C 三批加固：写编排脏检查/快照、实时 worker 事件、
      worker token 口径、去阻塞、`depends_mode`；
- [x] **Phase 4D**：worker 提议只读任务 + host 校验/总量上限 +
      `TaskGraphExtended` 事件。

**仍计划中**：

- 成本感知预算（全局/任务/worker token 上限）；
- 模型分层（worker 用更便宜的模型）；
- 扩展更难的能力评测任务（常规短任务套件已饱和，能力结论改用 `long` 或新尺子）；
- 在噪声带之外量化编排/并发的真实收益。

---

## 17. Phase 3A 验收清单

- [x] `AGENT_ORCHESTRATOR_ENABLED=false` 时，事件/状态/行为与现有基线一致。
- [x] 开启后，`direct` 与 `parallel_readonly` 能根据请求/仓库画像被选中。
- [x] `parallel_readonly` 至少 2 个 explore worker 并发（事件/指标可见）。
- [x] worker 失败不影响同批其它 worker；依赖失败标 `blocked`。
- [x] `synthesize` 的 `worker_context` 进入主 agent 的 planner/act，但**不写回**主消息历史。
- [x] `WorkflowSelected` / `TaskGraphCreated` / `WorkerStarted` / `WorkerFinished` /
      `OrchestrationFinished` 事件与审计齐全。
- [x] `estimate_recursion_limit` 覆盖编排路径。
- [x] `ruff`、全量 unit、CI 子集、WSL 集成通过。
- [x] 文档明确区分已实现与计划中。

> 单任务烟雾见 §14；通过率/成本仍需 `--repeat 2` 量化，不能据单次运行下结论。

---
