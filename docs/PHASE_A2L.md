---
layer: L3
doc_id: phase-a2l
title: Phase A2L：宿主能力代理（Phase 1 接口与文件清单）
owner: 项目作者
status: active
last_reviewed: 2026-10-25
review_cadence: A2L 协议、授权模型、工作区类型或安全档变动时
---

# Phase A2L：宿主能力代理（Phase 1 接口与文件清单）

本阶段把 [RFC-0002](rfc/0002-native-execution-domain-and-grant-model.md) 的批准结论
落成可实现的契约。决策与代价见 [ADR-0015](adr/0015-host-capability-broker.md)；
安全边界见 [THREAT_MODEL.md](THREAT_MODEL.md) 的 T4。

**Phase 1 只做“契约 + A2L 只读骨架”**：建立绑定客户端生命周期的 A2L 进程、host/sandbox 双工作区
引用、A4 到 A2L 的 typed RPC、本地 grant/ZX 判定、只读文件与工具链能力注册、
本地工作区映射、本地审计与心跳降级。**不开放宿主写、编译执行、安装，也不改默认后端。**

部署边界：**客户端只有 A1 与 A2L；A2/A3/A4 全在服务器。** A2L 是客户端执行域，
**绑定客户端生命周期**：随客户端启动/退出，不独立常驻系统、不注册系统服务。
Phase 1 以客户端子进程/托盘形式运行，企业设备注册不在 P1。

## 1. 目标与非目标

### 目标

- A2L 绑定客户端生命周期：随客户端启动/退出；客户端运行期间单实例、
  托盘可见；不独立常驻系统，不注册系统服务。
- A4 能按 `WorkspaceRef.kind` 把执行请求路由到 `sandbox`（服务端 WSL）或
  `host`（客户端 A2L）。
- A2L 以**只读**能力接入：`host.list_dir`、`host.stat`、`host.read_text`、
  `host.toolchains`、`host.env.inspect`、`host.health`，以及本地工作区注册
  `host.workspace.register/list/forget`。
- host 绝对路径只存 A2L 本地 SQLite；A2 只存
  `workspace_id + root_handle + a2l_id`。
- grant 与 ZX 在客户端判定；服务端只传 `grant_id`，不持有 scope。
- A2L 只外连，断线时 A4 降级到 sandbox，不阻塞。
- 默认关闭（`AGENT_HOST_BROKER_ENABLED=false`），现有行为零变化。

### Phase 1 工具接入范围

只接 `file_read`、`list_dir`、`find_files` 三个只读工具。`search_code`、
shell、写、删除、快照、编译、安装全部留到后续阶段，避免一次性改造所有
路径语义与审批路径。

### 非目标

- 宿主写/删除/快照/编译/安装（Phase 2–4）。
- 跨 kind 编译或验证；host 工作区不通过 `/mnt` 或 `\\wsl$` 暴露给 WSL。
- A2L 自动更新、生产服务化、企业设备注册/批量分发（Phase 4）。
- 新的事件/审计契约生成（Phase 1 只落最小 `host_op` 审计，事件在 Phase 3 补齐）。
- 提示注入防护本身；本阶段只限制注入成功后的能力面。

## 2. 拓扑与信任边界

```text
客户端 · 用户电脑                        服务端 · 企业服务器
┌──────────────────────────┐            ┌──────────────────────────┐
│ A1 前端                  │  HTTP/SSE  │ A2 控制面                │
│ A2L 宿主能力代理         │ ◀────────▶ │ A3 编排面                │
│   （随客户端启停）       │  只外连    │ A4 执行面                │
│   host 工作区（NTFS）    │            │   └─ sandbox 工作区(WSL) │
│   本地 grant/ZX/审计     │            │   └─ sandbox 工作区(WSL) │
└──────────────────────────┘            └──────────────────────────┘
```

- A2L 随客户端启动/退出，不独立常驻系统；A2/A3/A4 不在客户端部署。
- `host` 工作区在客户端 NTFS，由 A2L 执行；`sandbox` 工作区在服务端 WSL ext4，
  由 A4 执行；两者不跨挂载。
- A2L 只主动外连（桌面 loopback、企业 mTLS），不开入站端口。

信任规则：

1. A3 的 `ExecutionRequest` 是**请求**；A2L 的本地策略是最终判定。
2. A2 只持有 `grant_id`、`a2l_id`、`root_handle` 与审计索引，不持有 grant scope
   或 host 绝对路径。
3. A1 不直接调用 A2L 能力 API；本地同意 UI 由 A2L 自己渲染。
4. A2L 不导入 `tools/`、`sandbox/`、`graph/`、`runtime.py`；它是独立进程。
5. A2L 不得以 LocalSystem/root 运行；按交互用户会话隔离 grant 与工作区映射。

### 2.1 常驻形态与生命周期

| 项 | Phase 1 | 生产态（Phase 4） |
|---|---|---|
| 形态 | 客户端子进程/托盘 | 随客户端安装包分发的子进程/托盘 |
| 生命周期 | **随客户端启动/退出**；客户端退出即终止 | 同左；**不注册系统服务、不独立开机自启** |
| 单实例 | 本地文件锁 + 命名事件 | 同左 |
| 崩溃恢复 | 客户端监督重启 A2L；指数退避 | 同左 + 告警 |
| 健康 | `host.health` + 心跳 | 同左 + 企业监控 |
| 离线 | 托盘可见；A4 降级 sandbox | 同左 |
| 更新 | 随客户端重启/升级 | 随客户端签名包更新，可回滚 |
| 权限 | **lowpriv + ACL**（`user-t1` 仅本地开发） | 同左；AppContainer 仅受限只读档 |
| 多用户 | 每个客户端实例一个 A2L | 同左；不共享 grant/映射 |

Phase 1 的“常驻”只指**客户端运行期间**；客户端退出后 A2L 必须退出。
**不得把 user-t1 子进程当成企业安全档**，也不得把 A2L 做成独立系统服务。
企业批量部署、设备证书、客户端安装包更新在 Phase 4。

## 3. Phase 1 接口契约

### 3.1 工作区与执行目标

```python
# workspace/model.py
WorkspaceKind = Literal["host", "sandbox"]

@dataclass(frozen=True, slots=True)
class WorkspaceRef:
    workspace_id: str
    kind: WorkspaceKind
    root_handle: str          # 不透明句柄；跨层不传绝对路径
    display_name: str = ""

# execution/contracts.py（只增字段）
ExecutionTarget = Literal["auto", "host", "sandbox"]

@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    op_id: str
    kind: str
    workspace_id: str = ""
    workspace_kind: WorkspaceKind = "sandbox"
    target: ExecutionTarget = "auto"
    grant_id: str = ""
    run_id: str = ""
    tool_call_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    timeout_s: float = 60.0
    capture_limit: int = 0
```

**Root handle 归属**

- A2L 本地 SQLite（`host_broker/store.py`）保存：
  `workspace_id, a2l_id, root_handle, absolute_root, display_name, created_at, last_seen`。
- A2 只保存 `workspace_id + root_handle + a2l_id`；`root_path` 对 host 工作区留空。
- A2 的 `workspaces.workspace_id` **必须沿用 A2L 返回的 workspace_id**：A2L
  用它查本地绝对路径，A2 另生成 UUID 会导致 `WORKSPACE_HANDLE_UNKNOWN`。
- 桌面 host 模式默认开启工作区注册表；启动配置的本机目录会登记成默认项目。
- 会话创建后工作区绑定不可变：`workspace_kind/a2l_id/root_handle` 随会话
  落库；本地 catalog 丢失时从 `workspaces` 表恢复，绝不退回 sandbox 路径。
- 目录选择属于人工操作：`host.workspace.register` 的 RPC 超时放宽到 600 秒，
  普通文件读取仍保持短超时。
- host 工作区由客户端本地选目录后注册；A2 不接受任意主机路径。
- `root_handle` 是不透明随机 ID，不是路径编码；A2L 重启后凭本地映射恢复。
- 映射丢失/损坏时，A2 侧工作区标记 `unavailable`，A2L 要求用户重新选目录，
  生成新 handle；不猜测、不按旧路径重建。

**P1 根目录限制**

- 只接受**本地固定 NTFS** 根（`GetDriveTypeW == DRIVE_FIXED`）。
- 拒绝 OneDrive、云占位文件（`FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS` /
  `OFFLINE`）、网络盘/映射盘、UNC、WSL/VM 共享、reparse 根、设备路径。
- 注册时用 handle 级校验；路径字符串看起来像 `C:\...` 不够。
- 敏感根（`Documents`、`Desktop`、`OneDrive`、浏览器 profile、`.ssh`、
  `.aws`、`.kube` 等）不允许作为普通 scope；必须 `sensitive=True` 逐次确认。
- `%USERPROFILE%`、盘符根、`C:\Users` 不允许作为 scope。

路由规则：

| `target` | `workspace_kind` | 后端 |
|---|---|---|
| `auto` | `sandbox` | WSL2 |
| `auto` | `host` | A2L |
| `host` | `host` | A2L |
| `host` | `sandbox` | 拒绝 `TARGET_KIND_MISMATCH` |
| `sandbox` | `host` | 拒绝 `TARGET_KIND_MISMATCH` |

Phase 1 的 `host` 后端只允许只读 kind；出现 `write_file/edit/delete/snapshot/exec`
时返回 `OP_NOT_ENABLED_IN_PHASE1`，不静默降级。

### 3.2 A2L 协议

传输：WebSocket over TLS（企业）或 loopback WebSocket（桌面）。
A2L 是客户端，**只主动连接**；协议帧为 JSON，单帧上限 1 MiB。

```json
{
  "v": 1,
  "type": "request",
  "id": "msg_01",
  "op_id": "run_42:call_7",
  "kind": "host.read_text",
  "grant_id": "g_01",
  "workspace": {"workspace_id": "ws_01", "kind": "host", "root_handle": "h_..."},
  "payload": {"path": "src/main.c", "max_bytes": 262144},
  "deadline_ms": 30000
}
```

```json
{
  "v": 1,
  "type": "response",
  "id": "msg_01",
  "op_id": "run_42:call_7",
  "status": "ok",
  "exit_code": null,
  "duration_ms": 12,
  "data": {"text": "...", "truncated": false},
  "artifacts": [],
  "error_code": "",
  "error": ""
}
```

帧类型：

| type | 方向 | 说明 |
|---|---|---|
| `hello` / `hello_ok` | A2L → A2/A3 | 版本、设备 ID、能力摘要、安全档 |
| `heartbeat` | 双向 | 默认 15s；60s 无心跳判离线 |
| `request` / `response` | A2/A3 ↔ A2L | typed op |
| `cancel` | A2/A3 → A2L | 按 `op_id` 协作式取消 |
| `grant.request` | A3 → A2L | 请求授权：scope、verbs、ttl、reason、sensitive |
| `grant.pending` | A2L → A3 | 已进入本地 UI 等待用户确认 |
| `grant.granted` | A2L → A3 | 返回 `grant_id`、`expires_at` |
| `grant.denied` | A2L → A3 | 本地拒绝，附原因 |
| `grant.revoke` | A2L → A3 | 本地撤销立即生效；in-flight 请求取消 |
| `event` | A2L → A2/A3 | 本地同意/审计/进程状态（Phase 3 起用） |

幂等：`op_id` 由 A4 生成；A2L 对同一 `op_id` 返回缓存结果，不重复执行。
版本不匹配返回 `PROTOCOL_VERSION` 并断开；未知 `kind` 返回 `OP_UNKNOWN`。

**桌面内授权卡片（P1）**

- 桌面启动器生成内部 token，A2L 通过 `AGENT_CONSENT_URL/TOKEN` 把
  `grant.request` 转发到 A2 的 `/internal/consent`；前端轮询
  `/api/consent/pending`，在对话输出区渲染“允许本次 / 拒绝”卡片。
- A3 按 `workspace_id` 缓存 grant（TTL 1700s）；同一个本机工作区的后续
  读取复用授权，不再逐文件弹窗。缓存过期/撤销时自动重新请求一次。
- 未配置授权桥（CLI/测试）时仍回退到 Tk/Stdin 本地确认。

### 3.3 Phase 1 op 清单

| kind | 权限 | 说明 |
|---|---|---|
| `host.health` | 无 grant | 版本、安全档、能力摘要 |
| `host.list_dir` | `read` + scope | 单层目录，限制条目数 |
| `host.find_files` | `read` + scope | 原生遍历；不跟随符号链接，限制深度/数量 |
| `host.stat` | `read` + scope | 类型、大小、mtime；不解引用越界链接 |
| `host.read_text` | `read` + scope | UTF-8，显式 `max_bytes`，先限后读 |
| `host.toolchains` | 无 grant | 只返回 `ToolchainRef`，不返回绝对路径 |
| `host.env.inspect` | `read` + scope | 仅非敏感变量；名称含 KEY/TOKEN/SECRET/PASSWORD 一律过滤 |
| `host.workspace.register` | **本地用户操作** | 选目录、生成 `root_handle`、写 A2L SQLite；不是模型工具 |
| `host.workspace.list` | **本地用户操作** | 列出本机已注册工作区；不返回绝对路径 |
| `host.workspace.forget` | **本地用户操作** | 删除本地映射；A2 侧同步标记不可用 |

工作区注册/遗忘只能从 A2L 本地 UI 或本地 CLI 发起，**A3 不能替用户选目录**；
A2 只能接收 A2L 注册后上报的 `workspace_id + root_handle + a2l_id`。

写/执行/安装在 Phase 1 不注册；即使协议收到也返回 `OP_NOT_ENABLED_IN_PHASE1`。

### 3.4 错误码

| code | 含义 |
|---|---|
| `DENIED_ZX` | 命中绝对禁止根；优先级高于 grant |
| `DENIED_SCOPE` | 路径不在 grant scope 内 |
| `DENIED_VERB` | grant 不含所需动词 |
| `GRANT_REQUIRED` / `GRANT_PENDING` | 需要本地授权/等待用户确认 |
| `GRANT_DENIED` | 用户在本地拒绝 |
| `GRANT_EXPIRED` / `GRANT_REVOKED` | grant 失效 |
| `SENSITIVE_ROOT` | 命中敏感根，必须逐次本地确认 |
| `PATH_ESCAPE` | 路径逃逸、junction/符号链接/UNC/设备路径 |
| `INVALID_PATH` | 非法路径（相对路径、ADS、保留名等） |
| `UNSUPPORTED_FILESYSTEM` | 根目录不是本地固定 NTFS，或是云占位/网络盘 |
| `FILE_NOT_FOUND` / `FILE_TOO_LARGE` | 只读文件不存在/超过读取上限 |
| `INVALID_REQUEST` | 请求字段或文件内容不合法 |
| `TARGET_KIND_MISMATCH` | target 与 workspace kind 冲突 |
| `WORKSPACE_HANDLE_UNKNOWN` | `root_handle` 不在 A2L 本地映射中 |
| `WORKSPACE_UNAVAILABLE` | 本地根目录缺失/不可访问；需用户重新选择 |
| `OP_UNKNOWN` / `OP_NOT_ENABLED_IN_PHASE1` | 未注册或本阶段未开放 |
| `PROTOCOL_VERSION` | 协议版本不匹配 |
| `TOOLCHAIN_UNAVAILABLE` | 能力不存在或探测失败 |
| `TIMEOUT` / `CANCELLED` | 超时/取消 |
| `GATEWAY_OFFLINE` | A2L 不在线，A4 应降级到 sandbox |
| `A2L_PAUSED` | A2L 被用户暂停或处于离线状态 |
| `UNAUTHENTICATED` | 未完成 hello 握手 |
| `GRANT_LOCAL_ONLY` | A3 不能直接完成授权，必须走本地 UI |
| `INTERNAL` | 未分类错误；附 trace id |

### 3.5 grant 与 ZX

```python
# host_broker/policy.py（客户端）
Verbs = Literal["read", "write", "exec", "install", "network"]

@dataclass(frozen=True, slots=True)
class CapabilityGrant:
    grant_id: str
    session_id: str
    thread_id: str
    scope: tuple[str, ...]       # 路径前缀；拒绝 ""、"/"、盘符根
    verbs: frozenset[Verbs]
    ttl_s: int                   # 默认 1800；上限 28800
    issued_at: str
    expires_at: str
    reason: str
    sensitive: bool = False      # 敏感根必须逐次确认，不可长 TTL 复用
    revocable: bool = True
```

**grant 闭环**

1. A2L 收到无 grant 的 host op，返回 `GRANT_REQUIRED`
   （附 `scope/verbs/ttl_s/reason/sensitive`）给 A3。
2. A3 发送 `grant.request`；A2L 在本地托盘/面板显示确认，返回 `grant.pending`。
3. 用户批准 → A2L 签发 grant，返回 `grant.granted(grant_id, expires_at)`；
   用户拒绝 → `grant.denied(reason)`。
4. A3 带 `grant_id` 重试原 op；A2L 重新做完整判定，不因“刚批准过”跳过 ZX。
5. 用户撤销 → A2L 发 `grant.revoke`，终止该 grant 下 in-flight 请求；
   A3 后续请求返回 `GRANT_REVOKED`。

本地 UI 规则：

- **A1 只能显示“等待本机确认”，不能代批准**；最终批准在 A2L 本地 UI。
- 同一请求的重复 `grant.request` 合并展示；每会话 pending 上限与冷却时间可配。
- 敏感根（Documents/Desktop/OneDrive/浏览器 profile/.ssh 等）必须
  `sensitive=True` 且逐次确认，不给长 TTL 大 scope。
- 企业预授权只能是 allowlist + 审计的自动 grant，不能无限 scope；用户仍可
  在本地暂停/撤销。

判定顺序：**协议版本 → op 白名单 → ZX → grant 存在/未过期/未撤销 → scope →
verb → 敏感根逐次确认 → 路径 canonicalization → 执行**。任一步失败即拒绝，
不做“尽量放行”。

ZX 清单与 [ADR-0015](adr/0015-host-capability-broker.md) 第 4 节一致。
P1 默认 `lowpriv`：专用低权限账户 + 对 ZX 显式 ACL deny + 对项目根显式 ACL grant。
`appcontainer` 只作为受限只读档单独验证，不作为默认；`user-t1` 档只允许
`local-dev`，并在 `hello_ok` 与 UI 中声明 `security_tier=user-t1`、`threat_model=T1`。

### 3.6 配置项（计划新增，落地后以 `docs/contracts/config.md` 为准）

| 配置 | 默认 | 说明 |
|---|---|---|
| `AGENT_HOST_BROKER_ENABLED` | `false` | 总开关；关闭时行为不变 |
| `AGENT_HOST_BROKER_URL` | `""` | 企业 A2/A3 的 wss 地址；桌面可空 |
| `AGENT_HOST_BROKER_SECURITY_TIER` | `lowpriv` | `lowpriv`（默认）/ `appcontainer`（受限只读）/ `user-t1`（仅 local-dev） |
| `AGENT_HOST_BROKER_ACCOUNT` | `""` | 专用低权限账户名 |
| `AGENT_HOST_BROKER_DEVICE_CERT` | `""` | A2L 设备证书路径（mTLS 客户端身份） |
| `AGENT_HOST_BROKER_DEVICE_KEY` | `""` | A2L 设备私钥路径 |
| `AGENT_HOST_BROKER_TLS_CERT` | `""` | A3/A2 服务端证书路径 |
| `AGENT_HOST_BROKER_TLS_KEY` | `""` | A3/A2 服务端私钥路径 |
| `AGENT_HOST_BROKER_TLS_CA` | `""` | 双向校验 CA |
| `AGENT_HOST_BROKER_TLS_SERVER_NAME` | `""` | 客户端校验的服务器名 |
| `AGENT_HOST_BROKER_REQUIRE_TLS` | `false` | 是否强制加密通道；配置证书时实际强制 |
| `AGENT_HOST_BROKER_HEARTBEAT_S` | `15` | 心跳间隔 |
| `AGENT_HOST_BROKER_AUTOSTART` | `true` | 客户端启动时自动拉起 A2L；**不是操作系统级自启** |
| `AGENT_HOST_BROKER_PAUSED` | `false` | 本地一键离线/暂停；暂停时拒绝所有 host op |
| `AGENT_WORKSPACE_KIND_DEFAULT` | `host`（客户端）/ `sandbox`（服务端） | 新工作区默认类型 |
| `AGENT_GRANTS_ENABLED` | `false` | 关闭时回退 `access_mode` |
| `AGENT_HOST_DENY_ROOTS` | 内置 ZX | 只能追加，不能删除内置项 |
| `AGENT_HOST_ROOT_POLICY` | `local_ntfs_only` | P1 只接受本地固定 NTFS；拒绝云盘/网络/UNC/reparse |
| `AGENT_HOST_SENSITIVE_ROOTS` | 内置敏感根 | 命中必须逐次本地确认，不可长 TTL 复用 |
| `AGENT_HOST_TOOLCHAIN_ROOTS` | `""` | 工具链探测根；不暴露给模型 |
| `AGENT_EXECUTION_BACKEND` | `wsl` | 保持现有默认 |

配置项必须先改 `config.py` 再重跑 `python scripts/gen_contracts.py`，
不要手改 `docs/contracts/`。

## 4. 文件清单

### 4.1 新增

| 文件 | 职责 | 阶段 |
|---|---|---|
| `src/coding_agent/host_broker/__init__.py` | 包入口；不导出能力实现 | P1 |
| `src/coding_agent/host_broker/protocol.py` | 版本化帧、DTO、错误码；无 I/O | P1 |
| `src/coding_agent/host_broker/transport.py` | 传输抽象 + 内存通道；生产 WebSocket 后续接入 | P1 |
| `src/coding_agent/host_broker/client.py` | A4 侧客户端：外连、握手、心跳、请求/取消 | P1 |
| `src/coding_agent/host_broker/server.py` | A2L 进程：握手、分发、取消、暂停、优雅退出 | P1 |
| `src/coding_agent/host_broker/policy.py` | grant/ZX/verb 判定；判定顺序唯一入口 | P1 |
| `src/coding_agent/host_broker/paths.py` | Windows handle 级 canonicalization、junction/UNC/ADS 防护 | P1 |
| `src/coding_agent/host_broker/toolchain.py` | 能力注册与 `ToolchainRef`；对模型只暴露摘要 | P1 |
| `src/coding_agent/host_broker/ops.py` | Phase 1 只读能力：list/find/stat/read/toolchains/env | P1 |
| `src/coding_agent/host_broker/sync_client.py` | 同步调用桥：给工具层/ExecutionBackend | P1 |
| `src/coding_agent/host_broker/acceptor.py` | A3 侧监听、握手、`AcceptedHostCaller` | P1 |
| `src/coding_agent/host_broker/__main__.py` | A2L 子进程入口（主动外连） | P1 |
| `src/coding_agent/host_broker/consent.py` | stdin/回调/本机对话框同意 | P1 |
| `src/coding_agent/host_broker/workspace_registry.py` | 本机选目录、生成 handle、保存本地映射 | P1 |
| `src/coding_agent/host_broker/errors.py` | op/policy 统一业务错误 | P1 |
| `src/coding_agent/execution/host_file_access.py` | A2L typed ops 的 FileAccess 适配 | P1 |
| A2L 本地审计（计划中，尚未落文件） | 本地哈希链、设备签名、异步转发 | P2 |
| `src/coding_agent/host_broker/store.py` | 本地 SQLite：workspace_id ↔ absolute_root、grants、审计游标 | P1 |
| `src/coding_agent/host_broker/op_journal.py` | 本地 op 日志：`started/finished/unknown`；P1 只读，P2 写/执行防重放 | P1 |
| `src/coding_agent/execution/file_access.py` | `FileAccess` 协议 + `SandboxFileAccess` / `HostFileAccess`；P1 只读三工具使用 | P1 |
| `src/coding_agent/host_broker/lifecycle.py` | 绑定客户端生命周期、单实例、崩溃重启、暂停/离线、托盘状态 | P1 |
| `src/coding_agent/host_broker/consent.py` | 本地同意 UI 骨架（Phase 1 只读不弹窗） | P1 |
| `src/coding_agent/execution/host_backend.py` | 实现 `ExecutionBackend`，把请求映射成 `host.*` | P1 |
| `src/coding_agent/control/migrations/versions/0005_workspace_kind.py` | 工作区 kind/a2l_id/root_handle 迁移 | P1 |
| `src/coding_agent/control/migrations/versions/0006_session_workspace_binding.py` | 会话持久化 workspace_kind/a2l_id/root_handle | P1 |
| `src/coding_agent/workspace/model.py` | `WorkspaceRef` / `WorkspaceKind` | P1 |
| `tests/unit/test_execution_targets.py` | 路由矩阵、冲突拒绝 | P1 |
| `tests/unit/test_workspace_ref.py` | 引用序列化、不传绝对路径 | P1 |
| `tests/unit/test_host_broker_protocol.py` | 版本、帧、幂等、错误码 | P1 |
| `tests/unit/test_host_policy.py` | ZX > grant、verb/scope/TTL/撤销 | P1 |
| `tests/unit/test_host_paths.py` | junction/符号链接/UNC/设备路径/ADS | P1 |
| `tests/unit/test_host_toolchain_registry.py` | 能力摘要不含绝对路径 | P1 |
| `tests/unit/test_host_broker_channel.py` | 只外连、心跳、离线降级 | P1 |
| `tests/unit/test_host_broker_lifecycle.py` | 随客户端启停、单实例、重启、暂停/离线、托盘状态 | P1 |
| `tests/unit/test_host_workspace_registrar.py` | handle 映射、绝对路径不外泄、映射丢失 fail closed、根目录类型限制 | P1 |
| `tests/unit/test_host_op_journal.py` | started/finished/unknown、重启不重放 | P1 |
| `tests/unit/test_host_policy.py` | ZX > grant、scope/verb/TTL/撤销/敏感根 | P1 |
| `tests/unit/test_host_paths.py` | 根限制、junction/UNC/ADS/保留名 | P1 |
| `tests/unit/test_host_toolchain_registry.py` | 摘要不含绝对路径 | P1 |
| `tests/unit/test_host_broker_ops.py` | 只读 op 在 grant/ZX 之后执行 | P1 |
| `tests/unit/test_host_backend.py` | A4 target 路由、host 只读后端、冲突拒绝 | P1 |
| `tests/unit/test_workspace_kind.py` | host 工作区只存 handle，不存绝对路径 | P1 |
| `tests/unit/test_agent_context_host.py` | A3 上下文与 host runtime 自动注入 | P1 |
| `tests/unit/test_host_workspace_registrar.py` | 本机选目录、重复目录、取消、非法路径 | P1 |
| `tests/unit/test_agent_server_host_register.py` | A3 host 工作区注册路由 | P1 |
| `tests/unit/test_host_launcher_config.py` | 桌面 host 配置与启动命令 | P1 |
| `tests/unit/test_file_access.py` | Sandbox/Host 只读三工具路径语义一致 | P1 |
| `tests/integration/test_host_broker_readonly.py` | Windows 真机只读 e2e；标记 `host` | P1 |

### 4.2 修改

| 文件 | 改动 |
|---|---|
| `src/coding_agent/execution/contracts.py` | 增加 `target/workspace_kind/grant_id`；只增不改 |
| `src/coding_agent/execution/service.py` | 内置 target 路由；capabilities 增加 targets/workspace_kind |
| `src/coding_agent/execution/sandbox_backend.py` | 保持 WSL 语义；实现现有 protocol 的 `kind` |
| `src/coding_agent/workspace/browse.py` | 接收 `WorkspaceRef`，拒绝裸绝对路径 |
| `src/coding_agent/tools/files.py` | `file_read/file_list` 改走 `FileAccess`；P1 只读，不碰写路径 |
| `src/coding_agent/tools/search.py` | `find_files` 增加 host 原生遍历；不启动 shell |
| `src/coding_agent/runtime.py` | 按工作区 kind 注入 `HostFileAccess` 或 `SandboxFileAccess` |
| `src/coding_agent/agent_protocol.py` | A2/A3 上下文增加 `workspace_kind/a2l_id/root_handle` |
| `src/coding_agent/web/models.py` / `services.py` | 会话上下文透传 host 字段 |
| `src/coding_agent/agent_context.py` | host 工作区自动注入 `file_access/targets`，强制只读 |
| `src/coding_agent/desktop/launcher.py` | 自动拉起/停止/守护 A2L，注入 host 环境 |
| `src/coding_agent/desktop/config.py` | host 工作区、handle、安全档字段 |
| `src/coding_agent/control/workspaces.py` | 工作区记录增加 `kind/a2l_id/root_handle`；host 不存绝对路径 |
| `src/coding_agent/control/schema.py` | 加列迁移（旧行 `sandbox`）：`kind`、`a2l_id`、`root_handle` |
| `src/coding_agent/config.py` | 新增第 3.6 节配置项 |
| `src/coding_agent/desktop/supervisor.py` | 客户端启动/退出时拉起/终止 A2L；崩溃重启；失败不拖垮 A2/A3 |
| `src/coding_agent/desktop/launcher.py` / `config.py` | A2L 随客户端启动、托盘、安全档选择 |
| `pyproject.toml` | 新增 `host` pytest marker |
| `tests/unit/test_architecture.py` | AST 禁止前端/服务端导入 `host_broker.server/policy` |
| `tests/unit/test_desktop_supervisor.py` | A2L 守护、降级、单实例 |
| `docs/ARCHITECTURE.md` | 增加 A2L 与 host/sandbox 工作区 |
| `docs/THREAT_MODEL.md` | 增加 T4 与 A2L 防线 |
| `docs/glossary.md` | A2L、grant、ZX、host/sandbox 工作区、L4 |
| `docs/bounded-contexts.yaml` | 新增 `host-broker` 上下文与依赖规则 |
| `docs/acceptance/acceptance-criteria.yaml` | 增加 Phase 1 planned 验收项 |
| `docs/tech-debt.yaml` | 登记 Windows 无 bwrap、跨 kind 不支持等 |
| `docs/rfc/0002-native-execution-domain-and-grant-model.md` | 状态 accepted；补批准后的修订 |

### 4.3 不在 Phase 1 的文件

`host_broker/install.py`、`host_broker/snapshots.py`、`host_broker/exec.py`、
Windows 服务包装、MSI/签名、A2L 自动更新、`control_host_agents` 表。
这些在 Phase 2–4 单独评审，避免把“只读骨架”做成半个执行代理。

## 5. Phase 1 安全不变量

1. **A2L 只外连**：不监听任何 TCP/Unix socket；没有入站控制面。
2. **服务端请求 ≠ 授权**：每次执行都重新做本地 grant + ZX 判定。
3. **ZX 优先于 grant**：任何 grant 都不能覆盖 ZX。
4. **不传绝对路径**：跨层只传 `root_handle` + 相对路径；解析在 A2L 内完成。
5. **Phase 1 无副作用**：写/删除/执行/安装一律 `OP_NOT_ENABLED_IN_PHASE1`。
6. **不泄露宿主环境**：`host.env.inspect` 过滤敏感名；不返回完整 `PATH`。
7. **审计不可由 agent 改写**：本地审计在 agent 不可写目录，哈希链 + 设备签名。
8. **离线不伪装**：A2L 离线时 A4 降级 sandbox；不把 host 请求悄悄改到 WSL。
9. **安全档诚实**：`user-t1` 必须在握手、UI 与文档中标注 T1，不得宣称 T2。
10. **协议 fail closed**：版本/未知 op/畸形帧一律拒绝并断开。
11. **绑定客户端生命周期**：A2L 随客户端启动/退出，不注册系统服务、不独立
    开机自启；客户端运行期间单实例、按交互用户运行，不得以 LocalSystem/root
    运行；必须有托盘可见状态与一键暂停/离线。
12. **handle-only**：A2 不持有 host 绝对路径；A2L 本地映射丢失时
    `WORKSPACE_HANDLE_UNKNOWN`/`WORKSPACE_UNAVAILABLE`，不猜路径、不重建。
13. **本地 UI 是最终闸门**：工作区注册、grant、暂停、日志查看都在客户端；
    服务端不能替用户选目录或批准授权。
14. **op_journal 先于执行**：P1 只读也要落 `op_id`；P2 写/执行重启后未完成
    op 返回 `unknown`，绝不盲目重放。
15. **根目录限制**：P1 只接受本地固定 NTFS；拒绝 OneDrive、云占位、网络/映射盘、
    UNC、WSL/VM 共享、reparse 根与设备路径。
16. **敏感根逐次确认**：Documents/Desktop/OneDrive/浏览器 profile/.ssh 等
    不允许作为普通 scope，必须 `sensitive=True` 且每次本地确认。

## 6. 测试与验收

Phase 1 完成时至少通过：

```bash
conda activate agent
pytest -m "not wsl and not llm" \
  tests/unit/test_execution_targets.py \
  tests/unit/test_workspace_ref.py \
  tests/unit/test_host_broker_protocol.py \
  tests/unit/test_host_policy.py \
  tests/unit/test_host_paths.py \
  tests/unit/test_host_toolchain_registry.py \
  tests/unit/test_host_broker_channel.py \
  tests/unit/test_host_broker_lifecycle.py \
  tests/unit/test_host_workspace_store.py \
  tests/unit/test_host_op_journal.py \
  tests/unit/test_file_access.py
pytest -m host tests/integration/test_host_broker_readonly.py
python scripts/check_docs.py --strict
python scripts/gen_contracts.py --check
```

验收场景：

```text
Given 未配置 AGENT_HOST_BROKER_ENABLED
When  执行任意 wsl 任务
Then  行为与当前版本一致，回归套件全绿

Given host 工作区与 target=auto
When  A4 收到 host.read_text
Then  请求经 A2L 只读通道执行，回执含 workspace_kind=host

Given host 工作区与 target=sandbox
When  A4 收到请求
Then  返回 TARGET_KIND_MISMATCH，不静默改道

Given grant.scope=("D:\\Projects\\foo",) 且 verbs={"read"}
When  请求读取该前缀外文件
Then  DENIED_SCOPE；文件未被读取

Given 路径指向 C:\Windows\System32 或 junction 逃逸
When  请求读取
Then  DENIED_ZX 或 PATH_ESCAPE，且审计记录拒绝原因

Given A2L 断开
When  A4 需要执行 host 请求
Then  返回 GATEWAY_OFFLINE 并降级 sandbox；运行不被阻塞

Given 客户端启动/退出
When  客户端拉起或关闭
Then  A2L 随之启动/终止；不残留系统服务或独立后台进程

Given A2L 进程崩溃
When  客户端监督进程重启它
Then  单实例锁仍有效；本地 SQLite 映射仍在；未完成 op 不重放副作用

Given host 工作区由客户端注册
When  A2 保存该工作区
Then  只保存 workspace_id + root_handle + a2l_id，不保存绝对路径

Given A2L 本地映射丢失
When  A3 请求该 host 工作区
Then  返回 WORKSPACE_HANDLE_UNKNOWN；不猜测、不按旧路径重建

Given A3 请求无 grant 的 host.read_text
When  A2L 处理请求
Then  返回 GRANT_REQUIRED；A2L 本地 UI 显示 scope/verbs/reason；
      用户批准后返回 grant.granted，A3 带 grant_id 重试

Given 用户拒绝 grant.request
When  A3 重试
Then  返回 GRANT_DENIED；不读取文件；审计记录拒绝

Given grant scope 命中 Documents/Desktop/OneDrive/.ssh
When  请求读取
Then  必须 sensitive=True 并逐次本地确认；长 TTL 或宽 scope 被拒绝

Given host 工作区根是 OneDrive/网络盘/UNC/reparse
When  用户尝试注册
Then  拒绝注册；不生成 root_handle

Given A2L 在 host 写操作中途重启
When  A3 恢复
Then  op_journal 返回 unknown；不盲目重放写操作

Given 请求 host.write_bytes
When  在 Phase 1 到达 A2L
Then  OP_NOT_ENABLED_IN_PHASE1；不产生任何写
```

## 7. 完成定义与回滚

Phase 1 完成定义：

- 上述契约与文件清单落地，`config.py` 与生成契约一致；
- A2L 随客户端启动/退出，客户端运行期间单实例、崩溃重启、托盘暂停/离线；
  不注册系统服务、不独立常驻；不得以 LocalSystem 运行；
- host 工作区只存 `workspace_id + root_handle + a2l_id`，A2 不落绝对路径；
- grant 闭环（request/pending/granted/denied/revoke）与本地 UI 可用；
- `FileAccess` 抽象落地，P1 三个只读工具走 host/sandbox 同一接口；
- `op_journal` 落盘，P2 写/执行恢复时有 `unknown` 语义；
- P1 根目录只接受本地固定 NTFS，云盘/网络/UNC/reparse 被拒绝；
- 默认关闭时全量快反馈与现有 WSL 集成测试无行为变化；
- Windows 真机只读 e2e 通过，Linux CI 用 fake broker 通过契约测试；
- ADR-0015、RFC-0002、ARCHITECTURE、THREAT_MODEL、glossary、bounded-contexts
  与验收锚点同步；
- `check_docs.py --strict` 无断链、无裸 pytest、无过期 owner。

回滚：

- `AGENT_HOST_BROKER_ENABLED=false` + `AGENT_EXECUTION_BACKEND=wsl`；
- 工作区 `kind` 列保留，旧行按 `sandbox` 读取；
- 删除 A2L 进程不影响 A2/A3/A4；不修改 checkpoint/审计格式。

## 8. 后续阶段

**P2–P5 是后续工作，不纳入当前交付。** 当前只交付 Phase 1 只读能力与安全演示；
写/编译/安装/企业分发需要单独立项、评审与验收。

| 阶段 | 内容 | 关键前置 |
|---|---|---|
| P2 | host 写/删除/快照、grant 写授权、host 工作区 revision | P1 路径与 grant 测试全绿 |
| P3 | `host.exec` + 工具链调用 + `output_roots` + build 脚本策略 | L3/L4 等级与审计契约 |
| P4 | `host.install`（L4）、客户端安装包/签名更新、企业设备注册/吊销 | 签名安装器、企业策略、P1 生命周期稳定 |
| P5 | sandbox WSL 瘦身/加固复核、跨 kind 策略、提示注入评估 | P1–P4 稳定运行 |

跨 kind 编译/验证不在任何阶段默认开启；如需要，另立 RFC。

## 9. 实现进度（2026-10-25）

| 项 | 状态 | 落点 |
|---|---|---|
| `WorkspaceRef` / `WorkspaceKind` | 完成 | `src/coding_agent/workspace/model.py` |
| `ExecutionRequest.target/workspace_kind/grant_id` | 完成 | `src/coding_agent/execution/contracts.py` |
| `FileAccess` 协议 + `SandboxFileAccess` | 完成（契约层） | `src/coding_agent/execution/file_access.py` |
| 本地 `op_journal` | 完成（骨架） | `src/coding_agent/host_broker/op_journal.py` |
| A2L 配置骨架 + `host` marker | 完成 | `config.py`、`pyproject.toml` |
| A2L 协议 + 客户端/服务端骨架 | 完成（内存通道） | `host_broker/protocol.py`、`client.py`、`server.py`、`transport.py` |
| A2L mTLS + 设备/服务端指纹绑定 | 完成（P0） | `transport.py`、`client.py`、`server.py`、`agent_server.py` |
| hello nonce / 防重放 / a2l_id audience | 完成（P0） | `protocol.py`、`server.py` |
| grant one-shot / 同意限流 / 断线撤销 | 完成（P0） | `policy.py`、`server.py`、`acceptor.py` |
| A2L 生命周期（单实例/暂停/离线） | 完成（骨架） | `host_broker/lifecycle.py` |
| grant/ZX/路径/工具链策略 | 完成 | `host_broker/policy.py`、`paths.py`、`toolchain.py` |
| Phase 1 只读能力 | 完成（本地 handler） | `host_broker/ops.py` |
| grant 本地 UI | 协议帧已就绪；UI 待做 | `host_broker/protocol.py`、`host_broker/consent.py` |
| A4 target 路由 + host 只读后端 | 完成 | `execution/service.py`、`execution/host_backend.py` |
| 工作区 kind/handle 迁移 | 完成 | `control/schema.py`、`control/workspaces.py`、迁移 `0005` |
| `file_read/list_dir/find_files` 接入 `FileAccess` | 完成 | `tools/files.py`、`tools/search.py`、`tools/registry.py` |
| A2L 同步调用桥 + `HostFileAccess` | 完成 | `host_broker/sync_client.py`、`execution/host_file_access.py` |
| grant.request 本地同意闭环 | 完成（stdin/回调） | `host_broker/consent.py`、`server.py` |
| A3 侧监听 + 握手 | 完成 | `host_broker/acceptor.py`、`agent_server.py` |
| A2L 子进程入口 | 完成 | `host_broker/__main__.py` |
| desktop supervisor 拉起/终止 A2L | 完成（开发/桌面） | `desktop/supervisor.py`、`desktop/launcher.py` |
| runtime 注入 `file_access/targets` | 完成 | `runtime.py`、`graph/build.py` |
| A2 → A3 host 上下文字段 | 完成 | `agent_protocol.py`、`web/models.py`、`web/services.py` |
| runtime factory 按 host 工作区自动注入 | 完成 | `agent_context.py` |
| 本机同意对话框 | 完成（tkinter 最小实现） | `host_broker/consent.py` |
| 本机工作区注册（A2L 选目录 → A3 → A2 → 前端） | 完成 | `workspace_registry.py`、`agent_server.py`、`web/routes/workspaces.py`、前端按钮 |
| Windows 真机只读 e2e（TCP loopback） | 完成 | `tests/integration/test_host_broker_readonly.py` |
| 真实桌面冒烟（A3 + A2L + A2 握手） | 完成（本机 20s 冒烟） | `desktop/launcher.py`、日志确认 `A2L connected` |
| 托盘常驻/更完整的同意面板 | 待做 | tkinter 对话框在授权时弹出 |
