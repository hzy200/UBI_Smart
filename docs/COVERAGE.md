---
layer: L3
doc_id: coverage
title: 覆盖率基线
owner: 项目作者
status: active
last_reviewed: 2026-10-24
review_cadence: 测试口径、覆盖率门槛或主要子系统变动时
---

# 覆盖率基线

旧 `.coverage` 停在 2026-10-23 18:47，已不能代表当前工作区。下面的数字是
2026-10-24 P3 覆盖率闭环后用 CI 同口径重跑的结果。

## 1. 口径

```bash
pytest -m "not wsl and not llm" --cov --cov-report=term-missing
```

- 不走真实 WSL/bwrap：Windows 本地没有安装 WSL 项目依赖；
- `mysql` 标记用例在未设置 `AGENT_TEST_MYSQL_URL` 时 skip；
- 本轮 `-m "not wsl and not llm"` 收集 **1,904** 个用例，**223** 个被 deselect；
- 总语句 **19,940**，未覆盖 **3,948**，**80.20%**（门槛 76%）。

## 2. P3 目标达成情况

| 模块 | P3 目标 | 当前 | 覆盖手段 |
|---|---:|---:|---|
| `orchestrator/implement.py` | ≥50% | **77%** | fake sandbox + 假工具/假 LLM；WSL 路径仍由 `-m wsl` 门禁 |
| `control_server.py` | ≥70% | **99%** | 参数冲突、迁移失败、握手降级、shutdown 回调 |
| `execution/sandbox_backend.py` | ≥70% | **100%** | fake sandbox / fake fs / fake snapshots |
| `desktop/setup.py` | 配置/参数/错误分支 | **100%** | 无头驱动 `SetupApi.detect/finish/pick_folder` |
| `desktop/launcher.py` | 配置/参数/错误分支 | **62%** | 启动/锁/回退/错误窗口；GUI 窗口仍手测 |

## 3. 其他子系统缺口

| 模块 | 覆盖率 | 说明 |
|---|---:|---|
| `orchestrator/retry.py` | 21% | 重试分支基本未覆盖 |
| `desktop/supervisor.py` | 50% | 进程启动/重启分支未覆盖 |
| `control/backup.py` | 51% | MySQL 备份/恢复分支未覆盖 |
| `agent_server.py` | 60% | 部分路由与 SSE 分支未覆盖 |
| `orchestrator/merge.py` | 62% | 宿主侧 git 分支未覆盖 |
| `agent_server_support.py` | 77% | RunState / SSE 原语 |
| `web/local_runtime.py` | 85% | 同进程回退；配额/TTL 分支基本覆盖 |
| `cli/platform.py` | 89% | doctor/sandbox-init 适配器 |
| `agent_context.py` | 92% | A3 租户目录与 runtime 构造 |

`execution/service.py`（89%）、`control/migrate.py`（92%）、
`control/store.py`（89%）、`web/app.py`（83%）相对较好。

## 4. 结论

- 旧的 75.36% / 86.6% 不能继续引用；以本页和 CI 输出为准；
- 全局 80.20% 已超过门槛，但仍不代表所有子系统健康：`retry.py`、
  `supervisor.py`、`backup.py`、`agent_server.py`、`merge.py` 仍是明显缺口；
- P3 已把四个重点模块拉到达标线；`implement.py` 的剩余未覆盖行集中在真实
  WSL/patch 路径，继续靠 `-m wsl` 与集成测试守着，不再在 CI 里堆假集成；
- 后续按子系统设目标，不把全局门槛当成唯一指标。
