# ADR-0015：宿主能力代理（A2L）与 host/sandbox 双工作区

- **Status**：accepted
- **Date**：2026-10-25
- **Deciders**：项目作者
- **Owner**：项目作者

## Context

企业级部署下，用户电脑只运行 A1 前端；A2 控制面、A3 编排面、A4 执行面都在
服务端，工作区在服务端 WSL2 的 ext4 上。用户要求 agent 能直接使用**用户本机**的
编译器、解释器与包管理器，构建产物留在用户电脑，缺少的环境在授权后安装。

当前约束与冲突：

| 约束 | 初衷 | 冲突 |
|---|---|---|
| `[interop] appendWindowsPath=false` | 防 Windows PATH 进入 WSL：DLL 劫持、宿主凭据、绕过加固 | 服务端 WSL 内看不到用户本机工具链 |
| 工作区在服务端 ext4 | `/mnt` 9p 性能差，权限位/符号链接/文件锁语义不一致 | 产物不在用户熟悉的位置 |
| `\\wsl$` / `/mnt/c` 桥接 | 唯一互通手段 | 用户电脑与服务端不在同一台机器，物理上不通；在桌面场景也会绕开 bwrap 的挂载策略 |
| `access_mode` 三档 | fail-closed | 没有“路径前缀 × 动词”的中间态 |
| `host_ops` kind 白名单 | 宿主内部操作不经模型 | 不能覆盖“用用户本机 MSVC 编译” |

RFC-0002 已论证：正确做法不是放宽 WSL，而是在客户端增加一个**主动外连**的
执行域，并给 A4 增加一个可选后端。批准时用户进一步拍板：

1. **工作区主形态改为 host NTFS**，sandbox（WSL ext4）并存，不再是“服务端 ext4 为唯一真身”；
2. **A2L 安全档默认 lowpriv + ACL**；AppContainer 仅作为受限只读档单独验证，
   普通用户进程只允许 `local-dev` 的 T1 档；
3. **A2L 绑定客户端生命周期**：客户端只有 A1 与 A2L；A2/A3/A4 全在服务器。
   A2L 随客户端启动/退出，**不得脱离客户端独立常驻系统**，不注册系统服务，
   也不以 LocalSystem/root 运行。

本 ADR 固定这三个决定，并把 RFC-0002 中的 A1.5 正式命名为 **A2L**，避免与
“第五层是 A5”混淆。

## Decision

### 1. 新增 A2L，但 A1–A4 不重编号

拓扑：

```text
客户端 · 用户电脑
  A1 前端（浏览器 / pywebview / CLI / TUI）
  A2L 宿主能力代理（常驻）
        │ 只主动外连（loopback / mTLS）
服务端 · 企业服务器
  A2 控制面（认证、租户、会话、配额、审计索引）
        │
  A3 编排面（LangGraph、审批决定、checkpoint）
        │ ExecutionRequest(target=...)
  A4 执行面（路由）
        ├─ target=sandbox  → WSL2（非 root、无 interop、ext4）
        └─ target=host     → 经 A2L 在客户端执行

A2L：host 工作区、文件/快照/工具链/安装/进程/本地同意 UI/本地审计
      只主动外连，不开入站端口；随客户端启动/退出，不独立常驻系统
```

- **A2L 不是 A1 的反向代理。** A1 直连 A2；A2L 只接受 A2/A3 的 typed
  capability RPC 与本地同意 UI。把它做成前端网关会多一个解析 HTTP 的
  高权限进程，攻击面更大。
- **A2L 绑定客户端生命周期**：由客户端启动时拉起、客户端退出时终止；
  客户端最小化到托盘期间可继续运行，但**不得脱离客户端独立常驻系统**。
  Phase 1 以客户端子进程/托盘形式运行（便于调试）；生产态随客户端安装包分发，
  **不注册 Windows 服务**，也不做开机独立自启。企业设备注册不在 P1。
- 保留 A1–A4 编号与已冻结契约（`tests/unit/test_layer_contracts.py`）。
  A2L 是部署上的第五个组件，也是 A4 的 `host` 后端，不是新的编排层。
- 五层的规范表述：**A1 前端 / A2L 宿主代理 / A2 控制面 / A3 编排面 /
  A4 执行面**。数据流不是简单串行，A2L 旁路于 A4；服务端只部署 A2/A3/A4。

### 2. host/sandbox 双工作区，禁止跨挂载

工作区从“一个根路径”升级为带类型的引用：

```python
WorkspaceRef(
    id="ws_...",
    kind="host" | "sandbox",
    root_handle="opaque-handle",   # 跨层不传绝对路径
)
```

| kind | 位置 | 用途 | 执行后端 | 源文件真身 |
|---|---|---|---|---|
| `host` | Windows NTFS | 桌面/企业用户本机工具链项目 | A2L | 是 |
| `sandbox` | WSL2 ext4 | Linux 目标、不可信仓库、隔离脚本 | WSL2 | 是 |

- **默认客户端工作区 = `host`；服务端/CI 默认 = `sandbox`。**
- 每个工作区的源码与产物在**同一侧**；`verify` 与编译使用同一后端。
- **Phase 1 不支持跨 kind 编译或验证**。host 工作区不通过 `/mnt` 或 `\\wsl$`
  暴露给 WSL；sandbox 工作区也不把源码复制到 Windows 临时目录。
- 工具与 A3 只接收 `WorkspaceRef` 与相对路径；绝对路径只在 A2L/WSL 内部解析。
- **host 绝对路径只存 A2L 本地 SQLite**；A2 只存
  `workspace_id + root_handle + a2l_id`。host 工作区由客户端本地选目录/注册，
  A2 不接收任意主机路径；A2L 重启后凭本地映射恢复 handle。
- `host` 工作区的构建输出落在显式 `output_roots`（工作区 `.agent/build` 或用户
  配置的缓存根），快照默认只覆盖源码；产物按内容哈希登记并可清理。
- **P1 根目录只接受本地固定 NTFS**；拒绝 OneDrive、云占位文件、网络/映射盘、
  UNC、WSL/VM 共享、reparse 根与设备路径。路径字符串看起来像 `C:\...` 不够，
  必须用 handle 级校验确认文件系统类型与 reparse 状态。
- **敏感根**（Documents/Desktop/OneDrive/浏览器 profile/.ssh 等）不允许作为
  普通 scope；必须逐次本地确认，不能靠长 TTL 大 scope 复用。

### 3. 授权是客户端签发的凭证，不是服务端开关

`CapabilityGrant` 的最小粒度是 **（路径前缀 × 动词）**：

```python
CapabilityGrant(
    grant_id="g_...",              # 客户端签发；服务端只持有 id 引用
    session_id="...", thread_id="...",
    scope=("D:\\Projects\\foo",),  # 必须显式；不接受 ""、"/"、"C:\\"
    verbs=frozenset({"read", "write", "exec", "install", "network"}),
    ttl_s=1800,                    # 默认 1800；上限 8h；不提供永久授权
    issued_at="...", expires_at="...",
    reason="file_read D:\\Projects\\foo\\a.c",  # 展示给用户
    revocable=True,
)
```

- **服务端下发的是请求，不是指令。** 服务端签名只证明请求来源，不构成授权；
  A2L 每次执行都独立做本地 grant + ZX 判定。
- **授权不上云**：grant 的 scope/verbs/TTL 只存客户端本地；A2 只持有
  `grant_id` 与审计引用。
- **闭环帧**：`grant.request` / `grant.pending` / `grant.granted` /
  `grant.denied` / `grant.revoke`。A3 无 grant 时得到 `GRANT_REQUIRED`；
  A1 只能显示“等待本机确认”，不能代批准；最终批准/撤销在 A2L 本地 UI。
- **敏感根逐次确认**：命中 Documents/Desktop/OneDrive/浏览器 profile/.ssh 等
  必须 `sensitive=True`，不能拿长 TTL 的宽 scope 复用。
- 撤销立即生效，并终止该 grant 下仍在运行的子进程。
- 不提供“全部磁盘”“永久有效”授权；`C:\`、盘符根、用户 home 根都不是合法 scope。

### 4. ZX 必须由 OS 强制，不能只靠 glob

| Zone | 内容 | 默认 verbs | 强制点 |
|---|---|---|---|
| Z0 | 工作区（host 或 sandbox） | read + 受控 write + 受限 exec | 后端路径守卫 + 快照 |
| Z1 | 授权只读区 | read | grant + 路径前缀 |
| Z2 | 本机工具链 | exec | 能力注册 + 参数白名单 |
| Z3 | 授权可写区（产物/依赖） | write | grant + `output_roots` |
| ZX | 绝对禁止 | 无 | OS ACL（lowpriv）；AppContainer 为可选受限档 |

ZX 至少包含：`C:\Windows\*`、`C:\Program Files*`、`C:\ProgramData\*`、
其他用户 profile、凭据/浏览器 profile、`~/.ssh`、私钥与证书、`.aws/`、
`.kube/`、`.npmrc`、`.netrc`、`credentials*.json`、系统还原点/BCD/驱动目录。

**Deny 优先级高于任何 grant。** 但只有在 A2L 本身没有这些路径的访问权时，
“绝对禁止”才成立：

- `lowpriv`（**P1 默认**）：专用低权限 Windows 账户 + 对 ZX 显式 ACL deny，
  对用户选定的项目根显式 ACL grant；不是 T2，但 ZX 由 OS 拒绝；
- `appcontainer`（**可选受限只读档**）：A2L 运行在 AppContainer / 受限 token 下，
  ACL 显式 deny ZX；因文件系统与子进程限制较多，不作为 P1 默认；
- `user-t1`：普通用户进程，**只允许 `local-dev`**；ZX 只是策略承诺，
  不是 T2 保证，UI 与文档必须明示。

Windows 路径校验必须按 handle 级 canonicalization 处理 junction、符号链接、
UNC、设备路径、8.3 短名与 ADS；字符串 `realpath` 不够。

### 5. typed ops + 能力注册，不把宿主 shell 作为默认能力

- A2L 只接受白名单 op：`host.read_text`、`host.list_dir`、`host.stat`、
  `host.toolchains`、`host.env.inspect`、`host.exec`、`host.install`、
  `host.health` 等；未知 op 一律拒绝。
- 服务端工具通过 `FileAccess` 抽象访问 host/sandbox；P1 只接
  `file_read/list_dir/find_files`，`find_files` 用 host 原生遍历，不启动 shell。
- 每个 host op 先写本地 `op_journal`（`started/finished/unknown`）；
  P2 写/执行重启后未完成的 op 返回 `unknown`，绝不盲目重放。
- 模型看到的是**能力清单**，不是路径：

```json
{"toolchains": [
  {"id": "msvc-14.41", "kind": "cpp", "version": "14.41", "output_roots": ["build"]}
]}
```

- 调用时传 `ToolchainRef` 与受控参数；A2L 解析绝对路径并执行。模型不能设置
  `PATH`、不能拼任意命令行、不能让服务端注入环境变量。
- `host.exec` 是**代码执行**，不是只读能力：`cargo build`、`npm install`、
  `setup.py` 都会执行仓库里的构建脚本。Phase 1 不开放写/执行；Phase 3 开放时
  必须 L3 授权 + `output_roots` + 网络策略；不可信仓库仍走 sandbox。
- `host.install` 是 **L4 环境变更**：永不自动、不被 `allow_rules` 覆盖、
  必须 grant 含 `install` 且用户二次确认；安装器记录来源/版本/哈希。
  优先便携工具链到 `%LOCALAPPDATA%\CodingAgent\toolchains\<id>\<version>`，
  不改全局 `PATH`；系统级 MSI/exe/UAC 只提供“打开下载页/复制命令”，不自动执行。

### 6. 通道只外连，本地否决权是最终闸门

- A2L **主动外连**：桌面走 loopback 到 A3；企业走设备证书 + mTLS 到 A2。
  不开入站端口，不要求 `appendWindowsPath`，不使用 `\\wsl$`。
- 服务端不能无本地同意地推送任意命令；A2L 断线时 A4 降级到 `sandbox`，
  不阻塞任务，也不能假装 `host` 可用。
- **客户端生命周期**：随客户端启动/退出；客户端运行期间单实例、崩溃由客户端
  监督重启、心跳/健康探针、托盘可见、一键离线/暂停；不得脱离客户端独立常驻，
  不得注册系统服务，不得以 LocalSystem/root 运行。
- **更新策略**：A2L 随客户端安装包更新，只接受签名包/管理员策略；不接受服务端
  静默推送，也没有独立更新通道。Phase 1 只保留客户端重启。
- **本地 UI 是最终闸门**：授权、暂停、查看日志都在客户端完成；服务端页面
  不能替代本地同意。
- A2L 本地审计写在 agent 不可写目录（如 `%ProgramData%\CodingAgent\audit`），
  哈希链 + 设备签名，异步转发 A2；A2 只建索引。
- A2L 自身更新沿用 `desktop/update.py` 的“只检查 + 打开下载页”策略，
  不接受服务端静默推送更新。

### 7. 兼容与回滚

- `AGENT_EXECUTION_BACKEND=wsl` 仍是默认；A2L 关闭时行为与当前版本一致。
- `AGENT_HOST_BROKER_ENABLED=false`、`AGENT_GRANTS_ENABLED=false`、
  `AGENT_WORKSPACE_KIND_DEFAULT=sandbox` 三个开关独立回滚。
- `access_mode` 三档映射为预置 grant，旧会话不需要迁移。
- `ExecutionRequest` **只增字段**（`target/workspace_kind/grant_id`）；
  `host_ops` kind 白名单只追加，不改既有语义。
- 工作区表的 `kind` 列是加列迁移；旧行默认 `sandbox`。

## Consequences

### 好的

- 用户本机工具链与产物留在本机，不需要在服务端重建环境；
- 服务端 WSL 加固保持原样，不需要打开 interop 或依赖 `\\wsl$`；
- 授权可撤销、可过期、可审计，比布尔开关更接近真实企业策略；
- host/sandbox 双工作区让“本地工具链”和“不可信仓库隔离”各得其所。

### 坏的 / 代价

- **A2L 是客户端运行期间的内网执行代理，新增 T4 横向风险**：服务端失陷时，
  攻击者能触达正在运行的客户端。本地否决权、ZX、出站-only、设备证书是硬要求；
  客户端退出即切断通道，但这不等于服务端无法在运行期间作恶。
- **客户端生命周期带来运维成本**：随客户端启动/退出、安装、升级、崩溃恢复、
  证书轮换、单实例、多用户会话隔离都要做；A2L 不在时 host 工作区不可用，
  必须显式降级。
- **路径映射只在客户端**：A2 无法在 A2L 离线时校验 host 路径；A2L 必须
  fail closed，A2 侧只把 `root_handle` 当不透明引用。
- **Windows 没有等价 bwrap。** `lowpriv` 只能做到“受限用户 + ACL + 作业对象”，
  不是内核级隔离；`appcontainer` 更受限但不适合作为 P1 默认；`user-t1` 明确只是 T1。
- **双工作区让测试矩阵翻倍**：host 后端必须在 Windows 真机跑，Linux CI
  只能跑 fake broker 与契约测试。
- **host 工作区的源码与产物在用户机器上，服务端看不到**；跨 kind 的编译/验证
  不支持，Phase 1 必须明确拒绝而不是静默回退。
- **安装与构建脚本是供应链风险**：L4 只降低概率，不能消除。
- 路径/权限/审计从“一处 WSL”变成“服务端 + 每个客户端”，运维与故障面扩大。

### 中性 / 需要留意

- **当前交付只包含 Phase 1 只读 + 安全演示**；`host` 写/删除/快照、编译执行、
  安装、企业分发（P2–P5）都标为后续，不在本轮范围。
- A2L 与 A4 共用 `ExecutionService` 契约，但**不复用** `tools/` 与 `sandbox/`
  的进程内路径；跨进程边界必须序列化，性能与错误码要单独测。
- `host` 工作区默认在客户端；服务端多租户不要默认开启。
- ZX 清单是安全资产，改动必须同步 `THREAT_MODEL.md` 与测试。
- 提示注入的后果从“改工作区文件”升级为“在用户本机安装软件”；本 ADR
  不解决提示注入，只把它列为 T4 的主要风险面。

## 复核

- **Verified by**（Phase 1 落地后）：
  `pytest -m "not wsl and not llm" tests/unit/test_execution_targets.py
  tests/unit/test_workspace_ref.py tests/unit/test_host_policy.py
  tests/unit/test_host_broker_protocol.py tests/unit/test_host_broker_lifecycle.py
  tests/unit/test_host_workspace_store.py tests/unit/test_host_op_journal.py
  tests/unit/test_file_access.py`
  以及 Windows 上的
  `pytest -m host tests/integration/test_host_broker_readonly.py`。
- **Revisit when**：Windows AppContainer/Job Object 能提供可用的文件系统/网络
  隔离；企业要求服务端完全不能触达客户端；或 host/sandbox 跨 kind 编译成为
  硬需求。
