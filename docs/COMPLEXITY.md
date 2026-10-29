---
layer: L3
doc_id: complexity
title: 项目复杂度分析
owner: 项目作者
status: active
last_reviewed: 2026-10-28
review_cadence: 每轮大改后重跑脚本
note: |
  数字由 `scripts/measure_complexity.py` 从源码生成，不要再手抄。
  重跑：`python scripts/measure_complexity.py`；引用前先确认日期。
  CI 会用 `python scripts/measure_complexity.py --check docs/COMPLEXITY.md`
  反查本文的关键数字，对不上就红。
---

# 项目复杂度分析

> 面向维护：用静态度量给出规模、复杂度分布与热点，并区分**本质复杂度**与**偶然复杂度**。
> 度量口径：Python `ast` 静态分析；圈复杂度为 McCabe 近似（判定点 + 布尔项）。
> 数据为 2026-10-28（P0/P1 交付就绪改动后）实测
> （`python scripts/measure_complexity.py`）。

## 1. 规模总览

| 项 | 值 |
| --- | --- |
| 源文件 | **263** |
| 源码行数 | **73,336** |
| 函数/方法 | **2,817** |
| 测试文件 / 行数 | **285 / 69,626** |
| 测试:源码 | **0.95 : 1** |
| 快反馈用例 | **3,002**（`pytest -m "not wsl and not wsl_env and not llm"`；本机 1 条 skip，其余通过） |
| 覆盖率 | **83.34%**（CI 子集；见 [COVERAGE.md](COVERAGE.md)） |

测试体量已接近源码；但覆盖率只覆盖 CI 子集，真实 WSL/bwrap 与桌面入口
不在其中，不能把全局数字当成所有子系统的健康度。

## 2. 复杂度分布（2,816 个函数）

| 指标 | 中位 | 均值 | 超阈值 |
| --- | --- | --- | --- |
| 函数长度 | **12 行** | 23.8 | >40 行：**12.7%** |
| 圈复杂度 | **3** | 4.4 | >10：**8.8%**，最大 **66** |

**结论：P2 把最大的几个分发函数拆开后，最大 CC 从 164 降到 63。**
约 91% 的函数圈复杂度 ≤10；剩余复杂度主要落在任务图校验、调度并发、
fan-in 汇总与执行契约分发 —— 都是规则或并发本身复杂，不是分支写法问题。

## 3. 复杂度热点（当前）

| 函数 | 长度 | CC | 类型 | 评价 |
| --- | --- | --- | --- | --- |
| `web/app.py::create_app` | 1,358 | **66** | **装配** | A2 的路由/依赖装配；路由已拆到 `web/routes`，但 `create_app` 本身仍是大函数（见第 7 节） |
| `orchestrator/graph.py::validate_tasks` | 129 | **57** | 真实逻辑 | 任务图校验，规则固有；可按规则族拆函数 |
| `orchestrator/fanin.py::synthesize` | 217 | 52 | fan-in 汇总 | 结构化结果合并 + worker 上下文 |
| `execution/service.py::_dispatch` | 203 | 50 | 执行契约分发 | A4 契约的 kind 分支，可表驱动化 |
| `llm/ratelimit.py::_sync_from_shared_locked` | 140 | 48 | 共享状态合并 | 多进程限流快照的校验与合并 |
| `web/routes/sessions.py::sessions` | 134 | 45 | 列表装配 | 会话列表的多路过滤与排序 |
| `orchestrator/plan.py::plan_tasks` | 213 | 42 | 任务图生成 | 规则拆解 + 校验/降级 |
| `runtime.py::_worker_events` | 123 | 40 | 事件翻译 | worker 事件 → 主图事件 |
| `agents/explore.py::run` | 282 | 39 | 子智能体循环 | 只读循环 + 报告 |
| `cli/app.py::memory` | 174 | 36 | 命令实现 | 记忆命令的多分支参数处理 |
| `orchestrator/scheduler_run.py::run` | 158 | 34 | **编排调度** | 原 `scheduler.py::scheduler`；派发/结算/取消已抽成 `dispatch_ready`、`drain_completed`、`abort_pending`，主循环只负责时序 |
| `host_broker/__main__.py::_run` | 187 | 33 | CLI 子命令 | `--run` 的命令族分派 |

P2 拆分后的关键分发函数：

| 函数 | 长度 | CC | 说明 |
| --- | --- | --- | --- |
| `graph/nodes/tools.py::run_tools` | 147 | 24 | 原 451/80；委派批次 93/13、只读批次 109/17、单次调用 60/16 |
| `cli/renderer.py::CliRenderer` | — | — | 原 `cli/app.render` 185/57 → 装配 6/1 + 21 个单事件 handler |
| `tui/app.py::_apply` | 4 | 2 | 原 300/82 → 表驱动 + 一组 `_on_*` handler |
| `orchestrator/implement.py::run` | 429 | 22 | 原 446/29；消息组装与单次工具调用已抽出 |

**两类高 CC 要区分**：

- **分发/装配型**（`_dispatch`、`_worker_events`、`_provider_specs`）：分支彼此
  独立，属**偶然复杂度**，可继续表驱动化。
- **逻辑型**（`validate_tasks`、scheduler、fan-in、ratelimit 合并）：
  规则/并发本身复杂，属**本质复杂度**；应继续拆函数而不是压平。

## 4. 最大文件

| 文件 | 行 | 备注 |
| --- | --- | --- |
| `runtime.py` | **2,962** | 编排中枢 + 事件翻译；`_stream` 已拆成主干 + 分发表 + 处理器，快照/回滚/差异/上传留底已移到 `runtime_snapshots.py` |
| `cli/app.py` | **1,915** | 命令；事件渲染已移到 `cli/renderer.py` |
| `web/app.py` | **1,670** | A2 装配，`create_app` 1,358 行；路由已拆到 `web/routes`，服务已拆到 `web/services` |
| `tui/app.py` | **1,601** | Textual 界面 + 表驱动事件映射 |
| `agent_server_routes/imports.py` | 1,375 | A3 的工作区导入路由（上传/校验/落盘） |
| `config.py` | 1,217 | 配置模型与校验 |
| `control/store.py` | 1,183 | 控制面存储；计费/配额已改为原子增量 |
| `sandbox/wsl_exec.py` | 1,171 | WSL 执行、并发槽位、取消与环境探测 |
| `tools/files.py` | 1,150 | 文件工具 |
| `control/workspaces.py` | 1,043 | 控制面工作区存储与注册表 |

## 5. 耦合与依赖方向

```
runtime -> audit config events graph memory messages sandbox steering
           tools execution orchestrator            (中枢，合理)
web/app -> control
web/local_runtime -> runtime                      (唯一的同进程回退适配器；延迟导入)
agent_server -> agent_protocol event_transport workspace runtime
control -> agent_protocol event_transport
graph   -> config llm messages sandbox steering tools orchestrator.graph
orchestrator -> agents config execution.host_ops graph.state llm sandbox steering tools
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
| 规模控制 | **偏大**：`runtime.py` 2,950 行、`web/app.py` 1,443 行（`create_app` 单函数 1,215）、`cli/app.py` 1,896 行 |
| 函数粒度 | **好**：中位 12 行 / CC 3；>40 行占 12.7% |
| 复杂度集中度 | **改善**：8.9% 函数 CC>10，最大 CC 164 → 57 |
| 真·复杂点 | `validate_tasks`、`create_app`、fan-in、`execution._dispatch`、限流快照合并 |
| 耦合 | **改善**：A2/A3 反向边已消除；宿主执行收敛到 `execution/host_ops.py`（ADR-0011） |
| 可测性 | **改善**：0.92:1 测试比；CI 子集 81.33%，`implement.py` 19% → 77%、`control_server.py` 39% → 99%、`sandbox_backend.py` 44% → 100%；**沙箱层另设门禁**（`.github/workflows/sandbox.yml`），桌面 GUI 仍手测 |

## 7. 可选降复杂度动作（按性价比）

| # | 动作 | 收益 | 风险 |
| --- | --- | --- | --- |
| 1 | `web/app.py::create_app` 按领域拆 APIRouter + `Deps` | **回归了**：路由 + services 已拆出，但 `create_app` 又长回 1,215 行（CC 56），是当前**装配侧**最大热点 | 低 |
| 2 | `runtime._stream` 的 per-node 分支拆成 `_emit_*` 或 `{node: handler}` | **已完成**：819/164 → 165/27，分发表 + `_on_*` handler | 中 |
| 3 | `agent_server.create_agent_app` 拆 routes / RunState / SSE primitive | 已完成：RunState/SSE/上下文与全部路由已拆；create_agent_app 321 行 | 低 |
| 4 | `orchestrator/scheduler.py::scheduler` 拆成并发派发 / 重试 / 合并三块 | **已完成首轮**：619/90 → 641/54；派发/结算/取消已独立 | 中 |
| 5 | `cli.render` / `tui._apply` 改表驱动 | **已完成**：render 185/57 → 6/1，`_apply` 300/82 → 4/2 | 低 |
| 6 | `graph/nodes/tools.py::run_tools` 拆成审批 / 并发执行 / 结果回填 | **已完成**：451/80 → 147/24 | 中 |
| 7 | `orchestrator/graph.py::validate_tasks` 按规则族拆分 | 高：当前最大 CC（129/57），纯校验逻辑易单测 | 低 |
| 7b | `AgentRuntime` 继续按 mixin 拆分（记忆 / 审计 / 事件翻译各一组） | 中：`runtime.py` 仍 2,950 行；快照组已拆出做样板 | 中 |
| 8 | `orchestrator/fanin.py::synthesize` 拆成结果合并 / 上下文拼装 | 中：217/52 | 中 |
| 9 | `execution/service.py::_dispatch` 改 kind 分发表 | 中：196/50，分支彼此独立 | 低 |

这些动作**不应改变外部契约**；路由/事件/工具清单由 `test_layer_contracts.py`
与 golden tests 兜底。

## 8. 相关文档

- [ARCHITECTURE.md](ARCHITECTURE.md) —— 分层、不变量、扩展点
- [COVERAGE.md](COVERAGE.md) —— 覆盖率基线与子系统缺口
- [WORKFLOW.md](WORKFLOW.md) —— 工作流设计与评价
- [`scripts/measure_complexity.py`](../scripts/measure_complexity.py) —— 本文数字的事实源，改代码后重跑
