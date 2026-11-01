---
layer: L3
doc_id: acceptance-phases
title: Harness 阶段验收（归档）
owner: 项目作者
status: snapshot
last_reviewed: 2026-11-01
review_cadence: 只读历史快照
---

# Harness 阶段验收（3A–4D，已实现）

> 从 `docs/ACCEPTANCE.md` 移出的历史验收清单；当前状态以
> [../ACCEPTANCE.md](../ACCEPTANCE.md) 与 `acceptance-criteria.yaml` 为准。
## 5. Harness 阶段验收（3A–4D，已实现）

> 完整方案见 [HARNESS.md](../HARNESS.md)。5.1–5.3 是编排/写/干预，5.4–5.7 是
> 评测、实时事件、依赖语义与动态任务图。

- [x] `AGENT_ORCHESTRATOR_ENABLED=false` 时，事件、状态与行为与现有基线一致。
- [x] 开启后，能根据提示词与仓库画像在 `direct` / `parallel_readonly` 间选择工作流。
- [x] `parallel_readonly` 至少 2 个只读 worker 并发执行（事件与机制指标可见）。
- [x] worker 失败不影响同批其它 worker；依赖失败的任务标记 `blocked`。
- [x] `synthesize` 的 `worker_context` 进入主 agent 的 planner/act，但不写回主消息历史。
- [x] `WorkflowSelected` / `TaskGraphCreated` / `WorkerStarted` / `WorkerFinished` /
      `OrchestrationFinished` 事件与审计齐全。
- [x] `estimate_recursion_limit` 覆盖编排路径。
- [x] `ruff`、全量 unit、CI 子集、WSL 集成通过。

单任务烟雾：`long/discount_and_audit` 通过，工作流 `parallel_readonly`，
5 个任务 / 4 个并发 worker。单次运行不能代表稳定通过率，需 `--repeat 2` 量化。

### 5.1 Phase 3B（已实现）

- [x] 任务失败后按 `AGENT_ORCHESTRATOR_TASK_RETRIES` 重试；重试耗尽才标 `failed`。
- [x] 默认 `partial` 不满足依赖；依赖 failed/blocked/**partial** 的任务标 `blocked`。
      5.6 增加 `depends_mode=any`，允许 review/test 消费 partial 产出。
- [x] `review` worker 可被任务图使用，复用只读工具循环并返回审查发现。
- [x] fan-in 只取每个任务**最后一次**尝试，并报告多 worker 关注同一文件。
- [x] `WorkerFinished` / 审计 / eval 记录 `attempt` 与 `worker_retries`。
- [x] `ruff`、全量 unit、CI 子集、WSL 集成通过。

### 5.2 Phase 3C（已实现，默认关闭）

- [x] `implement` 任务必须开启写编排、会话 `--write`、干净的 git 工作区；
- [x] implement 必须给出 `write_scope`；不同 implement 的 scope 不重叠，
      且 implement 之间不能互相依赖；
- [x] implement worker 在独立 git worktree 中运行，工具只作用于该 worktree；
- [x] 补丁先在主工作区 `git apply --check`，通过后才 `git apply`；
      冲突时主工作区不被修改，任务标 `failed`；
- [x] worker 内部工具调用逐条写审计；eval 记录 `worker_tool_calls` / `worker_merged`；
- [x] 写模式任务图校验失败时退化为单个 implement 任务：优先顶层目录，扁平仓库
      退到顶层文件；两者都没有时显式报告降级，写路径不会静默消失；
- [x] 写范围互不重叠的两个 implement worker 可同批并发（`long/parallel_modules`：
      2 worker / 最大同批 2 / 2 个 patch 合并）；
- [x] worker 脏检查排除 `.agent/` 与 `__pycache__/`，不会把并发 worktree/patch
      误判成用户未提交改动（真实 WSL 回归测试）；
- [x] **survey 脏检查同样排除 `.agent/`/`__pycache__/`**，并要求工作区是
      git 仓库根；否则第二次运行会静默关闭写编排（P0 回归）；
- [x] **合并前为主工作区目标文件留快照**，`worker_results.snapshot_ids`
      可被 `agent undo` 使用（真实 WSL 集成测试）；
- [x] 超过 `orchestrator_max_tasks` 的任务图按校验失败整图降级，不再静默截断；
- [x] worktree/patch 文件名截断后附加哈希，避免长任务 id 共用路径；
- [x] `ruff`、全量 unit、CI 子集、WSL 集成通过（含真实 worktree/patch 集成测试）。

单任务烟雾：`long/discount_and_audit` 通过；workflow `orchestrated_implement`，
1 个 implement worker 完成 33 次工具调用，patch 成功合并，最终验证通过。

多写并发烟雾：`long/parallel_modules` 通过；2 个 implement worker 同批并发
（最大同批 2），各自 worktree 中完成独立文件，2 个 patch 顺序合并，主 agent
最终验证 7 passed。两个 worker 状态均为 `partial`（各自全量验证时对方功能尚未
合并），依赖它们的 test 任务按 Phase 3B 规则标 `blocked`。
单次运行不代表稳定通过率。

### 5.3 Phase 3D（已实现）

- [x] `SteeringController` 按 `thread_id` 隔离；runtime `steer()` 入队并写
      `kind=steering` 审计（含来源/动作）；指令与暂停标记绑定 `run_id`，
      新 run 只清理旧残留并落审计；
- [x] `/pause` 在四个节点边界消费，`interrupt()` + checkpoint 挂起；
      `/resume` 通过 `resume_steering()` 继续，恢复次数受 `AGENT_MAX_RESUMES` 限制；
      CLI `agent resume` 可从 checkpoint 恢复 steering/审批挂起；
- [x] `/cancel` 协作式取消：只读/写 worker 在轮次与工具执行前检查；
      implement worker 取消后不生成 patch；scheduler 合并前再检查一次；
- [x] 已合并的 implement 改动保留（明确策略），未合并 patch 丢弃；
- [x] `/add` 只追加只读任务（explore/test/review）；direct 模式追加计划步骤；
- [x] `/replan` 保留 success/partial 任务，只重建剩余任务；已合并写补丁后
      新任务只允许只读；
- [x] `SteeringApplied` / `WorkflowRevised` 事件；TUI 暂停状态、提示与
      暂停期间拒绝新任务；
- [x] `ruff`、CI 子集、全量 unit、WSL 集成、无头 TUI 通过。

### 5.4 Phase 4 评测与对比（首切片，已实现）

- [x] 评测补充 **worker token / total token / worker_seconds /
      worker_parallelism / 角色分布 / 合并尝试与冲突率**；
- [x] `tests/eval/compare.py`：通过率 Δ 与两臂 `spread` 比较，
      带内时明确拒绝下结论；逐任务翻转与 `noise_suspect` 一并报告；
- [x] `tests/eval/steering_bench.py`：pause/resume/cancel 延迟，
      `wait`（提交→边界）与 `apply`（边界→事件）分开；
- [x] default 套件 `--repeat 2` 实测（`tests/eval/phase4_baseline.json`，
      工作区未提交改动 → `comparable=false`）：**两轮均 29/29、spread 0**；
- [x] 无模型、无 WSL 的单测覆盖 compare / bench / 新机制字段。

实测补充：compare 对旧 `baseline.json`（12/29）报 Δ=+58.6pp、超出 6.9pp
噪声带，但右侧 `comparable=false` 且跨版本，不能归因于本次改动。常规短任务
套件已饱和，后续调优应改用 `long` 或新尺子。steering bench
（delay=50ms，repeat=3）实测 pause apply 中位 **9.4ms**、resume **20.2ms**、
cancel apply **8.6ms**。

### 5.5 B 批：实时事件与成本口径（已实现）

- [x] `WorkerStarted` 通过 LangGraph custom stream 在拿到并发槽位后**实时**发出，
      不再等整批 worker 结束回放；`attempt` 一并给出；
- [x] 只读 worker 完成即发 `WorkerFinished`；implement 在主工作区合并后发，
      `merged`/`merge_error` 准确；
- [x] runtime 同时消费 `custom` 与 `updates`，用 `worker_id:attempt` 去重，
      直接调用节点时由 updates 兜底；
- [x] `RunFinished` / `RunFailed` 增加 `worker_input_tokens` /
      `worker_output_tokens`，TUI 显示；主 agent token 口径不变；
- [x] eval 的 `max_worker_batch` 改为按活跃 worker 数统计，适配实时交错事件；
- [x] implement 的 git/worktree/快照/patch 与 scheduler 合并统一
      `asyncio.to_thread`，避免阻塞事件循环；
- [x] `ruff`、CI 子集、全量、WSL 集成、TUI 通过。

### 5.6 C 批：依赖语义与写编排烟雾（已实现）

- [x] `TaskSpec.depends_mode`：`full`（默认，必须 success）/ `any`
      （success 或 partial 即可开始）；
- [x] `ready_tasks` / `mark_blocked` 按任务各自的 mode 判定；
      failed/blocked/cancelled 任何模式都不满足；
- [x] implement 不允许 `any`（写任务不能建在未通过完整验证的依赖上），校验失败降级；
- [x] planner 提示要求 review/test 在只需产出时使用 `any`；
- [x] 真实 WSL 烟雾：带未跟踪 `.agent/` 的仓库 → survey 判定干净且为仓库根
      → 规则选择 `orchestrated_implement` → implement patch 合并并留下快照；
- [x] `ruff`、CI 子集、全量、WSL 集成通过。

**模型烟雾实测**（带 `.agent/leftover` 的临时仓库，写编排开启）：
workflow `orchestrated_implement`；planner 生成 2 个 implement + 1 test + 1 review；
两个 implement worker 均 `partial` 但 patch 都合并；test 与 review 均执行成功；
主 agent 最终 `pytest` **7 passed**。`depends_mode=any` 对 partial 依赖的
放行由 `test_partial_dependency_is_consumable_with_any_mode` 与
`test_dispatch_runs_an_any_mode_review_after_a_partial_dependency` 单测锁定。

### 5.7 D 批：worker 驱动的动态任务图（已实现）

- [x] explore/review 的结构化报告可带 `proposed_tasks`（每个 worker 最多 2 个）；
- [x] `scheduler` 在 worker 完成点消费提议，经 host 校验后加入
      pending：仅 explore/test/review、非空 goal、去重、依赖必须已存在、
      总任务数不超过 `orchestrator_max_tasks`、拒绝 write_scope 与环；
- [x] failed/cancelled worker 的提议不采纳；partial worker 的提议仍可采纳；
- [x] `TaskGraphExtended(added, rejected)` 事件 + `task_graph` 审计 + TUI 展示；
- [x] 递归上限同步增加 `extend_tasks` 超步；
- [x] 单测覆盖校验、节点、worker 报告映射、runtime 事件、TUI；
- [x] `ruff`、CI 子集、全量、WSL 集成通过。

### 5.8 桌面 Phase 3：工作区与项目 Python（已实现）

- [x] Windows 原生文件夹选择，自动转成 `/mnt/<drive>/...` 或 WSL 路径；
- [x] `/mnt` 工作区显示 9P 性能提示，可一键复制到 WSL 原生目录（拒绝覆盖）；
- [x] 设置页同时展示项目 Python / Agent Python / 包管理器 / 锁文件 / 测试命令；
- [x] 项目存在 `.venv/venv/env` 时，自动测试命令使用项目解释器；
- [x] 在 VS Code 中通过 `code --remote wsl+<distro>` 打开工作区；
- [x] 浏览器模式隐藏桌面专属按钮，`GET /api/project` 仍可用；
- [x] 探测只读：不创建 venv、不安装依赖、不执行项目脚本。

### 5.9 桌面 Phase 4：功能补齐（已实现）

- [x] 会话搜索、重命名、tombstone 删除与 Markdown 导出；
- [x] 只读文件树/文件查看器：路径越界、二进制、大小截断均有边界；
- [x] Git 只读面板：分支、ahead/behind、改动、最近提交、diff；
- [x] 快照逐文件“接受/拒绝”，拒绝复用 `/api/restore` 并留底；
- [x] 上传列表、配额、删除与拖拽；
- [x] 恢复中心识别待审批/暂停/运行中/异常中断；
- [x] Provider 档案 CRUD、环境基线合并与 `/models` 发现（多用户禁用写入）；
- [x] 设置页展示预算/上传上限，可开系统通知。

### 5.10 桌面 Phase 5：质量与可选能力（已实现）

- [x] 诊断包包含版本/配置/脱敏日志/WSL 概况，且不泄露密钥；
- [x] server 崩溃看门狗按退避重启并重载窗口，关窗后停止；
- [x] 清单式更新检查（semver 比较、http(s) 校验），不自动安装；
- [x] 快照 diff 折叠、文件树懒加载、Git diff 截断；
- [x] 本地 OpenAI 兼容端点预设；
- [x] 自动更新回滚、MCP/插件、多窗口/IDE 扩展列为后续可选。

### 5.11 Phase A：四层架构契约与基线（设计完成，行为未变）

- [x] A1 前端 / A2 控制面 / A3 编排面 / A4 执行面的职责与依赖方向写入
      [PHASE_A.md](phases/PHASE_A.md) 与 [ARCHITECTURE.md](../ARCHITECTURE.md) 第 12–15 节；
- [x] 三种部署基线：Windows 11（A2 原生 + MySQL + WSL A3/A4）、Linux（原生）、
      Standalone（CLI/TUI 直连 A3+A4，无 A2/MySQL）；
- [x] 数据归属矩阵：MySQL 只存业务数据；checkpoint/工作区/运行状态不落库；
      企业 provider Key 只存 AES-256-GCM 密文 + 后四位，主密钥与明文不落库；
- [x] A2/A3 控制 API、SSE（seq/重放/挂起）与审批字段冻结；
- [x] Agent 功能 parity 清单覆盖 direct/orchestrated、审批、checkpoint、
      steering、工具、快照、记忆、审计与沙箱；
- [x] 关键路由、事件、会话字段与工具清单由
      `tests/unit/test_layer_contracts.py` 守住；
- [x] Phase B 五项决策已确认：A2 各平台原生、SecretProvider 两档、
      MySQL 只做索引 + 共享事件传输、SQLite 一等公民、静态 SPA + esbuild/TS；
- [x] 现有行为未改变，Phase B 才引入控制面抽取与 MySQL schema。
      决策理由与代价见 [ADR-0010](../adr/0010-four-layer-phase-b-decisions.md)。

### 5.12 Phase B：控制面首切片（已实现，默认关闭）

- [x] A2 SQLAlchemy schema：sessions/runs/audit_index/provider_profiles/usage；
- [x] `AGENT_CONTROL_DB_URL` 可选启用；空值继续使用 `gui-sessions.json`；
- [x] 旧 JSON 一次性导入；DB 重启后恢复会话目录与 tombstone；
- [x] SecretProvider 两档接口（keyring / systemd-creds）；
- [x] 共享事件传输：内存默认、Redis Streams 可选；SSE 可按 `seq` 跨 A2 重启重放；
- [x] 新流起始序号接续 transport 的 `latest_seq`，审批/收尾事件终止重放；
- [x] 独立 Agent Server：内部 token + context 鉴权，run/resume/steer/
      approvals/status/history 生命周期 API；
- [x] A2 配置 `AGENT_INTERNAL_URL` 后通过 `HttpAgentClient` 代理运行生命周期；
- [x] B4.2：文件/Git/项目环境、快照/checkpoint/fork/rewind/compact、记忆、
      上传、审计、会话索引、恢复中心路由已迁移到 Agent Server；
- [x] A2 provider key 随运行短期注入 A3 内存；审计查询时幂等写入 MySQL 索引；
- [x] `scripts/build_services.py` 构建 `UBISmartAgent`/`UBISmartControl`；
      Linux systemd unit、Windows SYSTEM 开机任务与环境文件示例已提供；
- [x] 前端源迁到 `frontend/src/app.ts`，esbuild 生成 CSP 兼容的 `static/app.js`；
- [x] schema 在 SQLite 实际建表、MySQL DDL 编译通过；
- [x] 默认行为不变，Standalone 仍走本地文件/SQLite。

### 5.13 Phase C：ExecutionService 契约（C0/C1 已实现，C2/C3 路径迁移）

- [x] `ExecutionRequest` / `ExecutionResult` / `ExecutionBackend` 契约；
- [x] `op_id` 幂等、冲突检测、跨服务重启回执日志；
- [x] 执行 `seq` 按 run 单调递增，与 SSE `seq` 独立；
- [x] `SandboxExecutionBackend` 包装 WSL/Linux 沙箱，run/读/写/列目录/stat/
      git/snapshot/restore/worktree 可用；
- [x] `AgentRuntime` 用 `ExecutionSandbox` 代理既有 sandbox，工具执行统一到 L4；
- [x] 共享契约用例 + WSL 本地通过 + Linux CI `pytest -m execution`；
- [x] WSL shell/snapshot/git/verify 集成回归通过；
- [x] 工具调用级 op_id（run_id+tool_call_id+序号）重试命中回执；
- [x] `record_unknown/record_cancelled` 提供 unknown/cancelled 终态；
- [x] 合并 `git apply/--check/--numstat` 走 ExecutionService 并写回执；
- [ ] merge 回执尚未进入 Scheduler 事件；Linux CI 实跑与回执轮转待补。

### 5.14 Phase D：双平台部署与故障演练（已实现，桌面 WSL 启动链待打包验收）

- [x] A2/A3 版本握手：A3 `/healthz` 与 `/v1/meta`，协议版本不匹配在
      A2 `agent_state` 中可见；
- [x] 桌面启动器切到 Windows A2 + WSL A3：固定端口、内存 token 复用、
      双进程 watchdog、关闭顺序；
- [x] A2 每 5 秒探测 A3；A3 重启后自动从 `unavailable` 回到 `ok`；
- [x] 控制面数据库 Alembic baseline（旧库 stamp）、有界连接池、
      只读降级、备份/恢复脚本与 runbook；
- [x] Linux systemd 单元顺序/权限/`ExecStartPre --migrate-only`，
      以及 `install.sh`（打包/源码两种模式）；
- [x] WSL Ubuntu 24.04 + systemd 255 完整安装演练：启动、握手、迁移、
      会话持久化、A3 重启恢复、`--uninstall`；
- [x] `scripts/phase_d_smoke.py` 7/7：启动握手、会话 CRUD、A3 崩溃恢复、
      A2 重启持久化、数据库降级、版本不匹配、迁移失败；
- [x] CI 安装 `db` extras，并在快反馈后执行 `phase_d_smoke.py`；
- [ ] Windows 桌面完整 WSL 启动链尚未在打包发行版中验收
      （本机 WSL 未安装项目依赖，代码路径与单测已覆盖）；
- [ ] 真实 LLM run、工具、审批、快照回滚未纳入协议级 smoke；
- [x] Phase E 已补齐主题/字体/快捷键、SSE 重连、任务字段与推理分块，
      详见 [PHASE_E.md](phases/PHASE_E.md)；后续已补 JS 变量高度虚拟列表、
      流式 Markdown 增量渲染、jsdom 交互测试与 pywebview 探针。

### 5.15 Phase E：前端补齐（已实现；后续补 jsdom 交互测试）

- [x] 深/浅/跟随系统主题；字体字族、字号、密度持久化；
- [x] 快捷键 `Ctrl/Cmd+K`、`Ctrl/Cmd+Enter`、`Esc`、`?`；
- [x] 可访问性：`:focus-visible`、`aria-live`/`role`/`aria-label`、
      `prefers-reduced-motion`；
- [x] SSE 客户端独立模块；`seq` 去重、自动重连退避、连接状态、失败重试；
- [x] 审批/steering 恢复保留 `lastSeq`，不重放历史事件；
- [x] 任务卡显示依赖、worker、耗时、token、tool_calls、merge_error；
- [x] reasoning 按节点分块折叠；隐私开关默认关闭；
- [x] 代码高亮/复制、代码块折叠、diff 复制；
- [x] 长会话 `content-visibility: auto`；
- [x] 前端资产测试与 CI 构建漂移/类型检查门禁；
- [x] 变量高度虚拟列表、流式 Markdown 增量渲染与大工具输出分页；
- [x] 74 个纯函数 + 40 个 jsdom 交互用例；6 个 Playwright E2E；pywebview 严格 CSP 探针；
- [x] Playwright 真实浏览器 E2E（用户端 smoke + `/admin`）；手测清单见 PHASE_E.md / FRONTEND.md。

### 5.16 Phase F：安全与性能验收（已实现，平台门禁见下）

- [x] 企业 provider Key 只以 AES-256-GCM 密文 + 后四位落控制面数据库；
      主密钥与明文不落库；Provider API 与日志不含 key；
- [x] 主密钥轮换事务重加密并递增 `key_version`；备份目录扫描不含主密钥；
      `phase_secret_key_drill.py` 无 MySQL 演练通过；
- [x] 审计与错误脱敏覆盖 `sk-…`、私钥、JWT、DB URL；
- [x] 内部接口 token、前端认证、路径逃逸与 A2 不执行工具的既有用例；
- [x] SQLite 迁移/回滚/备份/恢复演练：upgrade → 备份 → downgrade base →
      upgrade → 恢复 → 数据一致；
- [x] MySQL 8.0.27 独立测试库完成同一演练；
- [x] 性能基线脚本：ExecutionService 代理、SQLite/MySQL 控制面写入、
      A2 代理 SSE 首事件延迟；
- [x] CI 执行 `phase_f_drill.py`（SQLite）；
- [ ] WSL/bwrap 真实隔离需要 `pytest -m wsl` / `pytest -m execution`；
- [ ] MySQL 最小权限账号与 `SHOW GRANTS` 需要人工/DBA 环境验证；
- [ ] 浏览器长会话渲染与真实工具 I/O 开销仍需人工或平台验收
      （jsdom 覆盖交互，Playwright 覆盖真实浏览器 smoke；平台门禁仍待补）；
- [ ] 提示注入（T2 主战场）仍按 THREAT_MODEL 的残余边界处理。

### 5.17 前端会话视图与实时分屏（已实现）

- [x] 会话运行态归属 `SessionView`，`sessionViews[threadId]` 复用与 LRU（6 个）；
- [x] 每条 SSE 绑定发起视图，切换会话后后台流继续；后台完成标记未读并通知；
- [x] 主区与分屏并行运行，分屏可独立发送、显示状态与审批提示；
- [x] 每视图 AbortController，删除/清空会话时中止；
- [x] 会话未读/摘要/批量操作、主题对比度/强调色/代码排版、图片预览；
- [x] Markdown prose/代码块标题、文件树类型图标与引导线、Toast 堆叠、可拖拽分屏；
- [x] jsdom 覆盖视图隔离、后台流、实时分屏、审批、拖拽、主题与视觉组件；
- [x] 分屏宽度可拖拽并持久化；活跃 pane 有强调边框；
- [x] 编辑器标签/文件视图按会话隔离；分屏内可直接批准、拒绝或全部拒绝。
- [ ] 键盘焦点仍是浏览器全局（不按会话隔离）；分屏审批出现时不自动抢焦点。

### 5.18 深层工作流与知识产物（RFC-0009，部分落地）

所有能力默认关闭；验收锚点为 REQ-230～REQ-236。

- [x] 长文档全文覆盖或显式 `partial` + 未读区间（REQ-230）；
- [x] 长文档分批 map-reduce，不整篇塞入单次调用（REQ-231）；
- [x] 报告写作上下文包含 claim + quote + anchor（REQ-232，partial：检索工具已提供，尚未强制按节检索）；
- [x] `ArchIndex`（含 import 边）、`ArchBrief`、`impact_analysis` 接入规划提示（REQ-233，partial）；
- [x] 跨模块变更使用接口优先波次与波次 gate（REQ-234）；
- [x] 知识产物持久化与 `knowledge_read/search` 工具（REQ-235，partial：词法检索，无向量）；
- [x] 动态图构建失败回退现有四模板（REQ-236）；
- [x] Python 索引增量更新，只重解析变化文件（REQ-237）；
- [x] 多语言/规模抽样索引，截断时影响分析 fallback（REQ-238）；
- [x] 文档答案确定性 + LLM judge，LLM 失败回退确定性（REQ-239）；
- [x] 跨语言调用图参与影响分析与契约（REQ-240）；
- [x] 内容寻址的跨机器索引缓存，换 clone/改名可复用（REQ-241）；
- [x] judge 阈值可配置并支持标注集校准（REQ-242）；
- [x] 动态/反射/宏调用被识别或标记 fallback（REQ-243）；
- [x] 共享缓存按 TTL/条目/字节自动淘汰（REQ-244）；
- [x] 种子标注集可导出并驱动校准（REQ-245）；
- [x] 宏定义/调用边与可选 LSP 动态解析（REQ-246）；
- [x] 共享缓存跨进程锁与 stale 接管（REQ-247）；
- [x] 大规模标注集构建/校验/切分，且可强制人工标注（REQ-248）；
- [x] 150 条待人工审核队列，promote 强制 label+reviewer（REQ-249）。

### 5.19 多文档结构化阅读（ADR-0038）

- [x] map-reduce 按章节标题切分，超长章节保留标题再分段（REQ-250）；
- [x] 证据 quote 在归一化文本中定位，记录 `quote_verified` /
      `quote_span`，非法/无法定位的锚点不进入下游（REQ-251）；
- [x] 每篇固定槽位证据矩阵（问题/方法/算法/实验/局限/创新/结论），
      空槽位用已验证 claim 回填（REQ-252）；
- [x] 多文档按 fanout 树状归并，单次输入有上限，最终答案附紧凑矩阵（REQ-253）；
- [x] 按句子检查“提到的文档”与引用路径，归属错配写入
      `attribution_issues`（REQ-254）；
- [x] `document → author → report_check` 报告评测，断言章节、逐篇引用、
      矩阵槽位、引用跨度与归属（REQ-255）。

矩阵默认关闭（`AGENT_DOCUMENT_MATRIX_ENABLED=false`）；评测/长文档场景显式开启。
10 篇/230 页实测见 [DEEP_WORKFLOW_EVAL.md](eval/DEEP_WORKFLOW_EVAL.md) §9。

设计与落地顺序见 [RFC-0009](../rfc/0009-deep-workflows-and-knowledge-artifacts.md)；
决策见 [ADR-0038](../adr/0038-structured-document-matrix-and-attribution.md)。
