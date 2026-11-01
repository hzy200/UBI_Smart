---
layer: L3
doc_id: complexity
title: 项目复杂度分析
owner: 项目作者
status: active
last_reviewed: 2026-10-31
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
> 数据为 2026-10-31（重命名与文档流水线合入后）实测
> （`python scripts/measure_complexity.py`）。

## 1. 规模总览

| 项 | 值 |
| --- | --- |
| 源文件 | **346** |
| 源码行数 | **96,574** |
| 函数/方法 | **3,867** |
| 测试文件 / 行数 | **336 / 82,897** |
| 测试:源码 | **0.86 : 1** |
| 快反馈用例 | **3,142**（`pytest -m "not wsl and not wsl_env and not llm"`） |
| 覆盖率 | **84.70%**（CI 子集；见 [COVERAGE.md](COVERAGE.md)） |

测试体量已接近源码；但覆盖率只覆盖 CI 子集，真实 WSL/bwrap 与桌面入口
不在其中，不能把全局数字当成所有子系统的健康度。

## 2. 复杂度分布（3,867 个函数）

| 指标 | 中位 | 均值 | 超阈值 |
| --- | --- | --- | --- |
| 函数长度 | **13 行** | 21.5 | >40 行：11.7% |
| 圈复杂度 | **3** | 4.3 | >10：**7.6%**，最大 **80** |

**结论：本轮把文档流水线与工具层的超大函数拆开，最大 CC 从 127 降到 80。**
约 92% 的函数圈复杂度 ≤10；剩余复杂度主要落在 A2 装配、调度并发、
fan-in 汇总与执行契约分发 —— 都是规则或并发本身复杂，不是分支写法问题。

## 3. 复杂度热点（当前）

| 函数 | 长度 | CC | 类型 | 评价 |
| --- | --- | --- | --- | --- |
| `web/app.py::create_app` | 1,257 | **80** | **装配** | A2 的路由/依赖装配；需继续按领域拆 APIRouter（见第 7 节） |
| `orchestrator/scheduler_run.py::__init__` | 206 | 50 | **编排调度** | 调度器初始化；派发/结算已独立 |
| `execution/service.py::_dispatch` | 204 | 50 | 执行契约分发 | A4 契约的 kind 分支，可表驱动化 |
| `agents/explore.py::run` | 308 | 47 | 子智能体循环 | 只读循环 + 报告 |
| `orchestrator/document_workers.py::run` | 164 | 46 | 文档流水线 | 已从 948/127 拆到阶段函数，剩余归并逻辑 |
| `orchestrator/scheduler_run.py::run` | 189 | 41 | **编排调度** | 主循环只负责时序 |
| `runtime/event_map.py::_worker_events` | 123 | 40 | 事件翻译 | worker 事件 → 主图事件 |
| `graph/build.py::build_graph` | 463 | 37 | 图装配 | 节点/边装配；新增节点需同步 |
| `host_broker/__main__.py::_run` | 187 | 33 | 宿主代理入口 | mTLS 与服务启动分支 |
| `host_broker/client.py::_validate_hello` | 54 | 33 | 协议校验 | 握手字段校验 |
| `orchestrator/plan.py::plan_tasks` | 190 | 32 | 任务图生成 | 规则拆解 + 校验/降级 |
| `desktop/launcher.py::main` | 176 | 32 | 桌面启动 | 配置解析与服务拉起 |

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
| `orchestrator/document_workers.py` | **2,756** | 文档流水线 worker，拆函数后行数增加但复杂度下降 |
| `orchestrator/scheduler_run.py` | **1,674** | 事件驱动 DAG 调度主循环 |
| `tui/app.py` | **1,601** | Textual 界面 + 表驱动事件映射 |
| `config.py` | **1,588** | 配置模型与校验（约 400 个配置项） |
| `servers/agent_server_routes/workspace_import.py` | **1,538** | A3 的工作区导入路由（上传/校验/落盘） |
| `web/app.py` | **1,396** | A2 装配，`create_app` 1,257 行；路由在 `web/routes` |
| `orchestrator/implement.py` | **1,300** | worktree 写 worker |
| `tools/documents/extract.py` | **1,257** | 文档文本/结构抽取（已按格式拆函数） |
| `tools/documents/tool.py` | **1,242** | 文档工具装配与读写 |
| `sandbox/wsl_exec.py` | **1,171** | WSL 执行、并发槽位、取消与环境探测 |

## 5. 耦合与依赖方向

```
runtime/{core,event_map,stream} -> audit config events graph memory messages sandbox steering
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
cli/{app,commands/*} -> ... runtime tui web
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
- A2/A3 的反向边与 web 的 runtime 直连均已消除；
- **仍有两处模块级双向依赖**：`graph ↔ orchestrator`（子图装配与状态/路由契约）
  和 `tools ↔ workspace`（`tools/report.py` 与 `workspace/file_preview.py`），
  已列入后续收敛项。

## 6. 总体评价

| 维度 | 评价 |
| --- | --- |
| 规模控制 | **偏大**：`orchestrator/document_workers.py` 2,352 行、`web/app.py` 1,396 行（`create_app` 单函数 1,257）、`runtime/core.py` 906 行（runtime 包 3,280）、`control/store/` 已按聚合根拆成 7 个模块 |
| 函数粒度 | **尚可**：中位 12 行 / 均值 26.0 / CC 中位 3；>40 行占 14.2% |
| 复杂度集中度 | **需关注**：10.4% 函数 CC>10，最大 CC **127**（`document_workers.py::run`） |
| 真·复杂点 | `document_workers.run`、`validate_tasks`、`plan_tasks`、`create_app`、fan-in、`execution._dispatch`、限流快照合并 |
| 耦合 | **改善**：A2/A3 反向边已消除；宿主执行收敛到 `execution/host_ops.py`（ADR-0011）；`graph ↔ orchestrator` 与 `tools ↔ workspace` 仍待收敛 |
| 可测性 | **改善**：0.9:1 测试比；覆盖率与承重模块数字以 [COVERAGE.md](COVERAGE.md) / [METRICS.md](METRICS.md) 为准；**沙箱层另设门禁**（`.github/workflows/sandbox.yml`），桌面 GUI 仍手测 |

## 7. 可选降复杂度动作（按性价比）

| # | 动作 | 收益 | 风险 |
| --- | --- | --- | --- |
| 0 | `orchestrator/document_workers.py::run` 拆成切分 / 抽取 / 归并 / 评测四段 | **最高**：948 行 / CC 127，当前最大热点 | 中 |
| 1 | `web/app.py::create_app` 按领域拆 APIRouter + `Deps` | **部分完成**：事件流已拆到 `web/streaming.py`，`create_app` 1,509 → 1,257（CC 80）；限流/错误处理仍在内联 | 低 |
| 2 | `runtime._stream` 的 per-node 分支拆成 `_emit_*` 或 `{node: handler}` | **已完成**：819/164 → 165/27，分发表 + `_on_*` handler | 中 |
| 3 | `servers/agent_server.create_agent_app` 拆 routes / RunState / SSE primitive | 已完成：RunState/SSE/上下文与全部路由已拆；create_agent_app 321 行 | 低 |
| 4 | `orchestrator/scheduler.py::scheduler` 拆成并发派发 / 重试 / 合并三块 | **已完成首轮**：619/90 → 641/54；派发/结算/取消已独立 | 中 |
| 5 | `cli.render` / `tui._apply` 改表驱动 | **已完成**：render 185/57 → 6/1，`_apply` 300/82 → 4/2 | 低 |
| 6 | `graph/nodes/tools.py::run_tools` 拆成审批 / 并发执行 / 结果回填 | **已完成**：451/80 → 147/24 | 中 |
| 7 | `orchestrator/graph.py::validate_tasks` 按规则族拆分 | 高：224 行 / CC 101，纯校验逻辑易单测 | 低 |
| 7b | `AgentRuntime` 继续按 mixin 拆分（记忆 / 审计各一组） | **已完成首轮**：事件翻译与流式已拆到 `event_map.py` / `stream.py`，`core.py` 906 行（runtime 包 3,280）；记忆/审计仍待拆 | 中 |
| 8 | `orchestrator/fanin.py::synthesize` 拆成结果合并 / 上下文拼装 | 中：229/57 | 中 |
| 9 | `execution/service.py::_dispatch` 改 kind 分发表 | 中：203/50，分支彼此独立 | 低 |

这些动作**不应改变外部契约**；路由/事件/工具清单由 `test_layer_contracts.py`
与 golden tests 兜底。

## 8. 相关文档

- [ARCHITECTURE.md](ARCHITECTURE.md) —— 分层、不变量、扩展点
- [COVERAGE.md](COVERAGE.md) —— 覆盖率基线与子系统缺口
- [WORKFLOW.md](WORKFLOW.md) —— 工作流设计与评价
- [`scripts/measure_complexity.py`](../scripts/measure_complexity.py) —— 本文数字的事实源，改代码后重跑
