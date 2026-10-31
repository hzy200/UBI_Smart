---
layer: L3
doc_id: phase-c
title: Phase C：L3/L4 执行契约
owner: 项目作者
status: active
last_reviewed: 2026-10-24
review_cadence: ExecutionService 契约、幂等或后端适配变动时
---

# Phase C：L3/L4 执行契约

Phase C 把“执行”收敛成一个显式契约：A3 只通过 `ExecutionService` 调用
A4，不再由各工具直接摸沙箱。**本切片完成 C0 契约、C1 进程内适配，以及
C2/C3 的执行路径代理**：既有工具仍按 `sandbox.run() -> ExecResult` 调用，
但 Runtime 会把 sandbox 包成 `ExecutionSandbox`，实际命令统一走 A4 backend。

## 1. 契约

`src/ubismart/execution/`：

| 对象 | 作用 |
|---|---|
| `ExecutionRequest` | `op_id / kind / workspace_id / run_id / tool_call_id / payload / timeout / capture_limit` |
| `ExecutionResult` | `status / seq / exit_code / stdout / stderr / duration_ms / data / artifacts / snapshot_ids / receipt_id / replayed / error` |
| `ExecutionBackend` | `run / read_text / write_bytes / list_dir / stat / capabilities` |
| `SandboxExecutionBackend` | 把现有 `WslSandbox` / `LocalLinuxSandbox` + `SandboxFs` 包装成后端 |
| `ExecutionService` | 幂等、序号、请求→结果映射；不做审批/命令分级 |

当前已实现 kind：`run`、`read_file`、`write_file`、`list_dir`、`stat`、
`git`、`snapshot`、`restore`、`worktree_create`、`worktree_remove`、
`patch_apply`。未知 kind 返回 `unsupported` 回执，不静默执行。

**路径代理**：`AgentRuntime` 在 workspace 解析后创建
`ExecutionService(SandboxExecutionBackend(...))`，并用 `ExecutionSandbox`
包住 sandbox。既有 shell、文件、Git、快照、worktree 代码仍调用
`sandbox.run()`，但最终执行都经过 A4 backend；工具层没有第二条执行路径。

**工具级 op_id**：图/Scheduler/子智能体在调用工具前设置
`run_id + tool_call_id` 上下文；`ExecutionSandbox` 为每次内部 `run` 生成
稳定 op_id（`run:call:序号`）。同一工具调用重试/恢复时序号从头开始，
命中执行回执，不重复副作用。无上下文的运行时探针仍走 `run_raw` 兼容路径。

## 2. 幂等与序号

- **op_id 幂等**：同一 `op_id` 重复提交返回同一回执，`replayed=true`；
- **冲突**：同一 `op_id` 携带不同请求指纹 → `ExecutionConflict`，不执行第二次；
- **回执日志**：`<workspace>/.agent/execution/receipts.jsonl`，服务重启后仍可去重；
- **序号**：`seq` 按 `run_id` 单调递增，跨服务重启从回执日志恢复；
- **事件序号独立**：执行 `seq` 与 Web SSE 的 `seq` 不是同一个编号，避免重放语义混淆；
- **副作用**：shell 不做自动重放；重复请求只返回回执，不重复执行；
- **取消三态**：`record_unknown()` / `record_cancelled()` 写入
  `unknown`/`cancelled` 终态；工具层原有的 ToolJournal 继续负责
  “已开始但未结束 = 结果未知”的跨进程判断。

**worktree / patch 回执**：合并路径的 `git apply --check`、`git apply`、
`git apply --numstat` 通过 `ExecutionService(kind="git")` 执行并写回执；
新增 `patch_apply` kind，为后续把 merge 结果结构化打底。

## 3. 契约测试矩阵

共享用例在 `tests/contract/execution_cases.py`，覆盖：

- 文件写/读/列目录/stat；
- 成功命令、失败命令、路径逃逸；
- 快照 → 覆盖 → 恢复；
- 重复 op_id 不重复执行、序号连续。

执行环境：

| 后端 | 测试 | 运行环境 |
|---|---|---|
| Linux local | `tests/integration/test_execution_contract_local.py` | Linux CI（`pytest -m execution`） |
| WSL | `tests/integration/test_execution_contract_wsl.py` | Windows 本地（`pytest -m wsl`） |
| 语义/幂等 | `tests/unit/test_execution_service.py` | 任意平台 |

CI 已新增 Linux `pytest -m execution` 步骤；WSL 契约仍需在 Windows 本地跑。
两者跑的是同一份 `assert_execution_contract`，这是 Phase C 退出条件的承载物。

## 4. 本轮验证

- WSL 真实契约通过（run/write/read/list/stat/snapshot/restore/idempotency/escape）；
- 单元测试覆盖 git/snapshot/restore/worktree dispatch、幂等、冲突、跨服务重启序号、失败/超时、unsupported；
- Runtime 代理测试通过；
- 工具级 op_id 重试命中回执；
- 合并 `_git` 走 ExecutionService（不再回退 sandbox.run）；
- unknown/cancelled 终态与幂等回放；
- WSL 集成：`test_shell_tool.py`、`test_snapshots.py`、`test_git_tools.py`、
  `test_verify.py` 通过；
- 全量快反馈测试通过。

## 5. 尚未完成（C2/C3 剩余）

- worktree 的 `patch/merge` 回执尚未进入 Scheduler 事件/WorkerResult，
  目前只保证 `_git` 执行与回执落盘；
- Linux local 契约在 CI 上的首次实际运行结果；
- 执行回执日志的轮转/清理与权限收紧；
- 工具取消时自动写入 `cancelled` 终态（当前由 ToolJournal 标记未知）；
- 将 ExecutionService 暴露为本地 IPC/HTTP 服务（Phase B 已有 A2/A3 传输，但
  L3→L4 仍先保持进程内）。

## 6. 退出条件（Phase C 全量）

- [x] 所有执行 kind 都有契约实现；
- [x] WSL 与 Linux local 共用同一份契约测试（Linux CI 待首次实跑确认）；
- [x] tools/orchestrator 经过 ExecutionSandbox 代理，WSL 集成回归通过；
- [x] 工具调用级 op_id/回执、unknown/cancelled 终态；
- [x] 合并 `git apply` 走 ExecutionService 并写回执；
- [ ] merge 回执尚未暴露到 Scheduler 事件；Linux CI 实跑与回执轮转待补。
