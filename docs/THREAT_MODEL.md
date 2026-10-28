---
layer: L2
doc_id: threat-model
title: 威胁模型
owner: 项目作者
status: active
last_reviewed: 2026-10-28
review_cadence: 安全相关代码变动时（sandbox/ · approve.py · 审批语义 · 路径守卫 · 认证 · 跨域 · 桌面桥）
---

# 威胁模型

> 面向实现：说清**保护什么、假设对手是谁、现在挡得住什么、要挡住更强的对手还差什么**。
> 结论一句话：**默认交付是"防误操作"（T1），不是"防主动越狱"（T2）**；T2 需要额外一层内核级约束。

## 1. 资产（要保护什么）

| 资产 | 在哪 | 为什么值得保护 |
|---|---|---|
| 工作区外的用户文件 | WSL `$HOME`（`~/.ssh`、`~/.aws`、shell 历史…） | 私钥/凭据泄漏 |
| 宿主 Windows 文件 | `/mnt/c`、`/mnt/d` | 整台机器的数据 |
| 宿主进程环境 | 宿主的 `DEEPSEEK_API_KEY` 等 | 凭据泄漏、API 额度 |
| 工作区里的项目 | WSL 工作区 | 被误改/删 |
| 工作区元数据与根路径 | A2 MySQL | 路径被篡改会把执行引到错误目录 |
| revision 归档 | 本地归档盘（含完整源码） | 泄漏源码；误删后无法灾难恢复 |
| 审计与快照 | 宿主 `.agent/audit`；工作区 `.agent/backups` | 可追溯性与可回滚 |
| A2L 本地映射与 grant | 客户端本地 SQLite / 受保护目录 | 映射泄露会暴露宿主路径；grant 被篡改会越权 |
| 浏览器会话凭证 | 内存 / httpOnly Cookie / 桌面 sessionStorage | 被 XSS 读取或 CSRF 冒用会直接操作工作区 |
| 运行时配置与 API 基址 | 前端 `config.json` / 桌面注入 | 被篡改可把请求与凭证引到攻击者源站 |
| 桌面桥能力面 | pywebview `js_api` | 被远程页面或 iframe 触达会升级为本机文件/更新操作 |

**已核实（2026-10-04）**：

- 宿主进程的 `DEEPSEEK_API_KEY` **不会**进入 WSL 登录 shell（`wsl.exe … bash -lc 'echo $DEEPSEEK_API_KEY'` → 空）。
- **审计与 checkpoint 落在宿主侧**（`<宿主 cwd>/.agent/`），不是 WSL 工作区 —— 沙箱内命令改不到。
- **快照 `.agent/backups` 在工作区内**（WSL），因此工作区可写时，agent 能改动/删除自己的留底。

## 2. 信任边界与攻击面

```
模型（不可信输出）
   │  {tool, args}            ← 唯一入口，宿主解析
   ▼
宿主策略（policy / approve / tools）  ← 判定权在这里，模型自报不算数
   │  经 stdin 传给 WSL
   ▼
WSL2 登录 bash → （可选）bwrap 挂载命名空间 → 命令
   │
   ▼
Linux 用户空间（非 root）
```

攻击面：shell 命令、文件工具路径、git/deps 参数、审批交互、前端 SSE/HTML、文件上传、GUI 本地服务、checkpoint/审计文件、密钥环；前后端分离后新增浏览器跨域预检、认证 Cookie/CSRF、反代与 BFF、运行时配置、桌面桥调用方。

## 3. 两个威胁模型

### T1　防误操作（**默认交付目标**）

假设：模型**善意但会犯错**，或被**普通**（非对抗）提示误导；不主动设计逃逸。

这是默认配置（`AGENT_SHELL_SANDBOX=off`）的定位：把"手滑、越界、危险命令"挡住或交人工确认。

**验证入口**：[`tests/unit/test_policy.py`](../tests/unit/test_policy.py)、
[`tests/unit/test_approve.py`](../tests/unit/test_approve.py)。

### T2　防主动越狱（**对抗性，需要 bwrap**）

假设：模型输出（含被读取的文件内容）**主动尝试**越过工作区、读取凭据、外传数据。

默认 `AGENT_SHELL_SANDBOX=off` 不满足 T2；`untrusted` profile 强制 bwrap +
禁网后，能挡住大部分用户空间越界与网络外传，但**不防内核逃逸或恶意 WSL
发行版**，也不防拥有宿主权限的管理员。

**验证入口**：[`tests/integration/test_bwrap.py`](../tests/integration/test_bwrap.py)、
[`tests/unit/test_security_profile.py`](../tests/unit/test_security_profile.py)、
[`tests/unit/test_bwrap_args.py`](../tests/unit/test_bwrap_args.py)。

### T3　多用户 / 公网（本次新增）

假设：多个互不信任的用户通过公网访问同一 Web 服务，仓库来源不可信。

已实现的控制：

- Bearer Token 认证；未配置 token 只允许 loopback 匿名；
- per-user workspace / audit / checkpoint / steering / memory；
- registry 按 `(user, thread)` 隔离，跨用户访问返回 404；
- 非 loopback 必须有 token；远程无 TLS 默认拒绝；
- 请求体上限、按用户限流、runtime 数量上限、安全响应头；
- `untrusted` profile（bwrap + 禁网 + ask + 不信任仓库指令/记忆）；
- 秘密文件读保护 + 工具日志脱敏 + 审计哈希链；
- 单 Web 进程锁，避免多进程绕过配额。

企业账号（`AGENT_WEB_ACCOUNTS_ENABLED`）：

- 密码只存 scrypt 哈希；会话只存 token 的 sha256，Cookie 为 HttpOnly；
- Cookie 身份的非幂等请求强制双提交 CSRF + Origin/Sec-Fetch-Site 校验；
- 管理员权限在服务端校验（`/api/admin/*`），禁用用户立即吊销会话；
- 登录按来源 IP 固定窗口限流，成功/失败/登出/限流写入
  `control_auth_events`（不含密码或 token）；
- 上游 provider Key 用 AES-256-GCM 加密落库，主密钥缺失时拒绝写入；
- 主密钥不随数据库备份，备份后自动扫描；轮换脚本事务重加密并递增
  `key_version`，旧密钥轮换后立即失效；
- 按用户/日原子预留额度（独立 reservation 行 + owner/expires_at），
  运行结束按 id 释放；多实例只清理自己的过期预留，不会误伤活动运行；
- 计量按审计 `record_hash` 幂等，resume 用 `run_epoch` 取累计差值，
  成本按 USD 记账、人民币展示与配额判断。

设计冻结、尚未实现（见 §5.13 与 [RFC-0003](rfc/0003-frontend-backend-separation.md)）：

- 公网 BFF Cookie + OIDC/SSO（当前企业账号为本地密码）；
- CORS 精确允许列表、错误响应也带 CORS 头、`Vary: Origin`；
- 单飞 refresh 与短期 Bearer 轮换；
- 全链路 `X-Request-ID` 与稳定错误 `code`；
- 前端运行时基址，禁止把凭证或基址写死。

残余风险：宿主管理员、WSL/内核漏洞、恶意 WSL 发行版、绕开关键词的递归检索、
以及审计链可被有主机权限者整体重写；以上与是否前后端分离无关。

**验证入口**：[`tests/unit/test_tenancy.py`](../tests/unit/test_tenancy.py)、
[`tests/unit/test_web_auth.py`](../tests/unit/test_web_auth.py)、
[`tests/unit/test_web_app.py`](../tests/unit/test_web_app.py)。

### T4　客户端执行域（A2L，**新增**）

假设：服务端被攻破或存在恶意内部人员，攻击者试图把 A2L 当作内网横向执行的跳板；
或提示注入诱导 agent 在用户本机执行命令、安装软件、读取凭据。

T4 不是 T3 的别名：T3 是“多用户经公网访问同一服务”，T4 是“服务端 → 所有已部署
客户端”的横向风险。对应设计见 [ADR-0015](adr/0015-host-capability-broker.md) 与
[RFC-0002](rfc/0002-native-execution-domain-and-grant-model.md)。

已规划的控制：

- A2L **只主动外连**，不开入站端口；设备证书/mTLS 绑定 `a2l_id` 与用户；
- A3 本地设备台账（`DEVICE_POLICY=enforce`）按证书指纹预登记/吊销；
  吊销后 acceptor 周期重载并断开在线连接；
- A2L 用固定 Ed25519 公钥验签运行策略（写/exec/toolchain/限额），
  服务端静态改策略不能生效；
- A2L 可对连接 nonce 做设备证明：Windows TPM 背书密钥（CNG）优先，
  不可用回退软件 Ed25519；A3 用台账公钥校验；
- `security_profile=untrusted` 禁止 host 工作区与 `host_exec`；host 后端
  只面向可信本机仓库，`agent doctor` 显示隔离能力真实可用性；
- 服务端请求不是授权；A2L 每次执行都独立做本地 grant + ZX 判定；
- grant 的 scope/verbs/TTL 只存客户端，服务端只持有 `grant_id`；
- ZX 由 `lowpriv` + OS ACL 强制（默认）；`appcontainer` 提供只读 helper 与
  **受控 exec**；`user-t1` 仅 `local-dev`；
- 写/执行先落 `op_journal`，同一 `op_id` 不重放（`OP_UNCERTAIN`）；
  写前在工作区外留快照，恢复时校验 sha256；
- `host.exec` 只接受已注册 toolchain 的 argv，不经 shell；只在 AppContainer
  内运行，只对 `output_roots` 可写，默认无网络；安装（`host.install`）永不执行；
- typed ops 白名单，未知 op fail closed；本地同意 UI + 可撤销/可过期 grant；
- 本地哈希链审计在 agent 不可写目录，异步转发 A2；
- A2L 离线时 A4 降级 `sandbox`，不假装 host 可用；
- A2L 绑定客户端生命周期：随客户端启动/退出，客户端运行期间最小权限、
  单实例、崩溃重启、托盘可见、一键暂停/离线；不注册系统服务、不独立开机自启；
  不得以 LocalSystem/root 运行，按交互用户会话隔离；
- 自身更新只接受签名包/管理员策略，不接受服务端静默推送。

残余风险：Windows 没有等价 bwrap；`user-t1` 只是 T1；AppContainer exec 仍非
T2，toolchain root 需整树授 `RX`，超时对孙进程是 best-effort；构建脚本/安装器
本身就是任意代码执行；提示注入可把“改文件”升级为“在用户本机执行命令/装软件”；
设备证明没有 PCR quote/远程启动度量，软件回退只证明密钥持有；设备台账是本地
JSON，并发写按最后写入生效；服务端仍有机会在用户不仔细看的情况下诱导同意；
客户端运行期间 A2L 持续在线，攻击窗口覆盖整个客户端会话，必须靠最小权限与
本地可见性补偿。

**验证入口**（Phase 1 落地后）：
`tests/unit/test_host_policy.py`、`tests/unit/test_host_broker_channel.py`、
`tests/integration/test_host_broker_readonly.py`（`-m host`）。

## 4. 现有防线分级

| 防线 | 实现 | 归属 | 回归测试 |
|---|---|---|---|
| 命令分级 L0–L4（L4 规划）+ 复合取最高 | `sandbox/policy.py` | T1 | [`test_policy.py`](../tests/unit/test_policy.py) |
| 危险模式拦截（`rm -rf`、`sh -c` 包装、管道执行等） | `policy.DANGER_PATTERNS` | T1（**已知模式的枚举**，非完备） | [`test_policy.py`](../tests/unit/test_policy.py) |
| 未知命令/解析失败 fail-closed 为需确认 | `policy` | T1 | [`test_policy.py`](../tests/unit/test_policy.py) |
| 只读命令**参数越界升级**（`~`、外部绝对路径、`..`、`$()`/变量） | `policy._escalate_auto_command` | T1（词法 best-effort） | [`test_policy.py`](../tests/unit/test_policy.py) |
| 引号感知（字面量不误判，`$()`/`sh -c` 照扫） | `policy._mask_quoted` | T1 | [`test_policy.py`](../tests/unit/test_policy.py) |
| `.agent` 在 shell/git 路径的防护 | `policy`（整树 `git add`、`.agent` 引用 → L2） | T1 | [`test_policy.py`](../tests/unit/test_policy.py) |
| 路径双重校验（词法 + realpath，含悬空链接） | `pathguard` + `fs` | **接近 T2**（对文件工具路径是硬约束） | [`test_pathguard.py`](../tests/unit/test_pathguard.py)、[`test_fs_guard.py`](../tests/unit/test_fs_guard.py)、[`test_file_tools.py`](../tests/integration/test_file_tools.py) |
| 工作区根路径由 A2 生成 + 白名单校验 | `control/workspaces.py` + `paths.py` | T1（A2 层；符号链接由 A4 的 realpath 补齐） | [`test_workspace_paths.py`](../tests/unit/test_workspace_paths.py)、[`test_workspace_registry.py`](../tests/unit/test_workspace_registry.py) |
| 审批在节点、执行在 `tools` 唯一咽喉 | `graph/nodes/{approve,tools}.py` | **接近 T2**（前端绕不过） | [`test_approve.py`](../tests/unit/test_approve.py)、[`test_nodes.py`](../tests/unit/test_nodes.py) |
| 资源上限（CPU/文件/进程，沙箱内 `ulimit`+`timeout`） | `sandbox/limits.py` | T1 | [`test_limits.py`](../tests/unit/test_limits.py)、[`test_sandbox_timeout_cleanup.py`](../tests/unit/test_sandbox_timeout_cleanup.py) |
| 审计（宿主侧、追加写、失败显式报出） | `audit/` | **接近 T2**（沙箱改不到） | [`test_audit.py`](../tests/unit/test_audit.py)、[`test_audit_chain.py`](../tests/unit/test_audit_chain.py) |
| 可选内核级隔离 `AGENT_SHELL_SANDBOX=bwrap` | `sandbox/wsl_exec.py` | **T2 的一层**（opt-in，默认 off） | [`test_bwrap_args.py`](../tests/unit/test_bwrap_args.py)、[`test_bwrap.py`](../tests/integration/test_bwrap.py) |
| 前端只消费事件；GUI HTML 经 DOMPurify/CSP | `tui/` `web/` `desktop/` | 接近 T2（降低 XSS 面、无旁路执行） | [`test_web_app.py`](../tests/unit/test_web_app.py)、[`test_web_auth.py`](../tests/unit/test_web_auth.py)、[`test_cli_render.py`](../tests/unit/test_cli_render.py) |
| 编排写 worker：独立 worktree + `write_scope` + 合并前快照 | `orchestrator/implement.py`、`nodes.py` | T1（仍是 `--write` 预授权下的受控写路径） | [`test_write_scope.py`](../tests/unit/test_write_scope.py)、[`test_atomic_merge.py`](../tests/unit/test_atomic_merge.py)、[`test_implement_worker.py`](../tests/integration/test_implement_worker.py) |
| worker 提议任务只能只读，host 复核角色/去重/依赖/总量 | `orchestrator/graph.py::validate_proposals` | T1（不允许 worker 自由扩大写范围） | [`test_orchestrator.py`](../tests/unit/test_orchestrator.py) |
| A2L 只外连、设备证书、本地否决权 | `host_broker/server.py`、`host_broker/consent.py` | **T4 的第一层**（服务端不能静默下发指令） | Phase 1 planned：`test_host_broker_channel.py` |
| A2L 绑定客户端生命周期、最小权限、单实例、托盘暂停/离线 | `host_broker/lifecycle.py`、`desktop/supervisor.py` | T4（降低长期暴露与误操作） | Phase 1 planned：`test_host_broker_lifecycle.py` |
| grant 为（路径前缀 × 动词）+ TTL/撤销 | `host_broker/policy.py` | T1（授权语义；lowpriv+ACL 才接近 T2） | Phase 1 planned：`test_host_policy.py` |
| ZX 由 lowpriv + ACL 强制（AppContainer 仅受限只读） | `host_broker/policy.py`、Windows ACL | **T4 的硬边界**；`user-t1` 仅 T1 | Phase 1 planned：`test_host_paths.py` |
| 根目录只接受本地固定 NTFS；敏感根逐次确认 | `host_broker/paths.py`、`host_broker/policy.py` | T4（缩小暴露面） | Phase 1 planned：`test_host_workspace_store.py`、`test_host_policy.py` |
| 本地 `op_journal` + unknown 语义 | `host_broker/op_journal.py` | T1/T4（防止恢复时盲目重放） | Phase 1 planned：`test_host_op_journal.py` |
| host 路径 handle 级 canonicalization（junction/UNC/ADS） | `host_broker/paths.py` | 接近 T2（对 typed ops 是硬约束） | Phase 1 planned：`test_host_paths.py` |
| 工具链只上报能力摘要，不暴露绝对路径 | `host_broker/toolchain.py` | T1/T4（减少模型拼路径） | Phase 1 planned：`test_host_toolchain_registry.py` |

## 5. 从 T1 升到 T2 需要的前置条件

按"防线层"给出：现状 → 缺口 → 达标要做什么。

### 5.1 文件系统隔离（最关键）

- **现状**：默认 off，命令以用户身份运行，`/home` 可读；opt-in `bwrap` 已能隐藏 `/home` `/root` `/mnt`。
- **缺口**：默认不隔离；bwrap 依赖非特权 user namespace（多数 WSL 可用，但不保证）。
- **达标**：
  1. 默认改为 `bwrap`（或提供"无 bwrap 即拒绝启动"的部署档）；
  2. 只读根 + **白名单** bind（只挂工作区与必需系统目录），而非现在的固定集合；
  3. 明确"工作区是唯一可写点"。

### 5.2 网络隔离

- **现状**：bwrap 模式支持 `AGENT_SHELL_NETWORK=false`，会加 `--unshare-net` 完全断网；
  `untrusted` 与 `production/ops` profile 强制关闭网络。默认 local 仍为 `true`，
  因为 `deps_install`/构建通常需要网络。
- **缺口**：默认 local 不隔离网络；`off` 后端没有内核级网络限制。
- **达标**：按任务设置网络档（deny / allowlist proxy），默认对 implement/测试任务收紧。
  这是"安全 vs 可用"的直接取舍，必须显式配置而非默认放开。

### 5.3 进程与能力

- **现状**：`--unshare-pid --unshare-user --unshare-ipc --new-session
  --cap-drop ALL`；系统目录仍以只读方式挂载。
- **缺口**：未限制可执行集合（只读 `/usr` 仍可运行任意系统程序），也没有 seccomp。
- **达标**：限制可执行集合，并配合 5.4 的 syscall 过滤。

### 5.4 内核对接口（seccomp）

- **现状**：无 syscall 过滤。
- **达标**：`bwrap --seccomp` 加载策略，限制危险 syscall（如 `ptrace`、`mount`、原始 socket）。

### 5.5 命令解析

- **现状**：**词法守卫是 best-effort、可绕过**（编码、变量、间接执行、新变体无穷）。
- **结论**：**T2 不能把它当兜底** —— 必须由 5.1 的 OS 隔离兜底；词法守卫只作为"减少确认噪音 + 拦截明显危险"的 T1 手段。

### 5.6 资源隔离

- **现状**：`ulimit`；对子进程/新会话的约束不完全。
- **达标**：cgroup v2（memory/cpu/pids 硬上限），不依赖进程自我约束。

### 5.7 审计完整性

- **现状**：审计在**宿主侧**（好），但快照在**工作区内**（可被 agent 改）。
  编排合并前的快照同样落在工作区 `.agent/backups/`。
- **现状（新增）**：审计落盘前对常见 secret（`sk-*`、GitHub/GitLab token、
  AWS key、JWT、Slack/Google token、私钥块、`password=...`、数据库连接串）
  做模式脱敏；参数值递归处理。
- **现状（新增）**：shell / search / file_read 回灌模型前也做同一套脱敏，
  避免配置文件里的凭据直接进入模型上下文。
- **缺口**：脱敏是模式匹配，不保证覆盖所有凭据格式；审计与工具输出都不能
  替代真正的凭据管理；审计本身仍可被宿主用户改。
- **达标**：快照也移出工作区或加校验和；审计考虑远端/只追加存储，防"改完再抹痕"。

### 5.8 凭据与写入

- **现状（新增）**：WSL 命令通过 `env -i` 启动，宿主环境变量不再进入 Linux
  子进程；HOME 由脚本内 `getent passwd` 补回。`printenv` / `env` 升级为 L3。
- **现状（新增）**：读保护覆盖 `.env`、私钥/证书、`.npmrc`、`.netrc`、
  `.aws/**`、`.kube/**`、`credentials*.json`、`*.tfstate` 等；shell 中
  显式引用、`cat .env*` 这类通配形状、以及检索密码关键词都会升级为 L2。
- **现状（新增）**：`file_write`/`file_edit` 默认拒绝上述目标，
  `AGENT_WRITE_DENY_GLOBS` 可继续追加。
- **现状（新增）**：`AGENT_ALLOW_RULES` 支持 `tool:glob` 持久放行，但只覆盖
  原本会 ASK 的 L2，不会绕过 L3 或 `deny` 模式。
- **缺口**：shell 重定向仍可能写敏感路径（需 L2 审批；allow rule 也可能放宽）；
  读保护仍是文件名/模式匹配，无法识别任意自定义凭据。
- **达标（T2）**：shell 写路径接入同一套内容级策略；凭据改由 secret manager
  注入，而不是落在工作区文件里。

### 5.9 提示注入（真正的 T2 主战场）

- **现状**：读到的文件内容与用户指令**同等信任**，都进模型上下文。
- **untrusted 模式**：不注入仓库 `AGENTS.md`/`CLAUDE.md`，不读取仓库
  `.agent/memory.md`；记忆移到仓库外。文件内容本身仍可能诱导模型行动。
- **达标**：内容信任分级（工具结果标记为"数据、非指令"）、对"由文件内容触发的危险动作"强制人工确认。这是模型层问题，隔离层解决不了。

### 5.10 审批与前端

- **现状**：策略在宿主、前端绕不过；`--yes`/`approve` 只自动放行 L2，
  **L3 始终需要人工确认**。
- **Web**：Bearer Token 认证；未配置 token 只允许 loopback 匿名；非 loopback
  必须 token + TLS（本进程或可信反向代理），否则拒绝启动。
- **桌面 GUI**：只绑 loopback；每次启动生成随机 token，**不允许匿名**；随机端口；
  `TrustedHostMiddleware` 限制 Host；单实例锁。窗口关闭即停止服务。
- **访问模式**：只读会话在策略层拒绝 shell/tests/git/deps/LSP，并关闭自动
  verify 与编排 implement；`symbol_search` 强制 AST；写模式必须由服务端
  `--write` 显式开启，持久化配置不能反向提权。
- **上传**：单文件/数量/工作区跨会话累计限额、basename 清洗、SandboxFs 路径双重校验、写审计；
  写入 `.agent/uploads`，不自动执行/解压，不授予模型源码写权限。
- **密钥**：单机 GUI 注入的 key 只进 OS 密钥环；企业模式由管理员写入
  `control_provider_keys`，只存 AES-256-GCM 密文与后四位，主密钥来自
  `AGENT_SECRET_MASTER_KEY`/KMS，缺失时拒绝写入；解密失败 fail closed，
  不回退 keyring。任何路径都不回传明文、不写 `.env`、不写日志；
  连通性测试显式 `trust_env=False`，不走环境代理；
  `base_url` 始终由管理员配置。
- **达标**：T2 场景仍应禁用 `approve` 模式（L2 仍会被自动放行）；只读 GUI
  不应被当成“可以运行测试”的沙箱。

### 5.11 工作区注册表与归档

- **现状（新增）**：根路径只由 A2 的 `WorkspaceStore` 生成；托管来源必须落在
  `AGENT_WORKSPACE_ROOT` 内，挂载来源必须落在 `AGENT_WORKSPACE_MOUNT_ROOTS`
  显式白名单内。客户端提交的 `root_path` 不再被信任。
- **现状（新增）**：A2 只做词法校验；Windows 上的 A2 无法解析 WSL 路径，
  符号链接逃逸必须由 A3/A4 在沙箱内 `realpath` 补齐。
- **现状（新增）**：项目上传只接受 zip / tar / tar.gz；拒绝绝对路径、`..`、
  符号链接、硬链接、设备/FIFO、加密 zip；限制文件数、单文件、总量与解压比；
  `.git` 与 `.agent` 不随上传包导入，避免 hooks/config 执行与审计污染。
- **现状（新增）**：revision 先在 staging 完成 git 提交与归档，再原子激活；
  任何一步失败都不会留下半成品工作区。
- **现状（新增）**：企业 `clamav` 不可用时 fail-closed；桌面 `none` 必须在 UI
  明示“未扫描”。
- **缺口**：归档盘保存完整源码，当前依赖磁盘权限与运维备份；尚未做归档加密
  与异地复制。无内部 Git remote，代码没有第二份远端。A3 导入/上传任务状态
  在进程内存，重启后未完成任务丢失。
- **现状（新增，M4）**：A2→A3 可启用 `AGENT_INTERNAL_HMAC_KEY`，对
  `X-Agent-Context` 签名；A3 验签失败 401。`X-Agent-Context` 不进入
  access log。未配置 key 时只依赖内部 Bearer token（兼容旧部署）。
- **现状（新增，M4）**：归档恢复只接受归档根内、sha256 匹配的文件；
  解包允许 `.git` 但拒绝符号链接/硬链接/设备；恢复前生成 pre-restore
  revision，替换采用“备份 + 原子 rename”。
- **现状（新增，M4）**：桌面启动器在注册表启用时检查 WSL 发行版
  （非 root、`appendWindowsPath=false`），不满足则拒绝启动；专用
  `CodingAgent` 发行版已包含对应 `/etc/wsl.conf`。
- **现状（新增，M4）**：会话级运行租约落在 A2 的 MySQL，TTL 自动过期；
  幂等键覆盖工作区创建/导入/上传启动/回滚。SSE 客户端断开时租约不会立即
  释放（由终态事件或 TTL 释放），避免断开即被重复启动。
- **达标（部署要求）**：归档盘与工作盘分开挂载、`0700`、整盘加密，并至少具备
  云盘快照/RAID/异地备份之一；恢复演练写入 `runbook.yaml`。

### 5.12 客户端执行域（A2L，Phase 1 起）

- **现状**：Phase 1 只实现只读 typed ops（`host.list_dir/stat/read_text/
  toolchains/env.inspect/health`），不开放宿主写、执行与安装；A2L 只外连，
  默认关闭。工作区 `kind` 区分 `host`（NTFS）与 `sandbox`（ext4），禁止跨挂载。
  P2–P5（写/编译/安装/企业分发）是后续工作，不纳入当前交付。
- **缺口**：Windows 没有等价 bwrap；普通用户进程档（`user-t1`）只能防误操作，
  不能防主动越狱。AppContainer/低权限账户能把 ZX 变成 OS 级拒绝，但仍不是
  内核级隔离。
- **达标（T4 基线）**：
  1. A2L 默认以 `lowpriv` 运行并对 ZX 配 ACL deny；`appcontainer` 仅受限只读档；
  2. 服务端请求必须经本地 grant 判定，服务端不能持有/下发 scope；
  3. `host.exec` 与 `host.install` 分别按 L3/L4 处理，限制 `output_roots`、
     网络域、安装器来源/哈希；不可信仓库留在 `sandbox`；
  4. 本地审计在 agent 不可写目录，哈希链 + 设备签名，异步转发 A2；
  5. A2L 离线时降级 `sandbox`，不把 host 请求悄悄改到 WSL；
  6. A2L 绑定客户端生命周期，不注册系统服务、不独立开机自启；不得以
     LocalSystem/root 运行；单实例、托盘可见、一键暂停/离线、随客户端签名更新；
     host 绝对路径只存客户端；
  7. grant 必须走本地闭环（request/pending/granted/denied/revoke），
     A1 不能代批准；敏感根逐次确认，不能长 TTL 大 scope；
  8. P1 根目录只接受本地固定 NTFS；拒绝 OneDrive、云占位、网络/映射盘、
     UNC、WSL/VM 共享、reparse 根与设备路径；
  9. `FileAccess` 只暴露只读三工具；每个 host op 先写本地 `op_journal`，
     P2 写/执行恢复时未完成 op 返回 `unknown`，绝不盲目重放。
- **已知不可解决**：提示注入仍可诱导用户同意；构建脚本/安装器仍是供应链风险；
  拥有宿主管理员权限的人仍可绕过 A2L；客户端运行期间 A2L 持续在线，扩大攻击窗口。这些必须在 UI
  与答辩材料中直说，不能把 `user-t1` 包装成 T2。

### 5.13 前后端分离、浏览器凭证与桌面桥（设计冻结）

- **现状**：前端与 API 同源，服务端没有 CORS；Web 认证是长期静态 Bearer，
  token 存在 `sessionStorage`，不过期、不刷新、不注销；CSP 使用 Alpine
  CSP 构建，不需要 `unsafe-eval`；桌面桥注入本地页面，当前未加载远程 URL。
- **新增攻击面**：跨域预检与 CORS 配置、refresh Cookie 与 CSRF、BFF 会话、
  反代对 SSE/上传的缓冲与超时、运行时配置注入、远程页面触达桌面桥。
- **目标控制**（[ADR-0016](adr/0016-frontend-backend-topology-and-runtime-config.md)、
  [ADR-0017](adr/0017-auth-model-for-separated-deployment.md)、
  [ADR-0018](adr/0018-desktop-bridge-boundary.md)、
  [RFC-0003](rfc/0003-frontend-backend-separation.md)）：
  1. 基址只来自运行时配置，非法配置失败关闭；配置不含密钥，且不能扩大 CSP；
  2. Web 凭证只放内存，禁止 localStorage；短期 TTL、单飞刷新、注销吊销；
     401 与 403 分离；
  3. CORS 只对精确允许列表开放，禁止 `*`/`null`/任意 Origin 反射；
     `allow_credentials` 仅 Cookie 模式；CORS 最外层，错误响应也带头；
  4. Cookie 模式使用 `HttpOnly; Secure; SameSite`，配 CSRF；
     公网优先 BFF 同源，避免第三方 Cookie 限制；
  5. 全链路 request ID；日志与错误详情不回显 token/Cookie/CSRF；
  6. SSE 禁缓冲、禁压缩、心跳大于代理空闲超时；代理不自动重试已发送的流；
  7. 桌面桥只绑定本地 origin 与顶层窗口；远程页面永不注入 `js_api`；
     能力保持窄面，服务端对桌面专属端点二次判定；更新验签、诊断脱敏。
- **残余风险**：
  - 本地页面 XSS 仍可读取内存凭证或调用桥；只能靠 CSP、清洗、短 TTL、
    窄桥能力与本地确认降低影响，不能假设本地页面绝对可信；
  - CORS 配错（通配、反射）会把响应暴露给恶意源；CORS 本身不是认证；
  - 第三方 Cookie 被浏览器拦截时跨站 Cookie 失效，公网必须走 BFF；
  - 反代缓冲/压缩/超时会破坏 SSE 或延迟吊销生效；
  - CDN 若缓存了带凭证的响应或缺 `Vary: Origin`，会跨用户泄漏；
  - request ID 若来自不可信来源且不校验，可用于日志注入；
  - 分离不改变 A2 具备执行能力这一事实；A2 仍只能 loopback，
    暴露公网仍是最高风险。
- **缺口**：九步设计均已实现（运行时配置/基址注入、配置失败关闭、版本握手、
  前端认证适配/单飞刷新、错误协议与 request ID、CORS 允许列表、
  SSE 心跳与代理规则、上传限额与幂等、桌面桥 origin 绑定、独立发版契约）。
  真实跨域/反代/桌面窗口的部署 E2E 仍待补；这不改变 T3/T4 的残余风险结论。
  BFF 服务本体由部署侧提供，尚未在本仓库落地。实现前不得把当前形态
  宣称为"已支持公网多用户"。
- **验证入口**：验收项 `REQ-202`–`REQ-212`；实现后补认证、CORS、
  SSE 代理、上传与桥 origin 的契约测试。

## 6. 结论

- **默认 local profile = T1**：防误操作 + 可审计 + 可回滚。
- **untrusted profile / 公网 Web = 接近 T2 的工程基线**：bwrap + 禁网 +
  强制 ask + 租户隔离 + 秘密读保护 + 工具输出脱敏 + 审计链；但仍不防内核逃逸、
  恶意 WSL 发行版与拥有宿主权限的管理员。
- **production/ops profile = 只读审计档**：bwrap + 禁网 + `approval_mode=deny`，
  运行时强制关闭 `allow_write`；L2/L3 一律拒绝。它不是"安全的生产变更通道"，
  而是"允许只读排查、不允许任何变更"的档位。
- **T4 客户端执行域（A2L）**：只外连 + 本地 grant/ZX + lowpriv/ACL（AppContainer 仅受限只读）；
  它不是 T2，`user-t1` 只用于 `local-dev`。构建/安装是代码执行，必须 L3/L4。
- **T3 前后端分离增强（设计冻结）**：运行时基址、三档认证、CORS 允许列表、
  BFF Cookie/CSRF 与 request ID 已定义，但代码未改；实现前不得宣称
  已具备公网多用户部署能力。桌面桥只对本地页面开放，远程页面不注入 `js_api`。
- **答辩建议**：把 T1/T2/T3/T4 的边界讲清楚——"默认 T1；公网模式强制 T2 基线，
  生产只读用 production profile，客户端执行域是 T4 且默认关闭"，比含糊地说
  "很安全"更站得住。

### 6.1 高风险安全项的边界声明（TD-011 / TD-013）

> 本节是 TD-011 与 TD-013 的**唯一对外口径**。README、答辩材料、发行说明与 UI
> 只能使用“可宣称”列；出现“不可宣称”或“禁止措辞”列的内容即视为安全表述缺陷。

#### TD-011　Windows host 执行后端不是 T2

**一句话**：host 后端是“T1 + 部分 OS 级 ZX”，不是 bwrap 等价的内核隔离；
不可信仓库必须留在 `sandbox`。

| 维度 | 边界 |
|---|---|
| 可宣称 | 默认关闭；写需本地 grant，写前在工作区外留快照，`expected_sha256` 冲突检测，同一 `op_id` 不重放；`host.exec` 只接受**已注册 toolchain 的 argv**、不经 shell、只在 AppContainer 内运行、默认不授予网络能力、只对 `output_roots` 可写；`host.install` 永不自动执行；`untrusted` profile 禁用 host 工作区与 `host_exec` |
| 不可宣称 | 与 bwrap 等价的文件系统/网络/进程隔离；内核级隔离或 syscall 过滤；对已注册 toolchain、编译器、构建脚本自身的防护；对孙进程的硬超时；对宿主管理员、被攻陷客户端或提示注入的防护；lowpriv/ACL 与设备注册的全自动部署 |
| 硬边界 | toolchain root 需整树授 `RX`，**编译器/构建脚本本身就是任意代码执行**；依赖 `.git` hooks、Makefile、安装器时，授权执行即等于授权代码 |
| 启用前置 | 只对**可信本机仓库**启用；企业启用需 `DEVICE_POLICY=enforce` + 签名策略 + TLS；先完成 lowpriv/ACL 部署与 `agent doctor` 隔离项验收；不可信仓库一律 `sandbox` |
| 证据 | `tests/unit/test_host_broker_write.py`、`test_host_broker_exec.py`、`test_host_policy.py`、`test_host_paths.py`；`tests/integration/test_host_broker_readonly.py`（`-m host`）；`agent doctor` 的 host 隔离项 |
| 禁止措辞 | “T2”“内核级隔离”“沙箱隔离”“绝对禁止”（策略 glob 是 T1 约束，不是绝对边界） |

#### TD-013　A2L 的横向风险不能归零

**一句话**：A2L 是 T4 客户端执行域，**服务端失陷是跨所有在线客户端的横向风险**；
当前措施只降低风险，不提供“零横向风险”保证，默认关闭。

| 维度 | 边界 |
|---|---|
| 可宣称 | A2L 只主动外连、不开入站；mTLS/设备指纹；hello nonce；本地设备台账、注册/吊销与断线；固定公钥验签运行策略；连接 nonce 的 TPM 背书密钥或软件 Ed25519 证明；服务端请求不等于授权，本地 grant/ZX 是最终闸门；grant scope/verbs/TTL 不落服务端；grant one-shot/限流/断线撤销；`untrusted` 禁用 host；离线降级 `sandbox`；本地审计链 |
| 不可宣称 | PCR quote / 远程启动度量；对“设备未被攻陷”的远程证明；CSR 或自动证书签发；控制面 MySQL 设备表；红队演练结论；对已被攻陷客户端、宿主管理员或用户误点“同意”的防护；对服务端社工诱导的防护 |
| 硬边界 | 软件证明只证明密钥持有；服务端仍可在用户不仔细看时诱导同意；A2L 在整个客户端会话期间在线，攻击窗口等于客户端会话；构建/安装仍是供应链代码执行 |
| 启用前置 | 企业批量启用前：`DEVICE_POLICY=enforce` + 签名策略 + TLS + 设备台账预登记；完成吊销演练与红队；不可信仓库禁用 host 后端 |
| 证据 | `tests/unit/test_host_broker_tls.py`、`test_host_broker_channel.py`、`test_host_devices.py`、`test_host_attestation.py`、`test_host_policy_doc.py`；`tests/integration/test_host_broker_readonly.py`（`-m host`） |
| 禁止措辞 | “远程证明”“设备可信”“服务端授权即可执行”“零横向风险”“服务端静默自动更新” |

## 7. 维护

本文档随防线变动更新。新增/修改安全相关代码时，对照 [ARCHITECTURE.md](ARCHITECTURE.md) 第 6 节不变量表与本表
确认：**属于 T1 的手段不要被当成 T2 的保证**。涉及认证、跨域、Cookie、
反代或桌面桥时，同时对照 [RFC-0003](rfc/0003-frontend-backend-separation.md)
与 ADR-0016–0018，并更新 §5.13 的"现状/缺口"。
