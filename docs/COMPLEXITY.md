---
layer: L3
doc_id: complexity
title: 项目复杂度分析
owner: 项目作者
status: active
last_reviewed: 2026-10-24
review_cadence: 每轮大改后重跑脚本
note: |
  数字由 `scripts/measure_complexity.py` 从源码生成，不要再手抄。
  重跑：`python scripts/measure_complexity.py`；引用前先确认日期。
---

# 项目复杂度分析

> 面向维护：用静态度量给出规模、复杂度分布与热点，并区分**本质复杂度**与**偶然复杂度**。
> 度量口径：Python `ast` 静态分析；圈复杂度为 McCabe 近似（判定点 + 布尔项）。
> 数据为 2026-10-24 实测（`scripts/measure_complexity.py`）。

## 1. 规模总览

| 项 | 值 |
| --- | --- |
| 源文件 | **165** |
| 源码行数 | **42,612** |
| 函数/方法 | **1,554** |
| 测试文件 / 行数 | **163 / 41,448** |
| 测试:源码 | **0.98 : 1** |
| 快反馈用例 | **1,806**（`pytest -m "not wsl and not llm"`；223 deselected） |
| 覆盖率 | **77.59%**（CI 子集；见 [COVERAGE.md](COVERAGE.md)） |

测试体量已接近源码；但覆盖率只覆盖 CI 子集，真实 WSL/bwrap 与桌面入口
不在其中，不能把全局数字当成所有子系统的健康度。

## 2. 复杂度分布（1,554 个函数）

| 指标 | 中位 | 均值 | 超阈值 |
| --- | --- | --- | --- |
| 函数长度 | **11 行** | 26.3 | >40 行：**12.7%** |
| 圈复杂度 | **3** | 4.7 | >10：**8.8%**，最大 **164** |

**结论：复杂度依然高度集中。** 约 91% 的函数圈复杂度 ≤10；复杂度主要落在
事件翻译、编排调度、工具执行与新增的 A2/A3 装配处。均值较上轮下降，但
`runtime._stream`、`web/app.create_app`、`agent_server.create_agent_app`
仍是事实上的重型函数。

## 3. 复杂度热点（当前）

| 函数 | 长度 | CC | 类型 | 评价 |
| --- | --- | --- | --- | --- |
| `runtime.py::_stream` | 819 | **164** | **真实逻辑 + 事件分发** | 唯一的“重型”函数；继续增长会难以维护 |
| `orchestrator/scheduler.py::scheduler` | 619 | 90 | **编排调度** | 并发、重试、合并、取消、实时事件集中在这里 |
| `tui/app.py::_apply` | 300 | 82 | if/elif 分发 | 事件→渲染映射，可表驱动化 |
| `graph/nodes/tools.py::run_tools` | 451 | 80 | 工具执行 | 并发只读工具 + 审批 + 结果回填 |
| `cli/app.py::render` | 185 | 57 | 分发 | 与 TUI 同构，可共享表驱动 |
| `orchestrator/graph.py::validate_tasks` | 129 | 57 | 真实逻辑 | 任务图校验，规则固有 |
| `orchestrator/fanin.py::synthesize` | 217 | 52 | fan-in 汇总 | 结构化结果合并 + worker 上下文 |
| `execution/service.py::_dispatch` | 196 | 50 | 执行契约分发 | 新增 A4 契约的 kind 分支，可表驱动化 |
| `llm/ratelimit.py::_sync_from_shared_locked` | 140 | 48 | 共享状态合并 | 多进程限流快照的校验与合并 |
| `orchestrator/plan.py::plan_tasks` | 213 | 42 | 任务图生成 | 规则拆解 + 校验/降级 |
| `runtime.py::_worker_events` | 123 | 40 | 事件翻译 | worker 事件 → 主图事件 |
| `agents/explore.py::run` | 282 | 39 | 子智能体循环 | 只读循环 + 报告 |

**两类高 CC 要区分**：

- **分发/装配型**（`_apply`/`render`/`_worker_events`）：分支彼此独立，属
  **偶然复杂度**，可表驱动化。
- **逻辑型**（`_stream`、scheduler、`run_tools`、`validate_tasks`、fan-in）：
  并发/重试/合并/事件规则本身复杂，属**本质复杂度**；应拆分而不是压平。

## 4. 最大文件

| 文件 | 行 | 函数 | 备注 |
| --- | --- | --- | --- |
| `web/app.py` | **2,464** | 112 | A2 路由 + 装配，`create_app` 2,291 行；system/providers/sessions/workspace 已拆出 |
| `runtime.py` | **2,918** | 89 | 编排中枢 + 事件翻译 + worker 协调，复杂度天然集中 |
| `cli/app.py` | **2,012** | 54 | 命令 + 渲染 |
| `tui/app.py` | **1,552** | 72 | Textual 界面 + 事件映射 |
| `agent_server.py` | **1,066** | 65 | A3 独立进程路由；RunState/SSE/上下文已拆出 |
| `sandbox/wsl_exec.py` | 1,131 | 53 | WSL 执行、并发槽位与环境探测 |
| `tools/files.py` | 1,107 | 19 | 文件工具 |
| `memory/longterm.py` | 892 | 54 | 长期记忆 |
| `config.py` | 789 | 13 | 配置模型与校验 |
| `orchestrator/implement.py` | 742 | 18 | 写 worker |

## 5. 耦合与依赖方向

```
runtime -> audit config events graph memory messages sandbox steering
           tools execution orchestrator            (中枢，合理)
web/app -> control
web/local_runtime -> runtime                      (唯一的同进程回退适配器)
agent_server -> agent_protocol event_transport workspace runtime
control -> agent_protocol event_transport
graph   -> config llm messages sandbox steering tools orchestrator.graph
orchestrator -> agents config graph.state llm sandbox steering tools
agents  -> config llm messages sandbox tools
tools   -> config diffing sandbox
execution -> sandbox
sandbox -> config diffing
cli/app -> ... runtime tui web
cli/platform -> sandbox                  (doctor/sandbox-init 的唯一适配器)
tui/web -> audit config events runtime
```

**关键结论**：

- `tui`/`web` 静态上仍没有指向 `tools`/`sandbox` 的边；
- P1.1 已把 A2↔A3 DTO、事件传输、工作区浏览下沉到 `agent_protocol` /
  `event_transport` / `workspace`，`agent_server` 不再依赖 `web` 或
  `control.agent_client`；
- P1.2 已把同进程回退隔离到 `web/local_runtime.py`；`web/app.py` 不再
  直接持有 runtime；
- CLI 的沙箱探测已收窄到 `cli/platform.py`（唯一适配器），`cli/app.py`
  不再直接 import `WslSandbox`；
- 依赖无环；A2/A3 的反向边与 web 的 runtime 直连均已消除。

## 6. 总体评价

| 维度 | 评价 |
| --- | --- |
| 规模控制 | **需关注**：`web/app.py` 2,464 行（`create_app` 2,291）、`runtime.py` 2,918 行、`agent_server.py` 1,066 行 |
| 函数粒度 | **好**：中位 11 行 / CC 3；但 >40 行占 12.5% |
| 复杂度集中度 | **可接受**：8.8% 函数 CC>10，位置可解释 |
| 真·复杂点 | `_stream`、`create_app`、`create_agent_app`、scheduler、`run_tools`、fan-in |
| 耦合 | **改善**：A2/A3 反向边已消除；同进程回退隔离在 `web/local_runtime`，CLI 适配器在 `cli/platform` |
| 可测性 | **分层**：0.98:1 测试比；CI 子集 77.59%，但 `implement.py` 19%、`control_server.py` 42%、桌面入口 0% |

## 7. 可选降复杂度动作（按性价比）

| # | 动作 | 收益 | 风险 |
| --- | --- | --- | --- |
| 1 | `web/app.py::create_app` 按领域拆 APIRouter + `Deps` | **最高**：system/providers/sessions/workspace 已拆；剩余 2,291 行 | 中（路由/认证/回退语义须不变） |
| 2 | `runtime._stream` 的 per-node 分支拆成 `_emit_*` 或 `{node: handler}` | **最高**：把最大函数降到若干小函数；已有事件流测试兜底 | 中（编排核心，须行为不变） |
| 3 | `agent_server.create_agent_app` 拆 routes / RunState / SSE primitive | 高：A3 装配可维护，减少与同进程路径分叉 | 中 |
| 4 | `orchestrator/scheduler.py::scheduler` 拆成并发派发 / 重试 / 合并三块 | 高：编排逻辑可单测、可组合 | 中 |
| 5 | `cli.render` / `tui._apply` 改表驱动 | 高：消掉最长 if/elif；两个前端可共享形状 | 低 |
| 6 | `graph/nodes/tools.py::run_tools` 拆成审批 / 并发执行 / 结果回填 | 中：工具执行路径更容易审查 | 中 |

这些动作**不应改变外部契约**；路由/事件/工具清单由 `test_layer_contracts.py`
与 golden tests 兜底。

## 8. 相关文档

- [ARCHITECTURE.md](ARCHITECTURE.md) —— 分层、不变量、扩展点
- [COVERAGE.md](COVERAGE.md) —— 覆盖率基线与子系统缺口
- [WORKFLOW.md](WORKFLOW.md) —— 工作流设计与评价
- [`scripts/measure_complexity.py`](../scripts/measure_complexity.py) —— 本文数字的事实源，改代码后重跑
