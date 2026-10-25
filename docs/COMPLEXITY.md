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
> 数据为 2026-10-24 P3 覆盖率闭环后实测（`scripts/measure_complexity.py`）。

## 1. 规模总览

| 项 | 值 |
| --- | --- |
| 源文件 | **179** |
| 源码行数 | **43,789** |
| 函数/方法 | **1,653** |
| 测试文件 / 行数 | **178 / 45,362** |
| 测试:源码 | **1.04 : 1** |
| 快反馈用例 | **2,045**（`pytest -m "not wsl and not llm"`；2,043 通过 / 2 skip） |
| 覆盖率 | **82.68%**（CI 子集；见 [COVERAGE.md](COVERAGE.md)） |

测试体量已接近源码；但覆盖率只覆盖 CI 子集，真实 WSL/bwrap 与桌面入口
不在其中，不能把全局数字当成所有子系统的健康度。

## 2. 复杂度分布（1,649 个函数）

| 指标 | 中位 | 均值 | 超阈值 |
| --- | --- | --- | --- |
| 函数长度 | **11 行** | 24.7 | >40 行：**13.2%** |
| 圈复杂度 | **3** | 4.4 | >10：**8.8%**，最大 **57** |

**结论：P2 把最大的几个分发函数拆开后，最大 CC 从 164 降到 57。**
约 91% 的函数圈复杂度 ≤10；剩余复杂度主要落在任务图校验、调度并发、
fan-in 汇总与执行契约分发 —— 都是规则或并发本身复杂，不是分支写法问题。

## 3. 复杂度热点（当前）

| 函数 | 长度 | CC | 类型 | 评价 |
| --- | --- | --- | --- | --- |
| `orchestrator/graph.py::validate_tasks` | 129 | **57** | 真实逻辑 | 任务图校验，规则固有；可按规则族拆函数 |
| `orchestrator/scheduler.py::scheduler` | 641 | 54 | **编排调度** | 派发/结算已抽成 `dispatch_ready`(70/19)、`drain_completed`(54/13)、`abort_pending`(13/3)，主循环仍负责时序 |
| `orchestrator/fanin.py::synthesize` | 217 | 52 | fan-in 汇总 | 结构化结果合并 + worker 上下文 |
| `execution/service.py::_dispatch` | 196 | 50 | 执行契约分发 | A4 契约的 kind 分支，可表驱动化 |
| `llm/ratelimit.py::_sync_from_shared_locked` | 140 | 48 | 共享状态合并 | 多进程限流快照的校验与合并 |
| `orchestrator/plan.py::plan_tasks` | 213 | 42 | 任务图生成 | 规则拆解 + 校验/降级 |
| `runtime.py::_worker_events` | 123 | 40 | 事件翻译 | worker 事件 → 主图事件 |
| `agents/explore.py::run` | 282 | 39 | 子智能体循环 | 只读循环 + 报告 |
| `cli/app.py::memory` | 174 | 36 | 命令实现 | 记忆命令的多分支参数处理 |
| `llm/deepseek.py::_provider_specs` | 88 | 32 | 装配 | provider 规格矩阵 |
| `graph/nodes/steering.py::_apply_commands` | 159 | 28 | steering 应用 | 命令族分派 |
| `runtime.py::_stream` | 165 | 27 | 事件流主干 | 已从 819/164 拆成主干 + 分发表 + 收尾/异常处理器 |

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
| `runtime.py` | **3,031** | 编排中枢 + 事件翻译；P2 把 `_stream` 拆成主干 + 分发表 + 处理器，行数略增但单函数大幅变小 |
| `cli/app.py` | **1,801** | 命令；事件渲染已移到 `cli/renderer.py` |
| `tui/app.py` | **1,581** | Textual 界面 + 表驱动事件映射 |
| `sandbox/wsl_exec.py` | 1,131 | WSL 执行、并发槽位与环境探测 |
| `tools/files.py` | 1,107 | 文件工具 |
| `web/app.py` | **895** | A2 装配，`create_app` 758 行；路由已拆到 web/routes，服务已拆到 web/services |
| `memory/longterm.py` | 892 | 长期记忆 |
| `orchestrator/implement.py` | **819** | 写 worker；消息组装与单次工具调用已抽出 |
| `config.py` | 789 | 配置模型与校验 |
| `orchestrator/scheduler.py` | 783 | 调度；派发/结算/取消已拆成独立函数 |

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
| 规模控制 | **可接受**：`runtime.py` 3,031 行（单函数已拆小）、`web/app.py` 895 行、`cli/app.py` 1,801 行 |
| 函数粒度 | **好**：中位 11 行 / CC 3；>40 行占 13.2% |
| 复杂度集中度 | **改善**：8.8% 函数 CC>10，最大 CC 164 → 57 |
| 真·复杂点 | `validate_tasks`、scheduler 主循环、fan-in、`execution._dispatch`、限流快照合并 |
| 耦合 | **改善**：A2/A3 反向边已消除；宿主执行收敛到 `execution/host_ops.py`（ADR-0011） |
| 可测性 | **改善**：1.04:1 测试比；CI 子集 82.68%，`implement.py` 19% → 77%、`control_server.py` 39% → 99%、`sandbox_backend.py` 44% → 100%，重试/备份/合并等薄弱模块已补到 92%+；桌面 GUI 仍手测 |

## 7. 可选降复杂度动作（按性价比）

| # | 动作 | 收益 | 风险 |
| --- | --- | --- | --- |
| 1 | `web/app.py::create_app` 按领域拆 APIRouter + `Deps` | 已完成：路由 + services 拆出，`create_app` 758 行 | 低 |
| 2 | `runtime._stream` 的 per-node 分支拆成 `_emit_*` 或 `{node: handler}` | **已完成**：819/164 → 165/27，分发表 + `_on_*` handler | 中 |
| 3 | `agent_server.create_agent_app` 拆 routes / RunState / SSE primitive | 已完成：RunState/SSE/上下文与全部路由已拆；create_agent_app 321 行 | 低 |
| 4 | `orchestrator/scheduler.py::scheduler` 拆成并发派发 / 重试 / 合并三块 | **已完成首轮**：619/90 → 641/54；派发/结算/取消已独立 | 中 |
| 5 | `cli.render` / `tui._apply` 改表驱动 | **已完成**：render 185/57 → 6/1，`_apply` 300/82 → 4/2 | 低 |
| 6 | `graph/nodes/tools.py::run_tools` 拆成审批 / 并发执行 / 结果回填 | **已完成**：451/80 → 147/24 | 中 |
| 7 | `orchestrator/graph.py::validate_tasks` 按规则族拆分 | 高：当前最大 CC（129/57），纯校验逻辑易单测 | 低 |
| 8 | `orchestrator/fanin.py::synthesize` 拆成结果合并 / 上下文拼装 | 中：217/52 | 中 |
| 9 | `execution/service.py::_dispatch` 改 kind 分发表 | 中：196/50，分支彼此独立 | 低 |

这些动作**不应改变外部契约**；路由/事件/工具清单由 `test_layer_contracts.py`
与 golden tests 兜底。

## 8. 相关文档

- [ARCHITECTURE.md](ARCHITECTURE.md) —— 分层、不变量、扩展点
- [COVERAGE.md](COVERAGE.md) —— 覆盖率基线与子系统缺口
- [WORKFLOW.md](WORKFLOW.md) —— 工作流设计与评价
- [`scripts/measure_complexity.py`](../scripts/measure_complexity.py) —— 本文数字的事实源，改代码后重跑
