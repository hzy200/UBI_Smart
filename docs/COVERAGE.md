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

旧 `.coverage` 已不能代表当前工作区。下面的数字是 2026-10-24 补齐测试分层与
薄弱模块用例后，用 CI 同口径重跑的结果。

## 1. 口径

```bash
pytest -m "not wsl and not llm" --cov --cov-report=term-missing
```

- 不走真实 WSL/bwrap：Windows 本地没有安装 WSL 项目依赖；
- `mysql` 标记用例在未设置 `AGENT_TEST_MYSQL_URL` 时 skip；
- 本轮 `-m "not wsl and not llm"` 选中 **2,045** 个用例：**2,043** 通过、**2** 跳过；
- 总语句 **19,940**，未覆盖 **3,453**，**82.68%**（门槛 76%）。

## 2. P3 目标达成情况

| 模块 | P3 目标 | 当前 | 覆盖手段 |
|---|---:|---:|---|
| `orchestrator/implement.py` | ≥50% | **77%** | fake sandbox + 假工具/假 LLM；WSL 路径仍由 `-m wsl` 门禁 |
| `control_server.py` | ≥70% | **99%** | 参数冲突、迁移失败、握手降级、shutdown 回调 |
| `execution/sandbox_backend.py` | ≥70% | **100%** | fake sandbox / fake fs / fake snapshots |
| `desktop/setup.py` | 配置/参数/错误分支 | **100%** | 无头驱动 `SetupApi.detect/finish/pick_folder` |
| `desktop/launcher.py` | 配置/参数/错误分支 | **62%** | 启动/锁/回退/错误窗口；GUI 窗口仍手测 |

## 3. 本轮补齐情况

| 模块 | 之前 | 当前 | 覆盖手段 |
|---|---:|---:|---|
| `orchestrator/retry.py` | 21% | **100%** | 空上下文、字段截断、条目上限 |
| `desktop/supervisor.py` | 50% | **98%** | 假 Popen/握手、端口校验、进程清理 |
| `control/backup.py` | 51% | **99%** | MySQL dump/restore、密码走环境变量、失败清理 |
| `agent_server.py` | 60% | **81%** | 共享传输 SSE、缓冲裁剪、运行异常、租户上下文 |
| `agent_server_routes/state.py` | 46% | **97%** | fork/rewind/checkpoints、记忆动作、审计与会话 |
| `agent_server_routes/workspace.py` | 50% | **94%** | 文件/Git/项目、快照恢复、上传配额与删除越界 |
| `agent_server_routes/lifecycle.py` | 79% | **94%** | 流式补齐、steering 恢复、取消/关停、compact |
| `orchestrator/merge.py` | 62% | **92%** | numstat 解析、快照/回退、3-way 预演 |
| `agent_server_support.py` | 77% | **100%** | RunState / SSE / tenant 纯函数 |
| `server.py` | 0% | **92%** | 参数解析、握手、uvicorn 装配 |
| `web/local_runtime.py` | 85% | 85% | 同进程回退；配额/TTL 分支基本覆盖 |
| `cli/platform.py` | 89% | 89% | doctor/sandbox-init 适配器 |
| `agent_context.py` | 92% | 92% | A3 租户目录与 runtime 构造 |

剩余缺口集中在真实 WSL/patch 路径与长连接超时分支（`agent_server.py` 的
keepalive、`desktop/launcher.py` 的 GUI 窗口），继续靠 `-m wsl`、集成测试与
桌面手测守着，不在 CI 里堆假集成。

`execution/service.py`（89%）、`control/migrate.py`（92%）、
`control/store.py`（89%）、`web/app.py`（83%）保持稳定。

## 4. 结论

- 旧的 75.36% / 80.20% 不能继续引用；以本页和 CI 输出为准；
- 全局 **82.68%** 已超过门槛，且此前的薄弱模块均已拉到 80% 以上；
- 测试分层与运行口径见 [TESTING.md](TESTING.md)：冒烟挡启动、回归守不变量、
  增量缩短反馈、集成与全量负责深层行为；
- 覆盖率仍是可见性工具：沙箱层在 Linux CI 不执行，不许用 omit 抹平。
