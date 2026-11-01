---
layer: L2
doc_id: glossary
title: 统一语言词汇表
owner: 项目作者
status: active
last_reviewed: 2026-10-31
review_cadence: 引入新术语或发现新歧义时
criticality: 高
note: 术语歧义是 AI 出错的头号来源，本项目三套数字编号系统并存，动手前先查这里。
---

# 统一语言词汇表

**大型项目里同名不同义是 AI 出错的头号来源。** 本项目有三套数字编号系统并存、
三个词指同一物、三种不同的并发实体——动手前先查这里。

用法：写文档、写代码注释、给 AI 下指令时，都用这里的**规范名**。
别名列出来是为了"见到时能认出来"，不是为了继续用。

---

## 一、三套数字编号系统（最容易混）

| 编号 | 是什么 | 在哪 | 别混 |
|---|---|---|---|
| **L0–L4** | **命令与工具的风险分级**（也有"文档四层"的同名用法，见下）；L4 为 A2L 环境变更，规划中 | `sandbox/policy.py`、`docs/contracts/tools.yaml` | 不是开发阶段 |
| **Phase 3A / 3B / 3C / 3D / 4D** | harness 的**历史开发阶段**（均已实现） | `HARNESS.md §14` | 已全部落地，只作历史坐标，不代表当前状态 |
| **T1 / T2 / T3 / T4** | **威胁模型等级**：防误操作 / 防主动越狱 / 多用户公网 / 客户端执行域（A2L 横向） | `THREAT_MODEL.md §3` | 与命令分级无关；T4 不是 T3 的别名 |

> ⚠️ 同名冲突：`docs/` 里"L0–L4"也用来指**文档资产四层**（可执行事实 / Agent 指令 /
> 决策与边界 / 叙事与上下文）。为避免混淆，文档四层一律写全称 **"文档 L 层"**，
> 命令分级一律写 **"命令 L0–L4"** 或 "命令分级"；当前实现是 L0–L3，L4 规划中。

### 命令分级（L0–L4，L4 规划中）

| 级别 | 行为 | `-y`/`approve` 能放行吗 |
|---|---|---|
| L0 只读 | 自动执行 | — |
| L1 低风险写 | 需 `--write` 一次性授权，之后自动执行 + 审计 | — |
| L2 变更性 | 需用户确认 | 能 |
| L3 危险 | 需用户确认（标注危险） | **不能** |
| L4 环境变更（规划） | 安装、写系统目录、改 PATH、注册服务、UAC 提权 | **不能**；须 grant 含 `install` + 用户二次确认 |

---

## 二、三种并发实体（名字都很像）

| 规范名 | 谁派发的 | 深度 | 上下文 | 配置 |
|---|---|---|---|---|
| **编排 worker** | `scheduler` 按任务图依赖 | 固定 1 | 独立 messages，只注入 `session_brief` | `AGENT_ORCHESTRATOR_*` |
| **委派子智能体** | 主 agent 在 `act` 里调 `delegate_explore` | 1 | 只有系统提示 + 本次任务 | `AGENT_EXPLORE_ENABLED`（默认关） |
| **同消息并发只读工具** | 同一条 `act` 消息里的多个只读调用 | — | 共享主上下文 | `AGENT_MAX_CONCURRENT_TOOLS`（4） |

**不要**把 worker 叫成"子智能体"，也不要反过来。它们的工具白名单、预算、
取消语义都不一样。

---

## 三、三个词指同一物

| 规范名 | 别名 | 实际是什么 |
|---|---|---|
| **快照**（snapshot） | 留底、backup | 写盘前把原内容存到 `.agent/backups/<快照id>/` |

> 文档统一写"快照"。注意**目录名仍是 `backups/`**（历史原因，改名会破坏兼容），
> 不要因为目录名就把文档改回"backup"。

### 三个都叫"回退"但完全不同的东西

| 规范名 | 作用对象 | 命令 |
|---|---|---|
| **快照回滚**（undo） | **文件内容** | `agent undo` / `--last-run` / `--since` |
| **checkpoint 回退**（rewind） | **会话状态** | `agent rewind -n 1` |
| **checkpoint 分叉**（fork） | 从某个 checkpoint 开新分支，历史保留 | `agent fork` |

---

## 四、两道质量关

| 规范名 | 管什么 | 代码 |
|---|---|---|
| **verify** | 行为对不对（跑项目测试） | `tools/testrun.py`、`nodes/verify.py` |
| **review** | 代码干不干净（语法、被改弱的测试、调试残留） | `tools/review.py`、`nodes/review.py` |

两者都失败时**共用同一份修复预算**，都走 `repair` 出口。

## 五、执行路径

| 规范名 | 何时走 | 说明 |
|---|---|---|
| **direct** | 小仓库 / 单点任务 | 单 agent 快速路径：`planner → act ⇄ approval_gate → tools → verify → review → advance` |
| **orchestrated** | 文件数 ≥ `AGENT_ORCHESTRATOR_MIN_FILES`（30）且编排开启 | 独立 orchestrator 子图，并发 worker + fan-in，之后交回主 agent |

工作流的**模板名**另有一套：`direct` / `parallel_readonly` / `survey_then_implement` /
`review_loop` / `orchestrated_implement`。执行路径只有上面两种。

## 六、状态词

| 词 | 含义 | 易错点 |
|---|---|---|
| `dirty` | 本步是否改过东西 | 按命令等级判定，不只认工具名 |
| `partial` | worker 完成但验证未全通过 | 默认**不满足** `depends_mode=full` 的依赖；`any` 可消费 |
| `blocked` | 依赖 failed/blocked/cancelled，任务不会启动 | 任何 `depends_mode` 都不满足 |
| `merging` | implement 结果已进入合并队列 | **必须立刻离开 pending**，否则会被重新派发（出过 P0） |
| `degraded` | 任务图校验失败后整图降级 | 必须显式标记并发出事件，不能静默 |
| `kind`（步骤） | `explore` / `implement` / `verify` / `unknown` | 与事件的 `type` 字段、审计的 `kind` 字段**不是**一回事 |
| `ok`（验证） | **三态** True / False / None | `None` = 没跑起来，不要当 `False` |
| `skipped` | 工具能力不可用（如未配置 LSP） | 与 `ok=False` 区分开，前端显示"已跳过" |

## 七、目录与位置

| 词 | 在哪一侧 | 说明 |
|---|---|---|
| **宿主侧** | Windows 或 Linux 宿主进程 | `<cwd>/.agent/`：checkpoint、audit、steering、rate_limits |
| **沙箱侧** / **工作区** | WSL2（Windows）或 Linux 本地 | `<工作区>/.agent/`：backups（快照）、memory.md、artifacts |
| **工作区**（workspace） | 由 `WorkspaceRef.kind` 决定 | 用户用 `-C` 指定的根目录；所有读写与命令限定在此 |
| **host 工作区** | 客户端 NTFS | 桌面默认；源码与产物同侧，由 A2L 执行；不跨 `/mnt` |
| **sandbox 工作区** | WSL2 ext4 | 服务端/CI 默认；不可信仓库与 Linux 目标；bwrap/非 root |

两侧都在 `.agent/` 下但**内容不同、被保护的方式不同**，别混。
审计在宿主侧（沙箱内命令改不到），快照在工作区内（工作区可写时 agent 能改自己的留底）。

## 八、编排术语

| 词 | 含义 |
|---|---|
| `write_scope` | implement 任务被允许修改的路径白名单，在文件工具 I/O 前强制校验 |
| `session_brief` | 从 checkpoint 抽取的有界会话摘要（默认 4000 字符），子图不复制 `messages`，只注入它 |
| `fan_in` | worker 结果汇总，产出 `worker_context`（给 planner）与 `orchestration_handoff`（结构化） |
| `worktree` | implement worker 的独立 git 工作树，改完导出 patch 回主工作区顺序合并 |
| `checkpoint_every` | 每完成多少个任务形成一次 checkpoint/干预边界；**不再限制每边界能启动几个** |
| `steering` | 运行中干预（pause/cancel/add/replan），只在节点边界消费 |
| **知识产物（knowledge artifact）** | run 级、可查询的结构化理解产物，落 `.agent/artifacts/knowledge/`；不是聊天摘要。见 [RFC-0009](rfc/0009-deep-workflows-and-knowledge-artifacts.md) |
| **`DocIndex`** | 文档结构索引：document → section → chunk → anchor；含覆盖状态与未读区间 |
| **`ClaimStore`** | 结论-证据库：每条 claim 绑定 `path#anchor` 与原文 quote，供写作检索 |
| **证据矩阵（`DocumentMatrix`）** | 每篇文档的固定槽位：问题/方法/算法/实验/局限/创新/结论；空槽位用已验证 claim 回填 |
| **树状归并（tree reduce）** | 多篇矩阵/摘要按 `document_tree_fanout` 逐层合并，单次输入受 `document_tree_input_chars` 限制 |
| **引用跨度（quote span）** | quote 在归一化文档文本中的 `{start,end,page}`；`quote_verified=false` 的证据不进入下游 |
| **归属校验（attribution）** | 按句子检查“提到的文档”与引用路径是否一致；错配写 `attribution_issues`，不是语义判断 |
| **已解析文档头** | 多文档答案开头的确定性清单，列出全部路径/标题，防止树状归并把已覆盖文档说成缺失 |
| **`ArchIndex`** | 代码架构索引：模块/文件/符号 + import 边 + 测试拓扑 + 入口/热点 |
| **`ArchBrief`** | 从 `ArchIndex` 生成的架构简报：子系统、边界、风险、未决问题 |
| **`ChangeContract`** | 跨模块变更契约：受影响文件/模块、接口、验收、受影响测试、波次 |
| **接口优先波次** | 先改接口/契约、再改调用方的顺序写波次；波次之间设 gate |
| **覆盖契约** | 长文档必须报告 `complete` 或 `partial + 未读区间`，禁止“读了前 15 页却像读了全文” |
| **文档 map-reduce** | 分批抽取 claim/实体，再按 section/document 合并；不把整篇塞进一次调用 |
| **动态图构建器** | 按复杂度信号从节点库组合阶段，输出仍是 `TaskSpec[]`，复用现有 scheduler |

## 九、GUI / 会话权限术语

| 词 | 含义 |
|---|---|
| 桌面 GUI | `desktop/` 启动的 pywebview 窗口；Windows 默认经 WSL Agent Server，Linux 走本机 `run_desktop`；复用 `web/` 与 `AgentRuntime`，不是第二套执行链路 |
| `access_mode` | 会话级权限：`read_only`（只读、不运行）、`write_ask`（逐条审批）、`write_auto`（L1 自动） |
| 会话配置 | `thread_id → workspace / access_mode / model / provider / uploaded_bytes`；持久化在租户审计目录的 `gui-sessions.json` |
| 缓冲 SSE / 事件重放 | 运行由后台 task 持有，事件带递增 `id`；`after` / `Last-Event-ID` 可补齐断线期间的事件 |
| 上传收件箱 | 用户上传写入工作区 `.agent/uploads`，不授予模型改源码权限；有单文件、单次数量、会话累计上限 |
| provider profile | provider/model/base_url/成本/能力组合；单用户/桌面可在 GUI 增删改，多用户必须用 `AGENT_LLM_PROVIDERS`（profile 写入返回 403） |
| 密钥命名空间 | 桌面用默认 `coding-agent`，多用户用 `coding-agent:<user>`；key 只进 OS 密钥环 |
| 压缩并分叉 | 用确定性摘要 + 最近纯文本轮次创建新 thread；原 thread 保留，不原地删消息 |

## 十、评测术语

| 词 | 含义 |
|---|---|
| `spread` | 同一份代码 `--repeat k` 观测到的通过率波动幅度。**差异必须大于它才值得讨论** |
| `flipped` | 两轮之间结论翻转过的任务清单。通过率相同也可能成员不同 |
| `comparable` | 基线可比性标记；工作区脏 / 只跑子集 / 沙箱无 pytest 都会置 false 并拒绝写入 |
| `verdict` | 四态：`passed` / `failed` / `error`（判定没跑起来）/ `flaky`（结论取决于运气） |
| `noise_suspect` | 两臂间翻转、但在任一臂内部也翻过的任务。**不能归因于改动** |
| 隐藏测试 | 判定前才加入、从不写进工作区的测试文件；`spec` 类任务必须有 |

> 措辞上刻意**不写**「置信区间 / 显著性」：重复 2~3 次没有那种统计效力，
> 把它写成区间等于给噪声套上科学的壳。

## 十一、目标架构分层（A1–A4 + A2L）

> 不要写 L1–L4：命令风险分级已经占用 L0–L4，文档还有“文档 L 层”用法。
> 架构层一律用 **A1–A4**；客户端宿主代理固定写 **A2L**，见
> [PHASE_A.md](archive/phases/PHASE_A.md)、[ADR-0015](adr/0015-host-capability-broker.md)。

| 规范名 | 职责 | 当前锚点 |
|---|---|---|
| **A1 前端** | 渲染、SSE、审批交互、主题/字体、平台桥 | `src/ubismart/web/static/`、`src/ubismart/cli/`、`src/ubismart/tui/app.py` |
| **A2L 宿主能力代理** | 客户端 `host` 工作区、本地 grant/ZX、工具链能力、本地同意与审计；只外连 | 规划中：`src/ubismart/host_broker/` |
| **A2 控制面** | 认证、租户、会话目录、运行/审批索引、配额、Provider 档案、MySQL | `src/ubismart/web/app.py`、`src/ubismart/identity/tenancy.py` |
| **A3 编排面** | 图、LLM、审批决定、checkpoint、steering、编排 worker、成本/限流 | `src/ubismart/runtime/`、`src/ubismart/graph/`、`src/ubismart/orchestrator/` |
| **A4 执行面** | shell、文件、Git、测试、快照、worktree、上传落盘；按 `target` 路由 host/sandbox | `src/ubismart/tools/`、`src/ubismart/sandbox/`、`src/ubismart/execution/` |

| 词 | 含义 |
|---|---|
| 控制面 | A2：业务与数据管理，**不执行工具**、不直接读写工作区 |
| 编排面 | A3：决定“做什么、是否批准”，拥有 checkpoint/运行状态 |
| 执行面 | A4：唯一产生副作用的地方，做路径/能力二次强制 |
| Standalone / Agent 本地模式 | CLI/TUI 直接驱动 A3+A4，不依赖 A2、MySQL 或前端 |
| 运行索引 | A2 保存的 run 状态/时间线索引，不是 checkpoint 本身 |
| 审计索引 | A2 保存的查询索引；可信源仍是 A3 的本地哈希链 |
| `seq`（SSE） | 同一 thread 内单调的事件序号，用于 `after` / `Last-Event-ID` 重放 |
| 会话租约 | 目标态：同一会话同时只允许一个活跃 run 的租约；当前由单进程 registry 近似 |
| 执行回执（receipt） | 目标态：A4 对一次执行返回的幂等结果与快照/退出信息 |
| SecretProvider | A2 的密钥托管抽象：桌面用 OS 密钥环，服务器用 Vault/KMS/systemd-creds；WSL 不持久化 |
| 共享事件传输 | 多实例 A3→A2 的事件通道（建议 Redis Streams），按 thread 保序、短 TTL、按 `seq` 重放；单实例用内存适配器 |
| Standalone 持久化 | CLI/TUI/开发/测试/eval 的本地文件/SQLite，不依赖 A2 或 MySQL |
| SPA + esbuild/TS | 前端保持静态 SPA 与 CSP，用 esbuild/TypeScript 构建，不引入 React/Vue |
| `SessionView` | 前端单个会话的运行态：消息、计划、工具、审批、用量、流式游标与连接状态；不再使用全局单份状态 |
| `SessionStore` | 前端 `sessionViews[threadId]` 注册表；复用空闲视图，LRU 保留最多 6 个，跳过运行中/分屏视图 |
| 后台流 | 发起 SSE 时绑定所属 `SessionView`；用户切走会话后流继续，完成/失败/审批通过通知与未读提示 |
| 实时分屏 | 主区与右侧分屏各绑定一个真实 `SessionView`，各自接收 SSE、显示状态并可独立发送任务 |
| ExecutionService | A3→A4 的唯一执行入口：请求/回执/幂等/执行序号；不做审批 |
| `op_id` | 执行请求的幂等键；同一 op_id 不同 payload 是冲突 |
| 执行 `seq` | 按 run 单调的执行操作序号，**不等于** Web SSE 的事件 `seq` |
| 执行回执 | `.agent/execution/receipts.jsonl` 中的结果；重复 op_id 返回 replayed=true |
| **A2L** | 宿主能力代理：**绑定客户端生命周期**（随客户端启动/退出，不独立常驻系统；客户端只有 A1+A2L，服务端是 A2/A3/A4），只外连，持有 host 工作区、本地 grant 与 workspace→absolute path 映射；不是 A1 的反向代理，也不是 A5 |
| **root_handle** | host 工作区的不透明句柄；A2 只存 `workspace_id + root_handle + a2l_id`，绝对路径只存 A2L 本地 SQLite |
| **HostFs / FileAccess** | 服务端工具访问文件系统的抽象；Sandbox 与 Host 两种实现共享同一只读接口 |
| **op_journal** | A2L 本地操作日志：`started/finished/unknown`；写/执行恢复时防止盲目重放 |
| **敏感根** | Documents/Desktop/OneDrive/浏览器 profile/.ssh 等；必须逐次本地确认，不允许长 TTL 宽 scope |
| **本地固定 NTFS** | P1 唯一允许的 host 工作区根；拒绝 OneDrive、云占位、网络/映射盘、UNC、reparse 与设备路径 |
| **grant** | 客户端签发的 `(路径前缀 × 动词)` 授权凭证，含 TTL、可撤销；服务端只持有 `grant_id` |
| **ZX** | 绝对禁止根（系统目录、其他用户、凭据、浏览器 profile 等）；Deny 优先于任何 grant，需 OS ACL 兜底 |
| **工具链能力** | A2L 探测到的 `ToolchainRef` 摘要（id/kind/version），模型看不到绝对路径 |
| **target / workspace_kind** | 执行路由字段：`target=auto|host|sandbox`，`workspace_kind=host|sandbox`；冲突时拒绝，不静默改道 |
| **L4 环境变更** | 安装、写系统目录、改 PATH、注册服务、UAC 提权；永不自动、不被 allow_rules 覆盖 |
