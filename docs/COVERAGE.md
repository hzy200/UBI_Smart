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
2026-10-24 用 CI 同口径重跑的结果。

## 1. 口径

```bash
pytest -m "not wsl and not llm" --cov --cov-report=term-missing
```

- 不走真实 WSL/bwrap：Windows 本地没有安装 WSL 项目依赖；
- `mysql` 标记用例在未设置 `AGENT_TEST_MYSQL_URL` 时 skip；
- 本轮 `-m "not wsl and not llm"` 收集 **1806** 个用例，**223** 个被 deselect；
- 总语句 **19,770**，未覆盖 **4,378**，**77.86%**（门槛 76%）。

## 2. 子系统缺口

| 模块 | 覆盖率 | 说明 |
|---|---:|---|
| `orchestrator/implement.py` | **19%** | 写 worker 主体未被 CI 子集覆盖；依赖 WSL/集成路径 |
| `orchestrator/retry.py` | 21% | 重试分支基本未覆盖 |
| `control_server.py` | 42% | CLI 参数、迁移失败、shutdown 路径缺测试 |
| `execution/sandbox_backend.py` | 44% | 真实 backend 分支依赖执行面 |
| `desktop/launcher.py` / `desktop/setup.py` | **0%** | GUI/向导路径未做无头测试 |
| `desktop/supervisor.py` | 50% | 进程启动/重启分支未覆盖 |
| `agent_server.py` | 57% | 部分路由与 SSE 分支未覆盖 |
| `orchestrator/merge.py` | 62% | 宿主侧 git 分支未覆盖 |
| `control/backup.py` | 51% | MySQL 备份/恢复分支未覆盖 |
| `web/local_runtime.py` | 85% | 同进程回退；配额/TTL 分支基本覆盖 |
| `cli/platform.py` | 89% | doctor/sandbox-init 适配器 |
| `agent_context.py` | 92% | A3 租户目录与 runtime 构造 |
| `agent_server_support.py` | 77% | RunState / SSE 原语 |

`execution/service.py`（89%）、`control/migrate.py`（92%）、
`control/store.py`（89%）、`web/app.py`（77%）相对较好。

## 3. 结论

- 旧的 75.36% / 86.6% 不能继续引用；以本页和 CI 输出为准；
- 全局 77.86% 不代表新子系统健康：`implement.py`、`control_server.py`、
  `sandbox_backend.py`、桌面入口仍是明显缺口；
- `implement.py` 的多数未覆盖行依赖 WSL 集成路径，不能只在 CI 里堆单测；
  应优先抽纯规划/合并逻辑，用 fake sandbox 覆盖；
- 后续按子系统设目标，不把全局门槛当成唯一指标。
