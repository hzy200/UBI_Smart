---
layer: L2
doc_id: rfc-0002
title: 客户端执行域、授权凭证模型与 A2L 宿主能力代理
owner: 项目作者
status: accepted
created: 2026-10-25
last_reviewed: 2026-10-25
review_cadence: 状态变更或范围变化时
---

# RFC-0002：客户端执行域、授权凭证模型与 A2L 宿主能力代理（五层架构）

> 客户端只有 A1 与新增的 A2L 宿主能力代理，A2/A3/A4 全在服务端；
> 用户本机的工作区与工具链由 A2L 直接持有，A4 通过 typed RPC 调用，产物留在用户电脑。

## 0. 批准后的修订（2026-10-25）

RFC-0002 已批准，状态改为 `accepted`。批准时用户补充了以下硬决策，
本节优先于下文原稿中与之冲突的表述：

1. **原 A1.5 正式命名为 A2L（宿主能力代理）。** 它不是 A1 的反向代理，
   也不重编号 A1–A4；实现落在 `host_broker/`，A4 增加 `host` 后端。
2. **工作区主形态改为 host NTFS，sandbox（WSL ext4）并存。**
   工作区用 `WorkspaceRef(kind=host|sandbox, root_handle=...)` 表达；
   源码与产物在同一侧，**禁止跨挂载**；跨 kind 编译/验证 Phase 1 不支持。
3. **A2L 安全档默认 lowpriv + ACL。** AppContainer 仅作为受限只读档单独验证；
   普通用户进程只允许 `local-dev` 的 T1 档；`user-t1` 不得宣称 T2。
4. **typed ops 优先，宿主 shell 不是默认能力。** 编译/安装属于代码执行，
   分别用 L3/L4 与 `output_roots`/安装器审计约束。
5. 下文原稿的“T3 客户端执行域”编号与既有 T3（多用户/公网）冲突，
   统一改为 **T4**；详见 [THREAT_MODEL.md](../THREAT_MODEL.md) 与
   [ADR-0015](../adr/0015-host-capability-broker.md)。
6. **A2L 绑定客户端生命周期**：客户端只有 A1 与 A2L；A2/A3/A4 全在服务器。
   A2L 随客户端启动/退出，客户端运行期间单实例、托盘可见；**不得脱离客户端
   独立常驻系统，不注册系统服务，不独立开机自启**。Phase 1 用客户端子进程/托盘
   便于调试；客户端安装包、企业设备注册在 Phase 4。
7. **host 绝对路径只存 A2L 本地 SQLite**：A2 只存
   `workspace_id + root_handle + a2l_id`；host 工作区由客户端本地选目录/注册。
8. **Phase 1 工具范围**：只接 `file_read/list_dir/find_files`；
   `search_code`、shell、写、编译、安装留到后续阶段。

Phase 1 的接口与文件清单见 [HOST_BROKER.md](../HOST_BROKER.md)。

## 1. 目标

- **G1** 服务端 agent 能调用**用户本机**的编译器/解释器/包管理器（MSVC、Node、JDK、
  cargo、conda…），构建产物留在用户文件系统内，无需在服务端 WSL 里重建工具链。
  验证：Phase 1 `tests/unit/test_host_toolchain_registry.py::test_capability_summary_hides_paths`；
  实际调用在 Phase 3 的 host 执行用例。
- **G2** 默认仍只可读工作区；越界读、任何写、任何安装都需**范围化授权**。
  验证：`tests/unit/test_host_policy.py::test_out_of_scope_write_denied`（Phase 2）。
- **G3** 授权是可撤销、有 TTL、可审计的凭证对象，而不是布尔开关。
  验证：`tests/unit/test_host_policy.py::test_revoke_takes_effect_immediately`（Phase 1）。
- **G4** 用户电脑在 NAT 后也能工作：A2L **主动外连**服务端，服务端不主动入站。
  验证：`tests/unit/test_host_broker_channel.py::test_outbound_only`。
- **G5** 无需打通 `\\wsl$\` / `/mnt/c`，不必放宽 `appendWindowsPath`，
  **服务端 A4 的 WSL 加固环境保持原样**。
  验证：`tests/unit/test_security_profile.py` 在两种后端下均全绿。
- **G6** `AGENT_EXECUTION_BACKEND=wsl`（默认）时行为与当前版本完全一致。

## 2. 非目标

- **不动 A2/A3/A4 的部署位置**：它们仍在服务端；本提案只在客户端加一层，
  并给 A4 增加一个可选后端。
- **不放弃 WSL 域**。`sandbox` 工作区仍在 ext4，Linux 目标平台构建、
  不可信仓库隔离与内核级隔离仍依赖它；`host` 工作区在用户 NTFS 上，
  两类工作区不跨挂载。
- **不在 Phase 1 支持跨 kind 编译/验证**。host 源码不通过 `/mnt` 或 `\\wsl$`
  暴露给 sandbox；需要跨 kind 时另立 RFC。
- **不在 Windows 上自研内核沙箱**（Job Object 只做资源上限，不冒充隔离）。
- **不提供"永久授权"或"全部目录授权"**。这是硬边界。
- **不改动 `events.py` 既有语义**（只增不改；新增事件走契约生成）。
- **不解决提示注入**（`THREAT_MODEL.md §5.9`），本提案会放大它的后果，需另立议题。
- **不改 `estimate_recursion_limit`**（图拓扑不变）。

## 3. 背景与证据

**现状拓扑（企业级部署）**：用户电脑上只跑 A1 前端页面；A2 控制面、A3 编排面、
A4 执行面（含 WSL 沙箱）全在服务端。工作区在服务端的 ext4 上。

**诉求**：默认严格（只读工作区），授权后可访问用户本机几乎全部文件与环境；
可调用本机编译器、产物留在本机、缺依赖可授权自动安装；并在 A1 与 A2 之间增加一层
客户端「代理」，负责客户端与服务端之间的自动化交互（如授权后安装软件）。

**约束证据**（`docs/adr/0009`、`docs/archive/phases/PHASE_G.md`）：

| 约束 | 初衷 | 代价 |
|---|---|---|
| `[interop] appendWindowsPath=false` | 防 Windows PATH 进入 WSL：DLL 劫持、宿主凭据、绕过加固 | 服务端 WSL 内没有用户本机工具链 |
| 工作区在服务端 ext4 | `/mnt` 性能差、大小写/权限位/符号链接/文件锁语义不一致 | 产物不在用户熟悉的位置 |
| `\\wsl$\` 与 `/mnt/c` 桥接 | 唯一互通手段 | 用户电脑与服务端不在同一台机器上，根本不通 |
| `access_mode` 只有三档 | fail-closed | 没有中间态；"只读"连 `shell_exec` 都禁 |
| `host_ops` 20 种 kind 白名单 | 宿主内部操作不经模型 | 无法覆盖"用用户本机 MSVC 编译" |

**核心判断**：这是把安全基线从「**技术隔离优先**」切换到「**授权 + 审计 + 可回滚优先**」。
Claude Code / Cursor 走的是后者，可行；但当前 56 条不变量中若干条建立在前者的前提上，
必须显式重写（见 §6.5）。

## 4. 目标拓扑

```
客户端 · 用户电脑
  A1     前端页面
  A2L    宿主能力代理（新增，随客户端启停）：授权中心 / 本地否决权 / 能力注册 / 客户端执行臂
           ├─ host 工作区（NTFS，客户端默认）
           ├─ 本地 workspace→absolute path 映射（绝对路径不上云）
           └─ 出站长连接（客户端主动建立，服务端不主动入站）
服务端 · 企业服务器
  A2     控制面 :8780      会话 / 认证 / 审计索引 / 计费
  A3     编排面 :8790      agent 核心：图 / orchestrator
  A4     执行面
           ├─ sandbox 后端  WSL2/ext4；加固环境不变；默认服务端
           └─ host 后端     经 A2L 执行；源码与产物在同一侧
```

**A4 的两个后端共用同一套 `ExecutionService` 契约**（`op_id` 幂等、指纹、回执），
在 `ExecutionRequest` 上增加 `target: "auto" | "host" | "sandbox"` 与
`workspace_kind`。调度时按 `WorkspaceRef.kind` 选择：Linux 目标或不可信仓库 →
`sandbox`；需要用户本机工具链/产物落本机 → `host`。Phase 1 对冲突的
`target/workspace_kind` 直接拒绝，不静默改道。

**通道方向**（工程硬约束）：用户电脑通常在 NAT 后，服务端无法主动连入。
因此 A2L 必须**主动外连**并保持长连接（WebSocket）。
服务端把执行请求放进这条通道，客户端回报结果。断开时 `host` 后端不可用，
任务**降级到 `sandbox` 后端继续**，不阻塞（与现有"降级是一等契约"一致）。

## 5. 受影响面

| 项 | 内容 |
|---|---|
| 模块 | `execution/`（新增 host 后端与 router）、新目录 `host_broker/`（A2L）、`sandbox/`（sandbox 后端保持）、`graph/nodes/approve.py`、`tools/`（scope 外写/删拒绝）、`control/`（网关注册表与上行通道） |
| 契约 | **新增**：`Grant` 模型、`ExecutionRequest.target/workspace_kind/grant_id`、`WorkspaceRef`、工具等级 **L4 环境变更**、审计 kind `grant`；`events.py` 新增 `GrantIssued`/`GrantRevoked`/`ToolchainDetected`/`InstallRequested` |
| 数据 | `control_*` 增一张 `control_host_agents`（a2l_id / user / capabilities / last_seen）；grant 只存客户端本地 SQLite，**不上云** |
| 前端 | CLI/TUI/Web/Desktop 均需新增授权 UI 与撤销入口；A2L 需独立分发与更新（可复用 `desktop/update.py` 的只检查不自动安装策略） |
| 权限 | **触及**：审批语义、沙箱后端选择、路径守卫、密钥归属、跨网络执行。安全 RFC，需人工批准 |
| 文档 | `ARCHITECTURE.md §2/§6`、`THREAT_MODEL.md §3/§5`、`adr/0009`（加固）、`adr/0011`（host_ops） |

## 6. 安全与隐私

### 6.1 A2L 的本质风险（必须先说清）

**A2L 在安全上等价于一个由服务端驱动的内网远程执行代理。**
它在客户端运行期间持续存在，因此攻击面是持续存在的：
一旦服务端被攻破，攻击者获得的是**所有正在运行的客户端**的执行能力，而不是一台服务器。
它必须绑定客户端生命周期（随客户端启动/退出，不独立常驻系统），并配单实例、
托盘可见状态、一键暂停/离线、最小权限账户与“不得以 LocalSystem/root 运行”；
这些不是可选项。

因此以下六条是本设计的**不可协商项**：

1. **本地否决权**：服务端下发的是**请求**，不是**指令**。A2L 每次执行都要独立做本地
   grant 判定；服务端签名只证明"请求来自服务端"，**不构成任何授权**。
2. **双向认证**：mTLS 或 token + 设备证书；服务端必须校验 a2l_id 与用户的绑定关系。
3. **授权不下发**：服务端只持有 `grant_id` 引用；grant 的 scope/verbs/TTL 全在客户端。
4. **可见性**：用户在 A2L 本地面板能看到"服务端让我做了什么"的完整日志，可逐条回放。
5. **一键离线**：用户可断开长连接，任务降级到 `wsl` 后端继续，不阻塞。
6. **无静默自更新**：A2L 自身更新沿用 `desktop/update.py` 的策略（只检查 + 打开下载页），
   不接受服务端推送的自动更新。

### 6.2 三处对原提案的修正

**修正 1：「工作区外只能通过命令行和环境变量操作，且绝对禁止修改/删除」不能作为约束。**

命令行就是完全的读、写、删除能力——它恰恰是当前设计里 `cp`/`mv`/`ln` 被排除在 L1
白名单之外的原因（shell 改动不产生快照、不留 diff、绕不过审批就绕过了备份）。
"只允许走命令行"不是收紧，是**放开**。

正确落点是把约束放在**执行层**：
- 工作区外默认 **只读 + 需 grant**；
- 写/删必须落在 `grant.scope` 内，由 `tools/`、`fs.py` 强制，与走不走命令行无关；
- `rm` / `mv` / `Remove-Item` / 输出重定向在 scope 外一律拒绝。

**修正 2：「授权后可操作几乎所有文件」必须 scope 化。**

LLM 驱动的 agent 每秒可发起多次调用，人不可能逐次审批。若授权是布尔开关，
现实中必然退化为「点一次允许 = 完全放开」。授权的最小粒度必须是 **(路径前缀 × 动词)**：

```python
Grant(
    grant_id: str,            # 客户端签发；服务端只持有 id 引用
    session_id, thread_id,
    scope: tuple[str, ...],   # 路径前缀，必须显式；不接受 "" 或 "/"
    verbs: frozenset[{"read", "write", "exec", "install", "network"}],
    ttl_s: int,               # 默认 1800，上限 8h；不提供永久
    issued_at, expires_at,
    reason: str,              # 触发它的具体工具调用与参数，展示给用户
    sensitive: bool = False,  # 敏感根必须逐次确认
    revocable: True,
)
```

**grant 闭环**：A3 无 grant 时得到 `GRANT_REQUIRED`；随后
`grant.request → grant.pending → grant.granted/denied`；本地撤销发
`grant.revoke`。A1 只能显示“等待本机确认”，不能代批准；最终批准在 A2L
本地 UI。敏感根（Documents/Desktop/OneDrive/浏览器 profile/.ssh 等）必须
逐次确认，不能长 TTL 大 scope 复用。撤销立即生效，并终止该 grant 下仍在
运行的本机子进程。

**P1 根目录限制**：只接受本地固定 NTFS；拒绝 OneDrive、云占位、网络/映射盘、
UNC、WSL/VM 共享、reparse 根与设备路径。路径字符串不够，必须 handle 级校验。

**文件抽象与幂等**：服务端工具通过 `FileAccess` 抽象访问 host/sandbox；
P1 只接 `file_read/list_dir/find_files`，`find_files` 不启动 shell。
每个 host op 先写本地 `op_journal`（`started/finished/unknown`），
P2 写/执行重启后未完成 op 返回 `unknown`，绝不盲目重放。

**修正 3：服务端 WSL 不该被弱化。**

原提案提到"WSL 只封装一个 Python"。按 §4 的拓扑，`sandbox` 工作区仍是
默认隔离执行域，需要完整 Linux 用户态（shell 脚本、符号链接、权限位、
`/proc`、大小写敏感）。本提案保留原样；用户本机工具链改由 A2L 提供，
不削弱服务端加固。是否把 `sandbox` 镜像瘦身为 Python-only 属于 Phase 4，
不是本决策的一部分。

### 6.3 区域模型（Zone）

| Zone | 内容 | 默认 verbs | 说明 |
|---|---|---|---|
| Z0-host | `host` 工作区（用户 NTFS） | read + 受控 write + 受限 exec | 源码与产物同侧；`.env` 等仍禁止 |
| Z0-sandbox | `sandbox` 工作区（WSL ext4） | read + 受控 write + 受限 exec | 现有 WSL 语义；不跨挂载 |
| Z1 | 授权只读区 | read* | 工作区外的源码/文档，需 `grant.scope` |
| Z2 | 本机工具链 | exec* | **上报能力清单，不暴露路径**（见 §6.4） |
| Z3 | 授权可写区 | write* | 构建产物/依赖目录，需 `grant.scope`，受快照 |
| ZX | 绝对禁止 | 无 | 任何动词、任何 grant 均拒绝 |

**ZX 清单（Deny 优先级高于一切，包括显式 grant）**：
`C:\Windows\*`、`C:\Program Files\*`、`C:\ProgramData\*`、
其他用户 profile `C:\Users\<other>\*`、`...\AppData\Roaming\Microsoft\{Credentials,Crypto,Protect}`、
`~/.ssh`、私钥（`*.pem/*.key/*.pfx/*.p12/*.jks`）、`.aws/`、`.kube/`、`.npmrc`、`.netrc`、
`credentials*.json`、浏览器 profile（Login Data / Cookies）、系统还原点与 BCD、驱动目录。

### 6.4 能力注册代替文件暴露（关键设计）

不打通 `/mnt/c`，也不把 `C:\Program Files\...` 暴露给服务端的模型。A2L 扫描本机工具链后，
只上报**能力清单**：

```json
{"toolchains": [
  {"name": "msvc", "version": "14.41", "kind": "cpp", "root": "…\\VC\\Tools\\MSVC\\14.41.34120"}
]}
```

模型看到的是「msvc 14.41 可用」，不是路径。调用时由 A2L 把 `compile(kind="cpp")`
翻译成受控的本地执行（参数白名单校验）。**这既拿到了用户本机编译器，又保留了服务端
加固环境，且不需要 `appendWindowsPath`。**

### 6.5 新增等级 L4 与被削弱的既有防线

- 保留 L0–L3；**新增 L4 环境变更**：`install`、写 Program Files、改系统 PATH、
  注册服务、UAC 提权。L4 永不自动、不被 `allow_rules` 覆盖、必须 grant 含 `install`
  且用户二次确认，审计需记录安装器哈希。
- **明确承认的退化**：
  - 本机编译 = 任意代码执行。当前 `verify` 用 `allow_manifest=False` 防的是
    「写恶意 Makefile → 下次脏写自动执行」；一旦允许本机 `make`，这条防线**被主动放弃**。
    补偿：产物限定 Z3、`make` 目标名白名单、产物目录快照、全程审计。
  - **Windows 上没有等价 bwrap**。Job Object 只能限 CPU/内存/进程数，不能做内核隔离。
    `host` 后端的安全等级是「T1 的授权版本」，**不是 T2**。必须在 UI 与文档里直说；
    `lowpriv`（默认）/`appcontainer`（受限只读）提升的是 ZX 的强制力，
    不改变“Windows 无 bwrap”的事实。
- **安装分两级**：
  - L4a 包管理器（`pip/npm/cargo/vcpkg/choco`）：需 grant；展示完整命令；网络域白名单；
    **优先 `--ignore-scripts`**；postinstall 优先落到服务端 WSL 域执行
    （postinstall = 任意代码执行）。
  - L4b 系统级安装器（MSI / exe / UAC）：**永不自动执行**，只提供「打开下载页 /
    复制命令到剪贴板」。

### 6.6 源码与产物的同侧约定

批准后的工作区模型消除了“源码在服务端、产物在客户端”的默认分裂：

- `host` 工作区：源码、编译、测试、产物都在用户 NTFS；`verify` 走 A2L，
  产物留在本机；
- `sandbox` 工作区：源码、编译、测试、产物都在 WSL ext4；`verify` 走 WSL；
- **Phase 1 不允许跨 kind 编译/验证**。把 host 源码复制到 sandbox 或反过来，
  都需要显式同步与新的授权模型，另立 RFC。
- 需要把 host 产物回传服务端时，走 A2L → A2 的受控通道，受大小上限与
  grant scope 约束；这不是默认路径。
- `verify` 与执行后端保持一致，避免“在服务端验证客户端产物”。

### 6.7 威胁模型更新

`THREAT_MODEL.md §3` 新增 **T4 级**条目：服务端被攻破后横向到所有客户端。
注意 T3 已被“多用户/公网”占用，不要复用编号。对应防线即 §6.1 的六条。
另需在 §5.9（提示注入）补充：注入的后果从“改工作区文件”升级为
“在用户本机安装软件”，这是本提案放大的最大风险面。

## 7. 兼容与回滚

- **破坏性变更**：无。`access_mode` 三档映射为预置 grant（向后兼容）；
  `host_ops` 20 种 kind 保留并追加（只增不改，与 ADR-0011 一致）。
- **回滚**：`AGENT_EXECUTION_BACKEND=wsl`（默认）回到纯 WSL 域；
  `AGENT_GRANTS_ENABLED=false` 回到 mode 语义；
  `AGENT_HOST_BROKER_ENABLED=false` 不启用 A2L；
  `AGENT_WORKSPACE_KIND_DEFAULT=sandbox` 让新工作区回到 ext4。
  四个开关互相独立。
- host 写操作在 Phase 2 单独实现快照；不直接复用只接受 WSL 路径的
  `sandbox/snapshots.py`，避免把两种路径语义混在一个后端。
- A2L 断线 → 降级 `sandbox` 后端；A2L 卸载 → 回到当前四层形态。

## 8. 可观测性

- 新增事件：`GrantIssued` / `GrantRevoked` / `ToolchainDetected` / `InstallRequested`；
  `WorkerFinished` 与 `Verification` 增 `target` 字段。
- 新增审计 kind：`grant`、`install`（含安装器哈希、来源 URL、目标前缀、a2l_id）。
- 回执落 `.agent/execution/receipts.jsonl`，增加 `grant_id`、`target`、`a2l_id`；
  撤销时记录被终止的子进程 pid 列表。
- 告警：L4 触发即告警；同一 grant 下 5 分钟内写操作超过 200 次告警；
  A2L 心跳丢失超过 60s 告警并提示降级。

## 9. 验收标准

```text
Given 工作区为 /srv/ws/proj，未签发任何 grant
When  模型请求读取 D:\Users\me\Documents\notes.md
Then  返回需要授权（ApprovalRequested，含 scope 提示），不读文件

Given 已签发 grant(scope=("D:\\Users\\me\\Documents",), verbs={"read"}, ttl_s=1800)
When  模型请求读取该前缀下文件
Then  读取成功，审计记录 grant_id 与 a2l_id

Given 上述 grant（verbs 不含 write）
When  模型请求 shell_exec("echo x > D:\Users\me\Documents\a.txt")
Then  被拒绝（scope/verb 不匹配），不改文件

Given 已签发 grant(scope=("D:\\proj\\build",), verbs={"write"})
When  模型请求写入 C:\Windows\System32\evil.dll
Then  被 ZX 拒绝（Deny 优先级高于 grant）

Given 已签发含 install 的 grant
When  模型请求 npm install lodash
Then  展示完整命令与网络域，要求二次确认；执行时带 --ignore-scripts

Given 用户点击撤销某 grant
When  该 grant 下有客户端子进程正在运行
Then  子进程被终止，后续请求立即被拒，审计记录 GrantRevoked

Given A2L 长连接断开
When  agent 需要编译
Then  降级到 wsl 后端继续，不阻塞，RunFinished 标注 backend=fallback

Given 配置 AGENT_EXECUTION_BACKEND=wsl 且未启用 A2L
When  执行任意任务
Then  行为与当前版本完全一致（回归套件全绿）
```

验收锚点写入 `docs/acceptance/acceptance-criteria.yaml`，链接到
`tests/unit/test_host_policy.py`、`tests/unit/test_host_toolchain_registry.py`
与 `tests/integration/test_host_broker_readonly.py`。

## 10. 替代方案与代价

| 方案 | 优点 | 代价 / 风险 | 为什么没选 |
|---|---|---|---|
| A 服务端打通 `/mnt/c` 式桥接 | 改动集中在服务端 | 源码与用户电脑不同机，物理上不通 | 不可行 |
| B 全部搬到用户电脑（A2–A4 本地） | 工具链问题一次解决 | 放弃企业级集中部署；工作区与审计分散到各客户端 | 与既有企业形态冲突 |
| C **A4 增加 host 后端 + A2L 代理**（本提案） | 保留服务端集中部署与 sandbox 加固；拿到用户本机工具链；host 工作区源码/产物同侧 | 新增一个客户端运行期间的内网执行代理（T4 风险）；测试矩阵翻倍；Windows 无 bwrap | — |
| D 远程 dev container（服务端即用户环境） | 隔离与工具链兼得 | 用户本机环境（CUDA、许可证、内网依赖）无法迁移 | 与"用用户本机环境"诉求冲突 |

## 11. 决策与后果（批准/实现后填）

- 最终选择：方案 C，A2L 宿主能力代理 + A4 `host` 后端；host NTFS 为主工作区，
  sandbox 并存；A2L 默认 lowpriv + ACL，AppContainer 仅受限只读档，
  普通用户进程仅 `local-dev` T1；A2L 随客户端启停，不独立常驻。
- 批准人 / 日期：项目作者，2026-10-25。
- 真实结果与预期差异：待 Phase 1 完成后补。
- 新增技术债：T4 横向风险、Windows 无 bwrap、host/sandbox 跨 kind 不支持、
  host 快照与安装审计尚未实现；登记见 `docs/tech-debt.yaml`。
- 关联 ADR：[ADR-0015](../adr/0015-host-capability-broker.md)。

## 12. 任务分解与验证计划

**当前交付只包含 P1（只读 + 安全演示）**；P2–P5 标记为后续，不纳入本轮范围。

| 阶段 | 任务 | 负责模块 | 验证方式 |
|---|---|---|---|
| P1 | `ExecutionRequest.target/workspace_kind/grant_id` + `WorkspaceRef` | `execution/contracts.py`、`workspace/model.py` | `tests/unit/test_execution_targets.py`、`test_workspace_ref.py` |
| P1 | A2L 进程骨架、只外连、握手/心跳/取消/幂等 | `host_broker/` | `tests/unit/test_host_broker_channel.py`、`test_host_broker_protocol.py` |
| P1 | A2L 绑定客户端启停、单实例、托盘暂停/离线 | `host_broker/lifecycle.py`、`desktop/supervisor.py` | `tests/unit/test_host_broker_lifecycle.py` |
| P1 | 本地工作区映射（绝对路径只存客户端） | `host_broker/store.py`、`control/workspaces.py` | `tests/unit/test_host_workspace_registrar.py` |
| P1 | grant/ZX/路径 canonicalization（只读 op） | `host_broker/policy.py`、`host_broker/paths.py` | `tests/unit/test_host_policy.py`、`test_host_paths.py` |
| P1 | grant 闭环 + 本地 UI（request/pending/granted/denied/revoke） | `host_broker/consent.py`、`host_broker/protocol.py` | 本地 UI 与协议测试 |
| P1 | `FileAccess` 抽象；只读三工具走 host/sandbox | `execution/file_access.py`、`tools/files.py`、`tools/search.py` | `tests/unit/test_file_access.py` |
| P1 | 本地 `op_journal`（started/finished/unknown） | `host_broker/op_journal.py` | `tests/unit/test_host_op_journal.py` |
| P1 | 根目录限制与敏感根逐次确认 | `host_broker/paths.py`、`host_broker/policy.py` | `test_host_workspace_store.py`、`test_host_policy.py` |
| P1 | host 只读后端 + 工具链能力摘要 | `execution/host_backend.py`、`execution/router.py`、`host_broker/toolchain.py` | `tests/integration/test_host_broker_readonly.py`（`-m host`） |
| P1 | 默认关闭、A2L 离线降级、文档同步 | `config.py`、`desktop/supervisor.py`、`docs/` | `pytest -m "not wsl and not llm"`、`check_docs.py --strict` |
| P2–P5 | **后续，不纳入当前交付**：写/快照、执行、安装、企业分发 | `host_broker/`、`packaging/` | 另立阶段评审 |
