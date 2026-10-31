---
layer: L2
doc_id: rfc-0008
title: verify 失败后的有界编排修复轮
owner: 项目作者
status: accepted
created: 2026-10-30
last_reviewed: 2026-10-30
review_cadence: 状态变更或范围变化时
---

# RFC-0008：verify 失败后的有界编排修复轮

> 编排器合并过 implement 补丁后，如果主图 verify 失败，允许带失败上下文
> 有界地重入 orchestrator 生成"只修失败项"的 implement 任务；默认关闭，
> 失败/超限时仍回到原有 repair/replan 路径。

## 1. 目标

- **G1 回流**：verify 失败且本次验证针对编排合并结果时，可以把失败摘要交给
  orchestrator，再派发少量 implement worker 修复。
- **G2 有界**：修复轮数由 `AGENT_ORCHESTRATOR_FIX_ROUNDS` 控制（默认 0 =
  关闭）；每轮递增计数，超限后回落到 repair/replan。
- **G3 不重做**：修复轮只生成针对失败项的写任务，不重新执行已完成的探索，
  也不重写整个计划。
- **G4 基线正确**：修复 worker 复用 RFC-0007 的 worktree 基线注入，能看到
  第一轮合并后的改动。
- **G5 安全不变**：write_scope、审批、快照、合并事务、路径守卫都不变；
  取消后不再进入修复轮。

可验证目标：

- `pytest -m "not wsl and not llm" tests/unit/test_routing.py tests/unit/test_verify_node.py tests/unit/test_orchestrator.py tests/unit/test_scheduler.py tests/unit/test_recursion_limit.py`
- 真实 WSL：`pytest -m wsl tests/integration/test_implement_worker.py`

## 2. 非目标

- 不做无限自动修复；不做跨会话的修复记忆。
- 不改变主图 repair/replan 的预算与语义；修复轮只是它们之前的一次机会。
- 不对 verify 的 `error`（执行环境异常）触发修复——模型修不了坏环境。
- 不新增 worker 角色、不改审批/沙箱/路径守卫。
- 不把修复轮的结果写入长期记忆。
- 不在只读编排（没有合并补丁）后触发修复。

## 3. 背景与证据

- 编排子图只在 `START` 进入；结束后交回主图 planner：
  `graph/build.py` 的 `route_entry` / `route_after_synthesize`。
- `pending_verification` 在 verify 中消费并清零：
  `graph/nodes/verify.py:53,133-135`。
- verify 失败后的出口只有 repair/replan/respond：
  `graph/routing.py:150-170`。
- 顺序写波次（RFC-0007）已提供 worktree 基线注入，修复 worker 可以看到
  第一轮合并结果，否则重入 implement 会因"工作区不干净"或看不到改动而失败。

## 4. 受影响面

| 项 | 内容 |
|---|---|
| 模块 | `graph/routing.py`、`graph/build.py`、`graph/nodes/orchestrator_fix.py`、`graph/nodes/verify.py`、`graph/state.py`、`orchestrator/{state,workflow,plan,scheduler_run}.py` |
| 契约 | 新增配置 `orchestrator_fix_rounds`（默认 0）；新增状态字段 |
| 数据 | 无迁移；修复轮复用 `.agent/` 下的 worktree/patch/快照 |
| 前端 | 可选：事件流会看到额外的 `WorkflowSelected` / worker 事件；无强制契约变化 |
| 权限 | 不新增；修复 worker 仍受 write_scope 与审批边界约束 |

## 5. 安全与隐私

- 修复请求只包含 verify 的结构化摘要、失败测试与关键 finding；不附带完整
  文件内容。verify 输出在回灌前已经脱敏（既有路径）。
- 修复任务仍必须声明 write_scope，并经 host 校验；不允许在修复轮放宽
  `implement 之间不得并发重叠` 的规则。
- 修复轮不改变 `allow_manifest=False` 的自动验证约束。
- 取消优先：`steering_cancelled` 时不进入修复轮。
- 轮数上限 + 递归上限双重约束，避免 token 循环。

## 6. 兼容与回滚

- **默认关闭**：`AGENT_ORCHESTRATOR_FIX_ROUNDS=0` 时路由与状态行为完全不变。
- **无数据迁移**；关闭开关即可回退。
- 已发生的修复轮不会撤销；其补丁仍走正常合并/快照/undo。
- 回滚后新 run 不再产生修复轮。

## 7. 可观测性

- 状态记录 `orchestrator_fix_rounds_used`、`orchestrator_fix_mode`、
  `orchestrator_fix_request`。
- 修复轮仍走 orchestrator 子图，因此 `WorkflowSelected`、`TaskGraphCreated`、
  `WorkerStarted/Finished`、`OrchestrationFinished` 都会自然出现。
- 审计可通过 task 的 stage/seed 字段区分修复 worker。
- 事件不新增必选类型；前端不需要同步。

## 8. 验收标准

```text
Given 编排合并过补丁，主图 verify 失败，AGENT_ORCHESTRATOR_FIX_ROUNDS=1
When  route_after_verify 评估出口
Then  进入有界修复轮；修复 worker 使用基线注入；修复后直接回到 verify
锚点：REQ-228
tests: tests/unit/test_routing.py::test_failed_orchestrated_verify_enters_fix_round
       tests/unit/test_scheduler.py::test_fix_mode_worker_receives_baseline_paths
       tests/unit/test_verify_node.py::test_failed_pending_verification_marks_fix_eligible
```

```text
Given 修复轮数已达上限，或 verify 是环境错误，或本次没有编排合并
When  verify 失败
Then  不进入修复轮，按原有 repair/replan/respond 处理
锚点：REQ-229
tests: tests/unit/test_routing.py::test_fix_rounds_are_bounded
       tests/unit/test_routing.py::test_verification_error_does_not_enter_fix_round
```

## 9. 替代方案与代价

| 方案 | 优点 | 代价 | 结论 |
|---|---|---|---|
| 新增有界修复轮（本 RFC） | 复用 orchestrator/worker；边界清晰 | 新增图回边与状态字段 | 选 |
| 在 repair 节点内直接调 implement worker | 不新增图边 | 把调度逻辑塞进 repair，状态更乱 | 否 |
| 让主 agent 自己修 | 零改动 | 失去 worker 并行与基线隔离 | 现状 |
| 无上限重入 | 可能修好 | token 循环，用户拿不到结论 | 否 |

## 10. 决策与后果

- 最终选择：方案 A，默认关闭。
- 批准人 / 日期：项目作者 / 2026-10-30。
- 真实结果与预期差异：实现后补。
- 新增技术债：修复轮只做"有界重试"，不做失败签名聚类；保留
  `worker_results` 以维持 token 预算，fan-in 会包含历史记录。
- 关联 ADR：[`0028-orchestrated-fix-rounds`](../adr/0028-orchestrated-fix-rounds.md)。

## 11. 任务分解与验证计划

| # | 任务 | 负责模块 | 验证方式 |
|---|---|---|---|
| 1 | 状态字段 + runtime 复位 | `graph/state.py`、`runtime/core.py`、`runtime/sessions.py` | `test_runtime_orchestrator.py` |
| 2 | verify 标记失败来源 | `graph/nodes/verify.py` | `test_verify_node.py` |
| 3 | 路由与递归上限 | `graph/routing.py`、`graph/build.py`、`estimate_recursion_limit` | `test_routing.py`、`test_recursion_limit.py` |
| 4 | 修复准备节点与图边 | `graph/nodes/orchestrator_fix.py`、`graph/build.py` | 新增单测 |
| 5 | 强制 workflow + fix 写模式 | `orchestrator/workflow.py`、`plan.py` | `test_orchestrator.py` |
| 6 | fix worker 基线注入 | `orchestrator/scheduler_run.py` | `test_scheduler.py` |
| 7 | 配置/契约/ADR/验收锚点 | `config.py`、`docs/` | `gen_contracts.py --check`、`check_docs.py` |

### 11.1 完成定义

- 开关为 0 时全部既有测试通过，图行为不变。
- 开关 >0 时 REQ-228/229 测试存在且通过。
- 递归上限覆盖最坏路径（fix 轮 × orchestrator 边界）。
- 取消、环境错误、只读编排都不会进入修复轮。
