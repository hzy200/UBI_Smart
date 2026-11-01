---
layer: L3
doc_id: phase-a
title: Phase A：四层架构契约与基线
owner: 项目作者
status: active
last_reviewed: 2026-10-24
review_cadence: 分层边界、控制面 API、事件/审批/运行状态契约变动时
---

# Phase A：四层架构契约与基线

Phase A 只做**契约、归属与验收基线**，不改变现有行为。目标是先把“以后怎么拆”写死，
再逐层落地；Agent 的编排面与执行面在这份方案里**功能零删减**。

> 命名约定：本文用 **A1 前端 / A2 控制面 / A3 编排面 / A4 执行面** 表示架构四层。
> 不要写成 L1–L4：项目里的 **L0–L3 是命令风险分级**，另有“文档 L 层”用法，
> 混用会让安全判定与架构讨论串味（见 [glossary.md](glossary.md)）。

## 1. 目标与边界

| 层 | 职责 | 当前代码锚点 | 目标归属 |
|---|---|---|---|
| A1 前端 | 对话、SSE 渲染、计划/任务/推理可视化、审批、文件/Git、设置 | `src/ubismart/web/static/`、`src/ubismart/tui/`、`src/ubismart/cli/` | 只调用 A2 API；桌面桥只做平台动作 |
| A2 控制面 | 认证、租户、会话目录、运行索引、配额、Provider 档案、审计查询、恢复索引 | `src/ubismart/web/app.py`、`src/ubismart/web/auth.py`、`src/ubismart/identity/tenancy.py` | 独立控制面；可接 MySQL；不执行工具 |
| A3 编排面 | LangGraph、LLM、计划、审批决定、checkpoint、steering、编排 worker、成本/限流 | `src/ubismart/runtime/`、`src/ubismart/graph/`、`src/ubismart/orchestrator/`、`src/ubismart/llm/` | Agent 服务；保留全部编排能力 |
| A4 执行面 | shell、文件 I/O、搜索、Git、测试、快照、worktree、上传落盘 | `src/ubismart/tools/`、`src/ubismart/sandbox/`、`src/ubismart/memory/` | 平台执行适配器；保留全部工具能力 |

**核心约束**：A3 与 A4 保留原 Agent 的全部功能；拆分只改调用路径，不删能力，
也不把 Agent 的执行语义搬进 A2。

## 2. 部署基线

### 2.1 Windows 11 桌面

```text
A1 pywebview / 浏览器
        │ loopback HTTP + 随机 token
A2 Windows 原生进程（或服务）+ MySQL（Windows 本机）
        │ loopback + 短期内部 token
A3 + A4 专用 WSL2 发行版（Linux 执行语义）
```

- A2 是 Windows 原生进程/服务，MySQL 只监听本机，仅 A2 访问；
- A3/A4 在 WSL 内，工作区放 ext4，不默认暴露 `/mnt`；
- A2 不直接读 WSL 工作区；文件/Git/上传/快照由 A3/A4 提供，A2 代理；
- 多实例/远程部署时，A3 事件先写共享事件传输层（建议 Redis Streams），
  A2 订阅并代理；单实例可用内存适配器；
- 保留现有 `desktop/` 启动器、watchdog、诊断与更新能力。

### 2.2 Linux

```text
A1 浏览器
        │ HTTPS/反向代理
A2 systemd + MySQL/MariaDB
        │ 本机 socket/loopback
A3 + A4 原生 Linux（bwrap/namespace/cgroup）
```

### 2.3 Standalone（保留原 Agent）

```text
CLI / TUI → A3 + A4 → 本地文件 / SQLite
```

不依赖 A2、MySQL 或前端。SQLite/本地文件是 Standalone 的一等持久化方案，
不会被 MySQL 取代；`agent run`、`agent tui`、`agent repl` 的行为与能力不变。

## 3. 数据归属

| 数据 | 归属 | 说明 |
|---|---|---|
| 用户、租户、认证会话 | A2 / MySQL | 认证与授权 |
| 会话目录、标题、删除 tombstone | A2 / MySQL | 业务目录；删除不清理审计/checkpoint |
| 运行索引、审批索引、用量汇总 | A2 / MySQL | 查询与看板；异步写入 |
| Provider 档案（base_url/model/成本/能力） | A2 / MySQL | 档案表不含 API Key |
| 设置、配额、上传元数据 | A2 / MySQL | 不含文件内容 |
| API Key 明文 | A2 的 SecretProvider | OS 密钥环/DPAPI/KMS；短期注入 A3 内存 |
| 企业 provider Key 密文 | A2 / MySQL | 仅 AES-256-GCM 密文 + 后四位；主密钥不落库 |
| LangGraph checkpoint、运行状态 | A3 | 会话恢复的 source of truth |
| 计划、任务图、pending approval、steering | A3 | 只在 Agent 侧 |
| 成本执行、provider 路由/冷却 | A3 | A2 只做汇总展示 |
| 工作区、文件快照、Git、worktree | A4 | 执行面 |
| 上传字节 | A4 | A2 只存元数据与配额 |
| 审计可信源 | A3 本地哈希链 | A2 MySQL 只建索引 |
| 审计查询索引 | A2 / MySQL | 引用 hash/run_id，不重写链 |
| SSE 热事件/重放 | 共享事件传输层 | Redis Streams 建议；按 thread 保序，consumer group |
| 事件查询索引 | A2 / MySQL | 只做查询/看板，不承担热重放 |

**MySQL 明确不存**：checkpoint 内容、工作区文件、明文 API Key、主密钥、
Agent 运行时状态；企业 provider Key 只存 AES-GCM 密文与后四位。
**A3/A4 不直连 MySQL**；A2 是唯一数据库访问方。共享事件传输层不是数据库，
只负责事件分发、短时保留与重放；MySQL 索引失败不能阻断事件流。

## 4. A2 ↔ A3 契约

### 4.1 控制 API（A2 对外，必要时代理 A3）

| 领域 | 现有入口 | Phase A 约定 |
|---|---|---|
| 会话 | `/api/sessions`、rename、delete、export | A2 拥有目录；历史由 A3 提供 |
| 运行 | `/api/run`、`/api/stream/{thread}` | A2 转发指令与 SSE，不执行图 |
| 暂停/恢复 | `/api/steer/{thread}`、`/api/resume/{thread}`、`/api/resume_steering/{thread}` | A2 只转发；A3 消费 |
| 审批 | `/api/approvals/{thread}` | A2 展示；决定回 A3 |
| 状态 | `/api/status/{thread}` | A3 产出；A2 不伪造 |
| 文件/Git | `/api/files`、`/api/file`、`/api/git` | A2 代理 A3/A4，不直接读工作区 |
| 快照 | `/api/snapshots`、`/api/diff`、`/api/restore` | A2 代理；A4 留底/恢复 |
| checkpoint | `/api/checkpoints`、`/api/fork`、`/api/rewind` | A3 所有 |
| 恢复 | `/api/recovery` | A2 索引 + A3 状态 |
| Provider | `/api/providers`、`/api/provider-profiles` | A2 管理档案与密钥 |

目标内部接口（Phase B 落地，名称可调整）：

```text
POST /agent/runs                 启动/恢复一次运行
GET  /agent/runs/{run_id}/events SSE，带 seq
POST /agent/runs/{run_id}/resume
POST /agent/runs/{run_id}/steer
GET  /agent/runs/{run_id}/state
GET  /agent/sessions/{id}/history
GET  /agent/workspaces/{id}/files | file | git | snapshots
```

每个请求带 `tenant_id + session_id + run_id + idempotency_key`；同一会话只允许
一个活跃 run，A3 持有租约。A2 不缓存并伪造 Agent 状态。

### 4.2 事件与 SSE

事件唯一契约仍是 `src/ubismart/events.py`（28 个领域事件）：

- 规划/步进：`PlanCreated`、`PlanRevised`、`StepStarted`、`StepFinished`；
- 模型/思考：`AssistantToken`、`ReasoningToken`；
- 工具/文件：`ToolCallStarted`、`ToolCallFinished`、`FileChanged`；
- 质量：`Verification`、`ReviewFinished`、`RepairStarted`；
- 编排：`WorkflowSelected`、`TaskGraphCreated`、`TaskGraphExtended`、
  `WorkerStarted`、`WorkerFinished`、`SubAgentStarted`、`SubAgentFinished`、
  `OrchestrationFinished`、`WorkflowRevised`；
- 运行/成本/干预：`RunStarted`、`RunFinished`、`RunFailed`、`CostReport`、
  `ContextTrimmed`、`SteeringApplied`、`ApprovalRequested`。

SSE 约定（当前由 `src/ubismart/web/app.py` 实现，Phase A 冻结）：

- 帧格式：`id: <seq>` + `data: <event json>`；15 秒无事件发 `: keepalive`；
- `seq` 在同一 thread 的 resume 前后保持单调；`after` 或 `Last-Event-ID` 重放；
- `ApprovalRequested` 或 `SteeringApplied(action=pause)` 收尾表示**挂起**，
  此时不发 `RunFinished`；
- `RunFinished` / `RunFailed` 表示本轮结束；失败事件必须带错误信息；
- 默认是单进程内存态（每流上限 8 MB、每人保留 8 个完成流）；
  配置 Redis 后事件同时写共享传输，A2 按 `seq` 重放（当前轮询式）；
  consumer group/积压监控待补；MySQL 只做索引，不承担 SSE 热重放。

审批字段冻结：`request_id / tool / command / level / reason`；决定通过
`/api/resume` 的 `{request_id: bool}` 回传，A2 不自行判定。

当前可观测运行状态：`running`、`suspended`（审批/暂停）、`finished`、`failed`。
`queued`、`cancelled` 是目标扩展，**尚未由现有代码发出**，Phase B 引入时必须
同时更新事件契约与前端。

## 5. Agent 功能 parity 清单

拆分后 A3/A4 必须逐项保留；任何一项只能改接口，不能删能力。

| 能力域 | 必须保留的内容 | 归属 |
|---|---|---|
| 执行路径 | direct / orchestrated；planner → act → verify → review → advance/replan | A3 |
| 子智能体 | explore/review 委派、独立 messages、工具白名单、预算 | A3 |
| 编排 | 任务 DAG、依赖/blocked、动态任务、fan-in、worker 并发 | A3 |
| 写编排 | worktree、write_scope、patch 校验、串行合并、合并后验证 | A3/A4 |
| 审批与分级 | L0–L3、allow rules、security profile、fail closed | A3（决定）+ A4（强制） |
| 干预 | pause/cancel/add/replan、跨挂起恢复 | A3 |
| 状态 | checkpoint、fork/rewind/compact、恢复提示、悬空 tool_calls 修复 | A3 |
| 工具 | shell、文件读写/编辑/批量/移动/删除、搜索、符号、Git、测试、依赖、审查 | A4 |
| 回滚 | 写前快照、撤销、逐文件/整轮恢复 | A4 |
| 记忆 | 项目/全局/向量记忆、pin/priority | A3/A4 |
| 观测 | 审计哈希链、成本/用量、provider 路由/冷却/fallback、并发池 | A3 |
| 沙箱 | WSL/Linux 本地执行、路径双重校验、bwrap、资源上限、工作区锁 | A4 |

**Standalone 证据**：`src/ubismart/cli/` 与 `src/ubismart/tui/app.py`
直接依赖 `AgentRuntime` 与事件，不经过 A2；Phase A 不要求它们改用 MySQL。

## 6. 前端能力基线

当前已实现（Phase A 保持）：

- 主题（深/浅）、字号、密度；会话搜索/改名/删除/导出；
- SSE 流式 Markdown、代码块、重连、`Last-Event-ID`；
- 计划、工具、任务、文件、文件树、Git、记忆、审计；
- 审批弹窗、pause/resume/cancel/add/replan；
- 推理面板（`ReasoningToken`，默认折叠）、成本行、上传配额、恢复中心；
- Provider/密钥、诊断包、更新检查、系统通知、文件树/Git 只读。

Phase A 目标清单（Phase E 实现）：

- 主题跟随系统、字体族、密度持久化与可访问性；
- 会话虚拟滚动、长 reasoning/工具输出折叠、diff 折叠（已有）；
- 按节点/步骤组织的推理时间线，区分“模型返回的 reasoning”与“系统计划轨迹”；
- 生成中停止/重试、复制消息、代码复制、连接状态；
- 命令面板、键盘快捷键、空态/错误态/加载态；
- 文件树按需加载（已有）、Git diff（已有）、上传列表/配额（已有）；
  图片/文本预览仍待补；
- 构建方式固定为 **静态 SPA + esbuild/TypeScript**：`app.js` 拆模块、
  事件/API 类型化，保留 CSP 与静态托管，不引入前端框架。

约束：A1 只消费 A2 API 与领域事件，**不得导入 `tools/` 或 `sandbox/`**；
桌面桥（pywebview）只做选文件夹、VS Code、诊断、更新等平台动作。

## 7. 安全与性能边界

### 安全

- A2 永远不执行工具；A3 决定审批；A4 二次强制路径/能力限制；
- A1 不接触 API Key；A2 的 SecretProvider 是唯一托管方，A3 只拿短期凭据；
  桌面用 OS 密钥环，服务器用 Vault/KMS/systemd-creds；WSL 没有 Secret Service
  daemon 时不落盘，由 A2 按运行注入；
- MySQL 只存业务数据，不存明文密钥/checkpoint/文件；企业模式仅存
  AES-GCM 密文+后四位；仅本机、最小权限；
- A2↔A3 仅 loopback + 短期 token；远程部署用 mTLS/TLS；
- Windows：专用 WSL、非 root、`appendWindowsPath=false`、高风险 profile 关 interop；
- Linux：非 root + bwrap/namespace/cgroup/seccomp；不可用时 fail closed；
- 审计单写者：A3 本地链为可信源，A2 只建索引，不重写链。

### 性能

- A2↔A3 只传控制/事件；工具 I/O 不跨层；
- MySQL 不进 SSE 热路径；事件/用量异步批量写；
- Windows 工作区用 ext4，禁止每次工具调用跨 `/mnt`；
- A1 长会话虚拟滚动、增量 Markdown；A3 事件批量渲染；
- 目标指标：SSE 重连不丢事件、工具额外延迟可度量、MySQL 写不阻塞流。

## 8. Phase A 退出条件

- [x] A1–A4 职责、部署基线与数据归属写入文档；
- [x] Agent parity 清单覆盖原有能力，standalone 路径明确；
- [x] A2/A3 控制 API、事件、审批、运行状态契约冻结；
- [x] 关键路由/事件/会话字段/工具清单有契约测试；
- [x] 现有行为未改变；Phase B 可以只做“控制面抽取”而不改 Agent 语义。

**Phase B 决策（已确认，见 [ADR-0010](adr/0010-four-layer-phase-b-decisions.md)）**：

1. **A2 在各平台都跑原生进程**：Windows 用服务/进程 + 本机 MySQL，
   Linux 用 systemd + MySQL/MariaDB；WSL 只承载 A3/A4。差异不在稳定性，
   而在宿主集成能力（服务生命周期、事件日志、ACL、DPAPI/KMS、systemd-creds）。
2. **SecretProvider 分两档**：桌面用 OS 密钥环；服务器用 Vault/KMS/
   systemd-creds。WSL 通常没有 Secret Service daemon，桌面密钥由 A2 持有，
   按运行短期注入 A3 内存，不落 WSL 磁盘。
3. **MySQL 只做业务索引；新增共享事件传输层**（建议 Redis Streams）：
   A3 发布事件，A2 订阅/代理并支持按 `seq` 重放；MySQL 不承担热重放。
   单实例可用内存适配器过渡，但契约保持同一套。
4. **SQLite 保留为一等公民**：Standalone CLI/TUI、开发、测试、eval 继续用
   本地文件/SQLite；只有 A2 使用 MySQL，不强制 Agent 依赖数据库。
5. **前端维持静态 SPA + esbuild/TypeScript**，不引入框架：`app.js` 拆分模块、
   事件/API 类型化，保留 CSP 与静态托管；以构建步骤换可维护性，不付框架代价。

## 9. 明确不做

- 不把 checkpoint/文件/明文 API Key 放进 MySQL；企业 provider Key 只放密文；
- 不让 A2 执行工具或直接读写工作区；
- 不删减原 Agent 能力；
- 不在 Phase A 引入原生 Windows 工具层、Windows Server、MCP 市场；
- 不把四层拆成多个微服务。
