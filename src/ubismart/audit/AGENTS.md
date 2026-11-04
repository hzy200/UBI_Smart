<!-- owner: 项目作者 · last_reviewed: 2026-10-22 -->

# audit/ · 可追溯性

**审计有缺口是本项目最不能接受的失败模式之一。**
一条悄悄丢掉的记录，比一次失败的运行更糟。

## 职责与入口

| 文件 | 入口 | 作用 |
|---|---|---|
| `logger.py` | `AuditLogger` / `AuditError` / `verify_audit_files` / `read_records` | 按天追加 JSONL、哈希链、轮转、读取 |
| `models.py` | `AuditRecord` / `kind` 常量 | 单一日志模型，字段按 `kind` 分组 |

字段分组的事实源是 `docs/contracts/audit.yaml`（由 `scripts/gen_contracts.py` 生成）。

`orchestration` 记录带结构化编排字段：`est_* / actual_*`、`invalid_steps`、
`invalid_step_ratio`、`state_from/to`、`checkpoint_id`、`rollback_level`、
`concurrency_unit`、`isolation_level`、`approval_ack_ms`。改这些字段后要重跑
`gen_contracts.py`，否则指标/回归脚本会对不上。

## 禁区

- **写失败必须抛 `AuditError`**，由 runtime 转成 `RunFailed` 事件；禁止静默丢弃或降级为日志。
- `async_write=True`（runtime 默认）走**有界队列 + 专用写线程**：`write` 在
  队列满或已有后台错误时立即抛 `AuditError`；`flush` 在运行收尾显式排空并
  抛出后台错误。异步化只移走阻塞，不允许把“审计失败”变成静默计数。
- 不要改 `_canonical_record` / `record_hash` 的字段顺序或算法——
  会破坏 `agent audit --verify` 与历史链。
- 参数先 `sanitize_args`（脱敏 + 截断）再落盘；
  不要把 `file_write` 的 content 原样写进日志。
- 轮转时新分片必须接上 `prev_hash`；改分片逻辑后必须验证跨文件哈希链。
- 改 `models.py` 的字段后重跑 `python scripts/gen_contracts.py`，否则 CI 契约检查会红。

## 依赖与测试

- 依赖 `security.secrets`、`filelock`；不要 import `graph` / `orchestrator` / `tools`。
- 回归：`pytest -m "not llm" tests/unit/test_audit.py tests/unit/test_audit_chain.py`
- 链验证的端到端命令：`agent audit --verify`。
