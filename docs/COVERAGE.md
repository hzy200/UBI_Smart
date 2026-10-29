---
layer: L3
doc_id: coverage
title: 覆盖率基线
owner: 项目作者
status: active
last_reviewed: 2026-10-28
review_cadence: 测试口径、覆盖率门槛或主要子系统变动时
---

# 覆盖率基线

旧 `.coverage` 已不能代表当前工作区。下面的数字是 2026-10-29 用 CI 同口径
重跑的结果；**用例数/覆盖率/承重模块的机器快照以 [METRICS.md](METRICS.md)
为准**（由 `scripts/snapshot_metrics.py` 生成），本页只解释口径与缺口。

## 1. 口径

```bash
pytest -m "not wsl and not wsl_env and not llm" --cov --cov-report=term-missing
```

- 不走真实 WSL/bwrap：Windows 本地没有安装 WSL 项目依赖；
- `mysql` 标记用例在未设置 `AGENT_TEST_MYSQL_URL` 时 skip；
- 2026-10-28 那次重跑选中 **3,002** 个用例（1 条 skip），总语句 **33,217**、
  未覆盖 **5,533**、**83.34%**（门槛 76%）。

> **当前值不在这里。** 用例数、总覆盖率与承重模块的机器快照以
> [METRICS.md](METRICS.md) 为准（由 `scripts/snapshot_metrics.py` 生成并在 CI 校验），
> 本节数字保留为当日记录，仅用于说明口径。

## 2. P3 目标达成情况

| 模块 | P3 目标 | 当前 | 覆盖手段 |
|---|---:|---:|---|
| `orchestrator/implement.py` | ≥50% | **75.8%** | fake sandbox + 假工具/假 LLM；WSL 路径仍由 `-m wsl` 门禁 |
| `control_server.py` | ≥70% | **98.9%** | 参数冲突、迁移失败、握手降级、shutdown 回调 |
| `execution/sandbox_backend.py` | ≥70% | **100%** | fake sandbox / fake fs / fake snapshots |
| `desktop/setup.py` | 配置/参数/错误分支 | **100%** | 无头驱动 `SetupApi.detect/finish/pick_folder` |
| `desktop/launcher.py` | 配置/参数/错误分支 | **46.3%** | 启动/锁/回退/错误窗口；GUI 窗口仍手测 |

## 3. 本轮补齐情况

以下为 2026-10-28 快反馈子集重跑值；真实 WSL 用例不在其中。

| 模块 | 之前 | 当前 | 覆盖手段 |
|---|---:|---:|---|
| `orchestrator/retry.py` | 21% | **100%** | 空上下文、字段截断、条目上限 |
| `desktop/supervisor.py` | 50% | **82.8%** | 假 Popen/握手、端口校验、进程清理 |
| `control/backup.py` | 51% | **99.1%** | MySQL dump/restore、密码走环境变量、失败清理 |
| `agent_server.py` | 60% | **75.6%** | 共享传输 SSE、缓冲裁剪、运行异常、租户上下文 |
| `agent_server_routes/state.py` | 46% | **96.3%** | fork/rewind/checkpoints、记忆动作、审计与会话 |
| `agent_server_routes/workspace.py` | 50% | **81.5%** | 文件/Git/项目、快照恢复、上传配额与删除越界 |
| `agent_server_routes/lifecycle.py` | 79% | **94.2%** | 流式补齐、steering 恢复、取消/关停、compact |
| `orchestrator/merge.py` | 62% | **91.6%** | numstat 解析、快照/回退、3-way 预演 |
| `agent_server_support.py` | 77% | **100%** | RunState / SSE / tenant 纯函数 |
| `server.py` | 0% | **92%** | 参数解析、握手、uvicorn 装配 |
| `web/local_runtime.py` | 85% | **83.1%** | 同进程回退；配额/TTL 分支基本覆盖 |
| `cli/platform.py` | 89% | **92.9%** | doctor/sandbox-init 适配器 |
| `agent_context.py` | 92% | **94.7%** | A3 租户目录与 runtime 构造 |

剩余缺口集中在真实 WSL/patch 路径与长连接超时分支（`agent_server.py` 的
keepalive、`desktop/launcher.py` 的 GUI 窗口），继续靠 `-m wsl`、集成测试与
桌面手测守着，不在 CI 里堆假集成。

`execution/service.py`（92.0%）、`control/migrate.py`（69.0%）、
`control/store/`（89.8%，拆包后按子模块统计）、`web/app.py`（82.2%）为本轮实测。

P1-3 新身份/审批模块本轮实测：`control/mfa.py` 92.8%、
`control/approvals.py` 97.8%、`control/oidc.py` 99.2%、
`web/routes/auth.py` 99.0%、`web/routes/admin.py` 90.2%。这些模块均已超过
P1-8 计划的 85% 承重模块门槛；`control/credentials.py` 95.8%、
`graph/nodes/approve.py` 100.0%。

P1-3/P1-5 新增与改动模块：`control/workspaces.py` 93.0%（ACL 授权/撤销/
可见性）、`web/middleware.py` 95.6%（审计只读中间件）、`agent_server.py`
80.1%（drain 入口与拒绝新运行）、`web/routes/workspaces.py` 82.7%、
`control/migrate.py` 69.0%（0013 与无版本旧库识别）。

## 4. 结论

- 旧的 75.36% / 80.20% 不能继续引用；以本页和 CI 输出为准；
- 全局 **84.62%** 已超过门槛；`web/routes/runs.py` 通过 fake RunDeps 补到
  **86.3%** 并纳入棘轮；`sandbox/fs.py`（95.9%）、`tools/files.py`（89.3%）
  已用假沙箱单测覆盖错误/回滚分支，`sandbox/snapshots.py`（76.8%）仍由
  WSL 门禁单列；
- 本地授权 `host_broker/consent.py` 已从 18.7% 提到 **97%**（Tk/远程/stdin
  与失败关闭），`tools/git.py` 用假沙箱补到 **97%**（路径越界、`.agent`
  保护、失败回报），`tools/code_intel.py` 用假 FS/LSP 补到 **99%**；三者均
  纳入覆盖率棘轮；`agent_server_routes/workspace_import.py` 补到 **88.1%**（导入 worker、上传入口/状态/
  取消、恢复/删除归档），也已纳入棘轮；未覆盖行仍必须继续可见；
- 测试分层与运行口径见 [TESTING.md](TESTING.md)：冒烟挡启动、回归守不变量、
  增量缩短反馈、集成与全量负责深层行为；
- 覆盖率仍是可见性工具：沙箱层在 Linux CI 不执行，不许用 omit 抹平。

## 5. 承重模块棘轮

`scripts/coverage_budget.json` 为账号、审批、执行契约、命令分级等模块设
独立下限；`python scripts/check_module_coverage.py --json coverage.json`
在 CI 卡“不许回退”，`--update` 只抬高下限（故意下调需要 `--force`）。
目标统一为 85%；低于目标的模块会打印告警，夜间/发版可用 `--strict-target`。
`sandbox/fs.py`、`sandbox/snapshots.py`、`tools/files.py` 另有 WSL 门禁
（`.github/workflows/sandbox.yml`）。`fs.py` / `files.py` 的 Python 侧分支
已由假沙箱单测覆盖，WSL 门禁继续负责真实脚本与路径校验；CI 子集的低覆盖
不会被当成“已达标”。

用例数、总覆盖率与模块当前值由 `scripts/snapshot_metrics.py` 生成到
[METRICS.md](METRICS.md) 与 [metrics.json](metrics.json)；CI 用
`--check` 校验，不再手抄。
