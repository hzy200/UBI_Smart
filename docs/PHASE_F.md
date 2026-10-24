---
layer: L3
doc_id: phase-f
title: Phase F：安全与性能验收
owner: 项目作者
status: active
last_reviewed: 2026-10-24
review_cadence: 安全边界、执行后端或性能基线脚本变动时
---

# Phase F：安全与性能验收

Phase F 不再加功能，只把安全边界、性能基线、迁移/回滚演练变成**可重复执行**
的验收，并明确哪些项必须人工或特定平台完成。

## 1. 安全验收矩阵

| 资产 / 边界 | 验收方式 | 状态 |
|---|---|---|
| API Key 不进控制面数据库 | `pytest tests/unit/test_phase_f_security.py`；SQLite 文件按字节扫描 | 通过 |
| API Key 不出现在 Provider API | 同上，`/api/providers` 响应不含 key | 通过 |
| API Key 不进日志 | 同上，`caplog` 扫描创建/会话路径 | 通过 |
| 审计/错误脱敏 | `redact_secrets` 覆盖 `sk-…`、私钥、JWT、DB URL 等 | 通过 |
| checkpoint/工作区/运行状态不进 MySQL | `control/schema.py` 列清单 + Phase A 数据归属 | 通过（结构） |
| A2 不执行工具、不直接读写工作区 | Phase A/B 边界 + 代码审查 | 通过 |
| 内部接口必须 token | `test_agent_handshake.py`：`/v1/meta` 无 token 401 | 通过 |
| 前端 API 认证 | `test_web_auth.py`：无 token 401、loopback 匿名边界 | 通过 |
| 路径逃逸 | `test_browse.py`、`test_execution_service.py` 的 escape 用例 | 通过 |
| 命令分级/审批 | `test_approve.py`、`test_shell_tool.py`（WSL） | 通过（WSL 门禁） |
| 审计哈希链 | `test_audit.py`、`agent audit --verify` | 通过 |
| MySQL 仅本机、最小权限 | `packaging/mysql/init.sql`；人工 `SHOW GRANTS` | 人工 |
| WSL 发行版隔离 | `pytest -m wsl`、`agent doctor`（非 root、路径/命令） | 平台门禁 |
| Linux bwrap/content 隔离 | `pytest tests/integration/test_bwrap.py`、`pytest -m execution` | Linux 门禁 |
| 内部接口只监听 loopback | systemd/桌面启动参数 + `test_desktop_topology.py` | 通过 |

WSL/Linux 沙箱用例需要真实执行面，CI 的 Linux runner 只跑
`pytest -m execution`，Windows 本地用 `pytest -m wsl`；两者都没有时必须在
`docs/ACCEPTANCE.md` 的“未验证”里保留缺口，不能把“快反馈绿”读成沙箱可用。

## 2. 性能基线

脚本：`scripts/phase_f_bench.py`。不设阈值，输出 JSON 供版本间比较。

本机实测（2026-10-24 P4 重跑，Windows 11 / conda `agent`；MySQL 未配置，
保留上一轮数值作参考）：

| 指标 | 结果 |
|---|---|
| A2 进程启动 → `/healthz` 就绪 | **5.61s**（单次样本；P4 前为 27.15s） |
| A2 代理 SSE 首事件（fake A3） | p50 30.99ms / p95 44.32ms（20 次，已预热） |
| ExecutionService 直调 backend | p50 0.003ms / p95 0.007ms（400 次） |
| 经 ExecutionService + 回执 | p50 1.018ms / p95 1.509ms；回执约 381 B/次（400 次） |
| SQLite `save_sessions`（10 行） | p50 10.55ms / p95 14.15ms（60 次） |
| SQLite `index_audit`（500 条） | 38.71ms；库文件 96 KiB |
| MySQL `save_sessions`（10 行） | p50 13.80ms / p95 15.66ms（上一轮，本轮未重跑） |
| MySQL `index_audit`（500 条） | 178.39ms；表数据+索引 128 KiB（上一轮） |

> **A2 启动口径**：从 `Popen` 到 `/healthz` 返回 `agent_state=ok`，含进程启动、
> 模块导入与 A3 握手。P4 发现拆分模式仍在模块级导入 A3 runtime（连带
> `torch`/`transformers`），改为 `web/local_runtime.py` 内延迟导入后，
> 启动从 ~27s 降到 ~5.6s。单次样本只用于趋势，不设阈值。
>
> 原始 JSON 不落库：需要版本对比时重跑脚本并保存输出；上表是 2026-10-24
> 的实测快照，代码/依赖变动后应视为过期。

口径与限制：

- 都是**本机 loopback**，不含真实网络、TLS、反向代理与 Redis；
- SSE 用 fake A3，不调用模型，测的是 A2 代理与序列化开销；
- 长会话渲染需要浏览器，未纳入脚本；Phase E 用 `content-visibility`
  做了渲染优化，实际帧率/输入延迟需人工观察；
- 工具真实 I/O（git/测试/大文件）未测；ExecutionService 只测了代理与回执开销；
- Windows 文件系统与 CI Linux runner 的绝对值会不同，比较时看同一平台的趋势。

运行：

```bash
python scripts/phase_f_bench.py
python scripts/phase_f_bench.py --mysql-url "mysql+pymysql://root:...@127.0.0.1:3306/coding_agent_test"
```

## 3. 迁移 / 回滚演练

脚本：`scripts/phase_f_drill.py`。顺序：

```text
upgrade → 写入 → 备份 → downgrade base → upgrade → 恢复 → 校验
```

实测：

- SQLite：upgrade、备份 69,632 B、downgrade 后表消失、重升级为空、
  恢复后标题回到 `before`、revision `0001_control_schema`；
- MySQL 8.0.27（独立 `*_test` 库）：同上，备份 9,660 B，恢复后数据一致。

运行：

```bash
python scripts/phase_f_drill.py
python scripts/phase_f_drill.py --mysql-url ".../coding_agent_test"
```

MySQL 演练会清空该库的表，脚本强制库名包含 `test`。CI 只跑 SQLite；
备份/恢复脚本与 runbook 见 `docs/runbook.yaml` RB-017。

## 4. 未覆盖 / 人工项

- 浏览器长会话渲染与交互延迟：在 `docs/PHASE_E.md` 手测清单中观察；
- WSL/bwrap 真实隔离：需要装好项目依赖的 WSL 或 Linux 主机，执行
  `pytest -m wsl` / `pytest -m execution`；
- MySQL 最小权限账号：需要 DBA/root 环境，按 `packaging/mysql/init.sql`
  创建后用 `SHOW GRANTS` 与 `USE mysql` 失败验证；
- 多实例 Redis 事件传输延迟与跨机 TLS/mTLS：未在本轮范围；
- 提示注入（T2 主战场）：仍按 `docs/THREAT_MODEL.md` 的残余边界处理，
  不属于本阶段可自动验收项。
