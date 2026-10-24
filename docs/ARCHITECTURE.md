---
layer: L3（第 6 节不变量、第 7 节取舍属 L2 内核）
doc_id: architecture
title: 架构
owner: 项目作者
status: active
last_reviewed: 2026-10-24
review_cadence: 拓扑或状态字段变动时
note: |
  第 6 节的 56 条不变量与第 7 节的取舍是本项目最硬的 L2 资产。
  新决策请写入 docs/adr/（只增不改），不要改写这两节。
---

# 架构

面向实现：分层、数据流、不变量与取舍理由。Harness 阶段方案见
[HARNESS.md](HARNESS.md)，当前状态与已知限制见 [ACCEPTANCE.md](ACCEPTANCE.md)。

## 1. 一条原则

> **宿主内置工具，模型只输出意图。**

LLM 拿不到操作系统句柄，只能产出结构化调用 `{tool, args}`；宿主校验后在 WSL2 沙箱里执行。
文件修改走专用工具（精确替换 + diff + 写前备份），不经 shell。

由此推出四件事：

| 后果 | 实现 |
|---|---|
| 模型说什么不算数，宿主说了算 | 命令分级在 `sandbox/policy.py`，不看模型自报 |
| 危险操作过不去 | L2/L3 先挂起等审批；`approve` 只自动放行 L2，L3 始终确认 |
| 改动可回滚 | 写盘前快照，恢复前再留底 |
| 一切可追溯 | 审计由 runtime 写，前端绕不过 |

## 2. 分层与依赖方向

```
┌──────────────────────────────────────────────────────────┐
│ 前端  cli/   tui/   web/   desktop/                        │
│   只消费领域事件，**不得导入 tools/ 或 sandbox/**           │
└───────────────────────┬──────────────────────────────────┘
                        │ AsyncIterator[Event]
┌───────────────────────▼──────────────────────────────────┐
│ runtime.py   AgentRuntime                                 │
│   唯一编排入口：持有图、沙箱、安全策略、审计、快照存储        │
│   把 LangGraph 的原始流翻译成领域事件                       │
└───────────────────────┬──────────────────────────────────┘
                        │
┌───────────────────────▼──────────────────────────────────┐
│ graph/  state.py routing.py build.py nodes/               │
│   planner → act ⇄ (approval_gate → tools) → verify        │
│   → review → advance → steering_step → replan → …         │
│   编排路径：plan_tasks → steering_plan → scheduler        │
│             ⇄ steering_dispatch → fan-in              │
│   所有收尾路径经 steering_final → respond                  │
│   两道质量关：verify 管行为对不对，review 管代码干不干净      │
│   一步没有产生任何改动 → nudge 打回重做一次（上限一次）        │
│   步进后经 replan：计划可在执行中被修正（修的是"后面"）        │
│   节点只读状态、返回状态增量；不碰 I/O 以外的宿主资源         │
└───────────────────────┬──────────────────────────────────┘
                        │
┌───────────────────────▼──────────────────────────────────┐
│ agents/  explore.py（只读 Explore 子智能体）                │
│   独立 messages；只绑定 EXPLORE_TOOL_NAMES 白名单；         │
│   由 tools 节点在宿主侧执行，不产生第二条写路径；            │
│   同一消息的多个 delegate 由 Semaphore+gather 并发（fan-out）│
└───────────────────────┬──────────────────────────────────┘
                        │
┌───────────────────────▼──────────────────────────────────┐
│ tools/   shell files git deps search testrun              │
│   能力层：执行 + 路径守卫 + 产出结构化 artifact             │
└───────────────────────┬──────────────────────────────────┘
                        │
┌───────────────────────▼──────────────────────────────────┐
│ sandbox/ wsl_exec fs snapshots policy pathguard limits    │
│   WSL2 进程、路径校验、命令分级、资源上限                   │
└──────────────────────────────────────────────────────────┘
        events.py  config.py  messages.py  diffing.py  audit/  memory/
                          （横向：各层都可依赖）
```

**依赖方向是单向的**，`tests/unit/test_architecture.py` 用 AST 扫描守住最关键的一条：
`tui/`、`web/`、`desktop/` 若导入 `coding_agent.tools` 或 `coding_agent.sandbox` 直接失败。

为什么这条最重要：一旦前端能直接调工具，就会出现**绕过审批的第二条执行路径** ——
Web 页面上一个按钮就能跳过命令分级。这条约束是安全属性，不是代码风格。

> `cli/` 是唯一的例外：`doctor` 与 `sandbox-init` 需要直接探测沙箱。
> 它们是诊断命令，不执行模型意图。

## 3. 目录职责

| 路径 | 职责 | 不该做的事 |
|---|---|---|
| `runtime.py` | 编排、事件翻译、审计、生命周期 | 不含业务规则（分级在 policy） |
| `events.py` | 前端契约（领域事件） | 不含逻辑 |
| `graph/state.py` | 跨节点传递的状态 | 不放能推导出来的派生值 |
| `graph/nodes/` | 单个节点的纯函数 | 不直接 yield 事件（例外：`scheduler` 用 custom stream 发 worker 实时事件） |
| `graph/routing.py` | 条件边 | 不做副作用 |
| `agents/` | 子智能体循环（独立上下文） | 不直接写主 state、不产生写路径 |
| `tools/` | 能力：执行 + 回报 | 不做安全判定（判定在 approval_gate） |
| `sandbox/policy.py` | 命令分级、会话策略 | 不执行命令 |
| `sandbox/fs.py` | 沙箱内文件读写 + 路径双重校验 | 不做业务判断 |
| `audit/` | JSONL 审计 | 不解析工具文本输出 |
| `memory/` | checkpoint / 会话索引 / 长期记忆 | 不持有业务状态 |
| `desktop/` | 桌面壳：local FastAPI + pywebview + 随机 token | 不实现业务逻辑；不直接调用工具 |
| `access.py` | 会话访问模式（只读/审批写/自动写） | 不做执行判定（在 policy） |

## 4. 一次完整运行

```
前端                  Runtime                     Graph                  沙箱
 │                      │                          │
 │─ run(prompt, tid) ──▶│                          │
 │                      │ 组装 inputs（含 memories 快照）
 │                      │ 组装 config（trace metadata）
 │                      │ 审计 run_start
 │                      │─ astream(payload) ──────▶│
 │                      │                          │─ planner ─┐
 │                      │◀─ updates{planner} ──────│           │ 分解子任务
 │◀─ PlanCreated ───────│                          │           │
 │◀─ StepStarted ───────│                          │◀──────────┘
 │                      │                          │
 │                      │◀─ messages{tokens} ──────│─ act ─────┐ 调模型
 │◀─ AssistantToken ────│                          │           │
 │                      │◀─ updates{act} ──────────│           │
 │◀─ ToolCallStarted ───│                          │◀──────────┘
 │                      │                          │
 │                      │                          │─ approval_gate ─┐
 │◀─ ApprovalRequested ─│◀─ __interrupt__ ─────────│                 │ 需要人工？
 │                      │                          │◀────────────────┘
 │  （前端收集答复后 resume）
 │                      │                          │─ tools ───▶│─ 校验 + 执行 ─┐
 │                      │◀─ updates{tools} ────────│◀───────────│◀─────────────┘
 │◀─ ToolCallFinished ──│  写审计 tool_call        │
 │◀─ FileChanged ───────│  写审计 file_change       │
 │                      │                          │
 │                      │◀─ updates{verify} ───────│─ verify ──▶│ 跑测试/构建
 │◀─ Verification ──────│  写审计 verify            │◀───────────│
 │                      │                          │
 │                      │                          │ 通过 → review
 │◀─ ReviewFinished ────│◀─ updates{review} ───────│─ review ──▶│ 确定性静态审查
 │                      │  写审计 review            │◀───────────│ （+ 探测到的 linter）
 │                      │                          │
 │                      │                          │ 任一关没过且还有预算 → repair → act
 │◀─ RepairStarted ─────│◀─ updates{repair} ───────│
 │                      │  写审计 repair            │
 │                      │                          │
 │                      │                          │─ advance → replan / respond
 │◀─ StepStarted ───────│                          │
 │                      │                          │  改了剩余步骤 → 写审计 replan
 │◀─ PlanRevised ───────│◀─ updates{replan} ───────│
 │◀─ AssistantToken ────│  （respond 的 token）      │
 │◀─ RunFinished ───────│  写审计 run_end            │
```

要点：

- **节点不直接调用前端**。绝大多数事件由 runtime 从 `updates` 里翻译；
  `scheduler` 是唯一例外：它通过 LangGraph `get_stream_writer()` 推送 worker
  开始/结束的 custom 事件（实时进度），runtime 用 `worker_id:attempt` 去重后
  翻译成领域事件。
  这样节点的测试不需要关心事件，前端的测试不需要关心图。
- **审计与事件同源**。绝大多数事件来自同一份 `updates`；`scheduler` 的 worker
  事件来自 custom stream，runtime 同样在翻译时写审计，不存在“界面显示了但没记账”。
- **挂起时以 `ApprovalRequested` 收尾**，不发 `RunFinished` —— 前端据此判断该 resume 还是收工。
- **桌面/Web 默认启用缓冲 SSE**：`/api/run` 把运行放进后台 task，事件带递增
  `id` 写入内存环形缓冲；HTTP 连接只是订阅者，断开不会取消运行。
  `GET /api/stream/{thread}?after=<seq>` 或 `Last-Event-ID` 可重放。
  缓冲是单进程内存态，多实例部署仍需共享存储/粘性会话。

## 5. 图状态与生命周期

| 字段 | 含义 | 谁重置 |
|---|---|---|
| `messages` | 对话与工具轨迹 | `add_messages` 追加 |
| `cwd` | 沙箱内工作目录 | 每次 run 由 runtime 注入 |
| `memories` | 项目长期事实（按请求检索的 top-K） | 每次 run 从文件重读 |
| `project_instructions` | 分层 `AGENTS.md` / `CLAUDE.md` 合并文本 | 每次 run 加载 |
| `stale_paths` | 本轮已成功修改的文件 | runtime 每轮置空；tools 追加 |
| `context_trim` | 最近一次 act/respond 的裁剪报告 | 节点每次覆盖 |
| `plan` / `plan_kinds` / `step_idx` | 子任务、类型与进度 | planner 初始化，advance 递增，replan 改写剩余部分（文本与 kind 同步） |
| `replan_count` | 已重建计划的次数 | planner 清零、replan 递增；**整个任务**的额度，advance 不动 |
| `tool_rounds` | 当前步骤已用轮次 | planner / advance / repair 清零 |
| `budget_exhausted` | 本步因预算耗尽收尾 | 同上 |
| `dirty` | 本步是否改过东西 | 由 tools 置位，advance 清零，planner 在编排合并后置位，repair 置位 |
| `pending_verification` | 编排合并过补丁、主 agent 尚未验证 | scheduler 置位；verify 执行后清零；advance/repair/replan 不碰 |
| `verification` | 最近一次验证结果 | planner / advance 清空 |
| `review` | 最近一次代码审查结果 | 同 `verification`：planner / advance 清空，**repair 刻意不清**（act 要靠它知道该改什么） |
| `review_watermark` | 已审过的最大快照 id | **只由 planner 归零**；advance / repair 都不许碰（见下方不变量 19） |
| `retry` | 当前步骤的修复次数 | planner / advance 清零，repair 递增。**verify 与 review 共用这一份预算** |
| `same_failure_count` / `last_failure_signature` | 连续同一失败签名的次数与签名 | verify 写入；repair 与 replan 都刻意不清；只有 planner / advance（换步骤）归零 |
| `repo_profile` / `delegation_allowed` / `delegation_budget` | 仓库画像与本次运行的委派预算 | runtime 在 `run()` 时注入；planner / advance 不动 |
| `session_brief` | 从 checkpoint 抽取的有界会话约束摘要 | runtime 每次 `run()` 重新生成；不写回 `messages` |
| `transient_retries` | 节点内瞬时错误重试次数 | runtime 每轮归零；节点写入，runtime 累加后写进 `run_end` 审计 |
| `run_status` | `running` / `finished` | runtime 每轮置 `running`；`respond` 收尾置 `finished` |
| `recovery_notice` | 上一轮异常中断时给模型的恢复提示 | runtime 每次 `run()` 根据 prior checkpoint 生成 |
| `orchestration_handoff` | fan-in 产出的任务级结构化交接（含 artifact 路径） | fan-in 每次覆盖；runtime 每轮清空 |
| `approvals` | call_id → 审批结果 | approval_gate 写入，tools **消费后清空** |
| `steering_cancelled` / `steering_replan` / `steering_replans` | 用户干预结果与次数 | runtime 每轮归零；steering 节点写入；plan_tasks 消费 replan |
| `steering_applied` / `workflow_revised_reason` | runtime 翻译事件用 | steering / plan_tasks 每次写入 |
| `tasks` / `task_status` / `task_attempts` / `worker_results` | 编排任务图与每轮结果 | plan_tasks 初始化；scheduler 增量写入 |
| `task_extensions_seen` / `task_extensions_applied` | worker 提议只读任务的扫描位置与本轮新增 | extend_tasks 写入；runtime 每轮归零 |

`approvals` 的一次性语义是安全属性：不清空的话，下一轮的调用会复用上一轮的批准。
有测试守着（`test_approvals_do_not_leak_into_next_round`）。

steering 的**指令队列不在 state 里**：它是外部输入，worker 需要一边运行一边
非破坏性轮询 `cancel_requested`，而 state 更新与图执行是串行的。暂停状态由
checkpoint 保存；未消费指令与 pause 标记由 `SteeringController` 原子落盘到
`.agent/steering/`，进程重启后可通过 `resume` 恢复。

## 6. 不变量

这些是几周里踩出来并逐一固化成测试的。改动相关代码前请先读它们。

| # | 不变量 | 为什么 | 测试 |
|---|---|---|---|
| 1 | **act 只在还有预算时产出 `tool_calls`** | 路由因此可无条件信任"有 tool_calls 就送去执行"，不会留下没人应答的 tool_calls（那会让下一轮 API 直接 400） | `test_act_stops_without_tool_calls_when_budget_exhausted` |
| 2 | **审批结果一次性** | 否则跨轮次误放行 | `test_approvals_do_not_leak_into_next_round` |
| 3 | **上下文裁剪只截断内容，绝不丢弃消息** | 丢消息会破坏 `tool_calls` ↔ `tool_call_id` 配对 | `test_preserves_tool_call_pairing` |
| 4 | **工具产物是结构化 artifact，不解析文本** | 文本是给模型看的，格式随时会变 | `test_parse_artifact_dispatches_on_kind` |
| 5 | **前端不得导入 tools / sandbox** | 否则出现绕过审批的第二条执行路径 | `test_ui_does_not_import_capability_or_sandbox` |
| 6 | **fail closed** | 审批记录缺失 / 答复无法解析 / 非交互环境，一律拒绝 | `test_missing_approval_fails_closed` |
| 7 | **审计失败显式报出** | 审计有缺口是这个项目不能接受的失败模式 | `test_audit_write_failure_surfaces_as_run_failed` |
| 8 | **所有写操作先留底** | 回滚本身也要可回滚 | `test_restore_is_itself_undoable` |
| 9 | **路径校验做两次**（词法 + realpath） | 词法挡不住"先建符号链接再穿透" | `test_symlink_escape_is_rejected` |
| 10 | **审批结果跨挂起/恢复仍带调用参数** | 挂起与恢复是两次 `_stream`；配对表若只活在局部，恢复后审计会丢掉参数——偏偏是最高风险的 L2/L3 调用。进程重启后从 checkpoint 尾部的 tool_calls 恢复 | `test_approved_tool_call_keeps_args_across_resume`、`test_pending_call_args_survive_a_process_restart` |
| 11 | **悬空符号链接也按目标解析**（`-e` 或 `-L` 都触发） | 悬空链接的 `-e` 为假，只判存在性会顺着链接写到工作区外 | `test_dangling_symlink_escape_is_rejected` |
| 12 | **只读命令的参数越界升级到人工确认** | 命令名只读 ≠ 参数安全（`cat ~/.ssh/id_rsa`） | `test_external_access_is_escalated_to_confirmation` |
| 13 | **引号内的字面量不当命令** | 危险模式匹配全文会把 `grep "rm -rf"` 误判；但 `$()` / `sh -c` 里的内容仍要扫 | `test_quoted_literals_are_not_dangerous` |
| 14 | **读取的大小上限在读取之前生效** | 读完再拒等于先把整份文件物化进内存，上限形同虚设 | `test_fs_oversized_file_is_not_read_in_shell` |
| 15 | **改了就要验证**：`dirty` 覆盖 shell 变更 | 只按工具名判定会漏掉经 shell 的修改（`sed -i`），verify 被跳过 | `test_shell_mutation_marks_dirty` |
| 16 | **重规划只改写剩余步骤** | 已完成的是既成事实；删掉它们会让 `step_idx` 与计划的对应关系断掉，后续步进全错位 | `test_revision_replaces_only_the_remaining_steps` |
| 17 | **重规划有硬性次数上限，且到顶后不再问模型** | 边做边改没有收敛点；每次重规划都要多一次模型调用 | `test_cap_stops_asking_the_model` |
| 18 | **重规划失败沿用原计划** | 模型给不出结构化结果不能拖垮整张图 —— 计划不完美也比任务中断强 | `test_structured_failure_keeps_the_old_plan` |
| 19 | **审查水位线跨步骤保留，只由 planner 归零** | 留底记的是**写前内容**，所以改过的文件永远与自己的留底不同。水位线一旦被逐步清零，每一步都会重审前面所有步骤的改动，同一个早已处理过的问题反复告警到把修复预算耗光 | `test_watermark_stops_the_same_findings_from_repeating`、`test_advance_increments_step_and_resets_per_step_state` |
| 20 | **路由每一跳只看自己那道关** | `repair` 刻意不清 `review`，若 `route_after_verify` 也去看 review，"验证失败→修复→验证通过"这条路上陈旧的阻断会把刚通过的验证直接打回 repair，**审查再也不重跑** | `test_verify_does_not_reroute_on_a_stale_review_block`、`test_review_blocking_loop_converges_and_reviews_every_round` |
| 21 | **审查阻断与验证失败共用修复预算，额度耗尽一律去 replan** | 两个独立计数器会让 `estimate_recursion_limit` 多一层乘积，而它算歪过三次。共用的收敛性由 replan 的硬上限保证 | `test_review_blocking_loop_converges_and_reviews_every_round`、`test_review_blocking_loop_terminates_with_a_zero_budget` |
| 22 | **审查没跑起来 ≠ 审查通过** | 与审计、验证同源的一条：脚本崩了要留一条可见的告警，而不是静默当成"审过了没问题" | `test_a_crashed_review_is_not_reported_as_clean` |
| 23 | **「这一步只读了文件」不等于「这一步做完了」** | `verify` 与 `review` 都只在 `dirty` 时执行，所以一个只读步骤会**一路短路到 advance** —— 实测里模型因此把整个计划"读"完就算完成（长程任务 10 次运行里 5 次一次写都没尝试过）。改为一律经 `nudge` 打回重做一次，并在提示里说明原因 | `test_a_step_with_no_change_is_sent_back_to_act`、`test_a_step_that_changes_nothing_is_retried_once_then_accepted` |
| 24 | **空步骤的重做有界，且必须给退路** | 只读步骤是合法的（计划第一步常常就是探索）。无限要求"必须有改动"会逼出无意义的改动，所以只给一次机会，且提示里明确写了"这一步确实只需只读就说明理由停下" | `test_the_nudge_happens_only_once_per_step`、`test_nudge_tells_the_model_why_it_is_back` |
| 25 | **「没验证」不等于「通过」** |  识别不出测试命令时原先是 `not_configured` → 路由判为非 failed → **审计记成 `ok=True`**，于是"没做"与"做对了"在账上分不开（P0-2）。现在：① 有源码就做**语法解析级降级验证**（`kind=syntax`），② 事件与审计的 `ok` 都是**三态**（True/False/**None=没验证**） | `test_a_python_project_without_tests_falls_back_to_a_syntax_check`、`test_the_audit_records_not_configured_as_unverified_not_passed`、`test_verification_ok_is_three_state` |
| 26 | **语法错在落盘前就拒** | 原先「先写下去 → review 事后查 → 不合格再 repair」，白烧一轮工具预算与一次模型往返。语法错是这里最便宜就能确定的错误（纯解析、零 I/O），没理由等到事后。**判据是「有没有变坏」而不是「是不是好的」** —— 原本就坏的文件允许继续改，否则会堵死模型自己的分步修复 | `test_write_rejects_broken_python`、`test_edit_rejects_a_change_that_breaks_syntax`、`test_an_already_broken_file_can_still_be_edited` |
| 27 | **`delegate_explore` 子智能体只能绑定只读白名单工具；implement worker 只能绑定文件/检索白名单** | 子智能体输入包含仓库内容，不能让它获得写/执行能力。构造时校验工具集合，运行时再按名字二次校验；委托调用本身仍走 `approval_gate → tools` 唯一咽喉。编排 implement worker 是另一条受控路径，构造时断言不含 shell/git/deps/test（见 32） | `test_runner_refuses_non_readonly_tools_at_construction`、`test_non_whitelisted_tool_call_is_refused` |
| 28 | **主 agent 的并发只发生在只读子智能体之间；编排并发由 scheduler 统一管理** | 多个 delegate 同批并发；主 agent 的普通工具（含写）仍串行。编排路径由 scheduler 的 ready 堆与信号量控制并发，任务完成即解锁依赖，失败立即短路。写操作与探索混在同一条消息时，提示词要求模型拆开，避免读到改动前的状态 | `test_multiple_delegate_calls_run_concurrently`、`test_concurrent_batch_respects_the_task_budget`、`test_dependent_starts_as_soon_as_its_predecessor_finishes` |
| 29 | **`implement` 步骤必须有实际改动才能推进** | 委派探索可以把「读」做完，但不能因此把实现步当成完成。`plan_kinds` 与 `plan` 逐项对齐：explore/verify 允许零改动直接步进；implement 先 nudge 一次，仍无 `dirty` 就收尾，不再静默 advance | `test_an_implement_step_nudged_without_changes_responds`、`test_an_explore_step_without_changes_advances_without_nudge` |
| 30 | **同一失败签名连续出现就改走 replan，且跨 replan 继续累积** | 24 次相同失败验证说明 repair 只是重复同一类修改。失败签名由失败测试 id + 归一化错误消息组成；连续达到 `max_same_failures` 后不再 repair。replan 会清空 `verification`，所以签名另存 `last_failure_signature`：新做法若仍撞同一个失败，计数继续增长，不会「清零→再修两轮→再 replan」循环。repair 时只重跑上次失败的 pytest 测试，子集通过后自动补跑全量。 | `test_the_same_failure_signature_goes_to_replan`、`test_stagnation_survives_a_replan_that_clears_the_verification`、`test_selected_pass_is_confirmed_by_a_full_run` |
| 31 | **委派只在 explore 步、且受仓库规模预算约束** | 38 文件的任务实测会拆 3–5 个子智能体，成本 +43%。默认：<30 文件不委派；30–99 最多 2 次；100–299 最多 4 次；≥300 用配置上限。实现/验证步一律拒绝委派 | `test_small_repo_disables_delegation`、`test_delegate_is_rejected_outside_an_explore_step`、`test_delegate_budget_comes_from_the_cost_policy` |
| 32 | **编排写 worker 只能在隔离 worktree 中并发，且写范围在 I/O 前就被校验** | 多个 implement 同时改主工作区会互相覆盖。每个 worker 一个 worktree；scheduler 串行执行 `git apply --check` → 留快照 → `git apply`；文件工具在写入前拒绝 `write_scope` 外路径，事后 diff 校验保留为第二道防线 | `test_concurrent_implement_workers_ignore_agent_state`、`test_merged_patch_takes_snapshots_so_undo_can_restore`、`test_dispatch_marks_a_conflicting_patch_failed`、`test_write_outside_scope_is_rejected_before_io` |
| 33 | **执行环境异常不等于代码失败** | WSL 掉线/命令无法启动时，`verify` 记 `error` 并直接收尾，`review` 记 `warned` 且不推进水位线；把这类错误送进 repair 只会让模型徒劳地改代码，或让整轮无谓失败 | `test_environment_failure_is_reported_not_fatal`、`test_an_environment_failure_is_warned_and_does_not_advance_watermark`、`test_verification_environment_error_goes_straight_to_respond` |
| 34 | **崩溃后必须修复悬空的 tool_calls** | 崩溃可能发生在 act 已提交、tools 未提交之间；同一 thread 重跑时若把新消息追加在未应答的 tool_calls 后，下一轮 API 会因配对不完整直接 400。runtime 为缺失的调用补占位 `ToolMessage`，并明确标注“结果未知” | `test_recover_adds_a_placeholder_for_each_unanswered_call`、`test_dangling_tool_calls_are_repaired_before_the_new_prompt` |
| 35 | **只重试瞬时/环境错误，且总等待有上限** | 对语义错误重试同一请求没有意义，对致命配置错误重试只是烧钱；无限退避会让一次运行永远挂住。`recovery.classify_error` 把异常压成四类，`retry_async` 只对前两类做指数退避 + 抖动 | `test_retry_async_does_not_retry_a_fatal_error`、`test_retry_async_stops_at_the_total_wait_budget`、`test_act_retries_a_transient_llm_error` |
| 36 | **写文件必须原子替换，并对瞬时占用做短重试** | 直接 `> file` 在进程被杀/磁盘满时会留下半截文件；`EBUSY/ETXTBSY` 往往只是编辑器/构建工具短暂持有文件，立刻报错会让模型做无意义的“修代码”。同目录临时文件 + `rename` 保证读者看到完整内容 | `test_write_uses_a_temp_file_and_atomic_replace`、`test_write_retries_on_busy_file`、`test_write_does_not_retry_a_semantic_failure` |
| 37 | **模型读到写之间文件被外部改过时不覆盖** | 否则 `file_write` 会静默丢掉编辑器/另一个进程的改动。只拿模型自己的读取缓存做乐观比较；没有读取记录不猜 | `test_changed_since_read_detects_external_modification`、`test_overwrite_is_rejected_when_file_changed_since_read` |
| 38 | **checkpoint 要能承受并发，且中断必须可识别** | 多前端共享 SQLite 时，默认 journal 模式容易 `database is locked`；WAL + busy_timeout 降低冲突。`run_status` 留在 checkpoint 里，下一次同 thread 运行才能判断上一轮是否异常中断，并把“可能有未验证改动”交给模型 | `test_sqlite_uses_wal_and_busy_timeout`、`test_interrupted_run_produces_a_recovery_notice`、`test_finished_run_does_not_produce_a_recovery_notice` |
| 39 | **有副作用的工具必须可检测“结果未知”，不能盲目重放** | 工具已执行、checkpoint 未记录时重放会造成第二次副作用（重复 commit / shell 写入）。日志按 `(thread_id, call_id)` 记录 started/finished；只有 started 时明确回灌“结果未知”并强制验证。事件与审计用 `interrupted` / `ok=None` 保留三态，不能退化成成功 | `test_a_side_effect_with_only_started_is_not_replayed`、`test_a_finished_side_effect_is_replayed_without_executing`、`test_a_side_effect_execution_is_recorded`、`test_interrupted_tool_audit_is_recorded_as_unknown` |
| 40 | **运行与验证各有独立时限** | `recursion_limit` 只管步数，管不住模型长时间卡住；验证命令通常比普通 shell 长，不能共用 60s。总时限跨 resume 累计，验证超时单独配置 | `test_run_deadline_stops_a_stuck_graph`、`test_verification_uses_the_dedicated_timeout` |
| 41 | **WSL 传输故障要熔断，但不能把命令失败算进去** | WSL 掉线时每个工具调用都拉起一个注定失败的 `wsl.exe`，会把一次运行拖成几分钟；连续传输失败后应 fail fast。命令退出码非零只说明用户命令失败，必须重置/保持闭合。冷却后只放一个半开探测，避免恢复瞬间又打满 | `test_transport_failures_open_the_breaker`、`test_normal_command_failures_do_not_open_the_breaker`、`test_half_open_probe_closes_the_breaker` |
| 42 | **worker / 工具 / WSL / LLM 并发由同一套资源池治理** | 各层独立限流会让高层在低层饱和时继续准入，优先级也传不下去。统一池让 scheduler、tools、sandbox、router 共享上限，获取槽位必须有超时，避免线程池被拖死 | `test_async_pool_limits_and_times_out`、`test_governor_limits_concurrent_llm_calls`、`test_sync_pool_times_out` |
| 43 | **调度要工作守恒：边界只管返回时机，预算不足不能全阻塞** | `checkpoint_every` 同时当启动上限会让 6 任务图先跑 4 个再空等；固定角色 token 预留会让大任务把小任务一起拖死。边界只按完成数返回；预算用 EWMA + next-fit 选择可容纳任务 | `test_checkpoint_boundary_does_not_cap_launches`、`test_observed_usage_lets_a_small_task_fit_where_static_reserve_would_not` |
| 44 | **低优先级任务要被 aging，探针要有保留槽** | 固定角色优先级会让 explore 在 implement 潮水下永久等待；WSL 长命令会把健康探针一起堵死。ready 队列按等待时间提升优先级；WSL 池保留 1 个槽只给 `reserved=True` 的探针 | `test_aging_raises_priority_but_does_not_beat_implement`、`test_reserved_slot_stays_available_for_probes` |
| 45 | **写批次要有合并背压，且只等待真正在跑的 implement** | 无上限地启动写 worker 会让补丁/快照堆积。在飞 + 待合并超过阈值时暂停新 implement；`has_active_implement` 只认 `in_flight`，否则待启动任务会让合并队列永远不 flush，收尾时还会整批丢弃 | `test_merge_backpressure_limits_concurrent_implement`、`test_merge_queue_only_waits_for_in_flight_implement_workers` |
| 46 | **同一工作区只能有一个运行中的 runtime** | 两个进程会同时拉起 WSL、同时改文件/合并补丁，快照与 review 水位线互相污染。用审计目录下的工作区哈希锁做进程内 + OS 级互斥；OS 锁返回“忙”时 fail closed，只有文件系统明确不支持锁才降级 | `test_workspace_lock_blocks_a_second_runtime`、`test_same_workspace_is_exclusive`、`test_lock_is_exclusive_across_processes` |
| 47 | **LLM 自适应并发要跨进程收敛** | 同一台机器上多个 runtime 各调各的，会在 429 恢复时一起打满。AIMD 结果写入现有共享限流文件，其他进程在下一次调用前同步并 clamp 本地池 | `test_concurrency_limit_is_shared_across_processes`、`test_aimd_shrinks_on_429_and_recovers_on_success` |
| 48 | **WSL 弹性并发只在饱和时依据 p95 延迟调整** | 非饱和时命令本身就慢（构建/测试），据此降并发会把正常负载误判成争用。只有 `in_flight >= limit` 时才按 p95 高/低于目标调整，且不抢占在飞命令 | `test_saturation_with_high_latency_lowers_the_limit`、`test_low_latency_recovers_toward_the_ceiling` |
| 49 | **常驻 shell 默认开启，但任何会话异常都必须退回一次性执行** | 一次性 `wsl.exe` 每次约 0.2s，是工具延迟的主要来源；常驻会话把读/检索降到约 20ms。会话破裂/超时/未返回结束标记时必须禁用它并回退，不能把稳定性押在一个长连接上 | `test_persistent_shell_reuses_one_process`、`test_persistent_shell_timeout_does_not_poison_the_next_command`、`test_non_wsl_test_cannot_execute_real_sandbox_command` |
| 50 | **文件写/编辑的读与写各只付一次沙箱往返** | 原先 stat + 读 + 写留底 + 写目标要 4 次进程启动。`read_for_update` 一次拿状态与内容，`write_with_snapshot` 一次写留底与目标；写盘仍是 temp+rename | `test_write_uses_a_temp_file_and_atomic_replace`、`test_overwrite_backs_up_previous_content` |
| 51 | **移动/复制/删除/建目录必须走受控文件工具，而不是 shell** | shell 的 `mv`/`rm` 是 L2 且不留快照、不过 review，直接破坏“可回滚”。文件工具版本带 write_scope、留底、diff 与审计；删除需要确认 | `test_move_renames_and_keeps_a_source_snapshot`、`test_delete_can_be_restored_from_the_snapshot`、`test_new_file_tool_levels` |
| 52 | **读缓存必须有字节上限，且只缓存模型可能复用的内容** | 长会话/大仓库里普通 dict 会无限增长。`ReadCache` 按字节做 LRU，超大单值直接不缓存；统计命中率供 run 指标 | `test_read_cache_evicts_least_recently_used_by_bytes`、`test_read_cache_skips_values_larger_than_the_limit` |
| 53 | **批量编辑必须“全量校验后一次落盘”** | 逐条编辑中途失败会留下半批修改，模型难以恢复。`file_edit_many` 先在内存中应用全部编辑、统一语法/陈旧/scope 校验，再用一次 `write_many` 落盘；任一失败整批不写 | `test_edit_many_applies_to_multiple_files_in_one_call`、`test_edit_many_rejects_the_whole_batch_on_a_bad_match` |
| 54 | **每个工具调用都要有统一耗时；重复探测要有 TTL** | 只靠 shell 的 duration 无法定位文件/检索工具慢在哪；AST 索引与包管理器探测每次重跑是纯浪费。tools 节点统一测 `duration_ms`，索引/探测按 TTL 缓存 | `test_selected_command_appends_a_safe_pytest_nodeid`、`test_read_cache_clear_and_stats` |
| 55 | **等待合并的 implement 必须离开 pending** | 一个 implement 完成、另一个还在跑时，前者会停在 merge 队列。若状态仍是 pending，`refresh_ready` 会把它重新排进 ready，导致同一任务被重复启动、同一补丁被重复加入批次，合并时第二次 apply 必然失败并触发回滚；按分量隔离后，同文件分量仍会一起回滚（当时实测 4 个 worker 全部失败） | `test_completed_implement_is_not_relaunched_while_waiting_for_merge` |
| 56 | **worktree 内的快照目录被 gitignore 时，`git add` 的 ignored-path 警告不是失败** | 快照写在 worktree 的 `.agent/`，它通常被 `.gitignore` 忽略；`git add -A` 会以 exit=1 报警告，但真实改动已暂存。直接返回“无改动”会让所有 implement worker 失败 | `test_implement_worker_creates_a_clean_patch`（setup 含 `.gitignore`） |

## 7. 关键取舍

### 为什么不直接用 `ToolNode`

`response_format="content_and_artifact"` 在 `tool.invoke()` 下**会丢弃 artifact**
（实测；该语义只在 `ToolNode` 内生效，而 `ToolNode` 在 langgraph 1.2.x 下无法脱离图调用）。
这条契约是事件层与审计层的地基，不适合押在语义不明的框架行为上，
改成工具返回 `artifacts.pack(text, artifact)` 自描述封装，由宿主显式拆开。

### 为什么审批在节点，不在工具里

审批结果是**逐次调用**的图状态，而工具是无状态的。把结果塞进模型可见的参数会被模型伪造；
塞进隐藏参数又要跟框架的 schema 校验搏斗。所以：

- `approval_gate` **只做决定**（放行 / 询问 / 拒绝），保持纯函数
  （`interrupt()` 恢复时节点会从头重跑）
- `tools` 节点**负责强制** —— 它是所有工具执行的唯一咽喉，没拿到 `auto`/`approved` 的调用一律不执行

### 为什么自动 review 也不走审批

与 verify 同一条理由，再加一条：审查**只读**工作区、不执行工作区里的代码。
它跑的是宿主写死的检查脚本（`tools/review.py`）加探测到的 linter，命令文本
完全由宿主决定 —— 与自动 verify 受同一条约束（不读模型可写的 manifest）。

### 为什么 review 的「改动前」用快照，不另建基线

文件工具每次落盘前都留了底（不变量 8），所以「这次改了什么」本来就有权威记录，
不需要再加一套影子存储。代价说清楚：**经 shell 改的文件（`sed -i`）没有留底，
会完全逃过审查** —— 这条质量关不是安全边界，「禁止用 shell 改源码」只是系统
提示里的软约束。

### 为什么 nudge 后的实现步强制一次工具动作

实测 deepseek-chat 在 implement 步偶尔会连续返回「无 tool_calls」的空动作：
文件已经读过了，但就是不落笔。`act` 在被 nudge 之后的**第一次**调用上使用
`tool_choice="required"`，至少强制出一个动作（通常是先读目标文件）；后续调用
恢复自动选择，留出编辑机会。provider 不支持强制 tool_choice 时回退到普通调用。

### 为什么多步任务只在最后验证

全量测试覆盖的是**整个功能**。计划第 2 步只做了模型/服务层时，报表与审批的
测试必然还是红的 —— 每步都跑全量，agent 会卡在第 2 步反复 repair，永远到不了
后面的实现步骤（实测 `long/discount_pipeline` 就是卡在这里）。

所以多步计划里，`kind=implement` 的非最终步骤不跑全量；只在 `kind=verify`
或最后一步验证。单步任务（也是最后一步）照常验证。
**`kind=verify` 的步骤即使 `dirty=False` 也会跑** —— 它的职责就是全量确认，
不应因为「这一步没有新改动」被短路。
`AGENT_VERIFY_ONLY_FINAL=false` 可以恢复旧行为。

### 为什么自动 verify 不走审批

区别在**谁决定执行什么**：verify 只能跑**命令文本由宿主写死**的命令
（`cargo test` / `go test` / `pytest`），模型影响不了跑什么；
模型主动调 `run_tests` 才走 L2 审批。

**这条线划过一次位置。** 早先的写法是「跑的是宿主从项目清单推导出的固定命令」——
但 `make test` 跑什么写在 Makefile 里、`npm test` 写在 package.json 里，而这两个
文件模型用 `file_write` 就写得到（`--write` 下是自动放行的 L1）。于是
「写个恶意 Makefile → 下一次脏写自动执行」就成了一条**不过审批的执行路径**，
把命令分级整个绕开。现在自动 verify 传 `allow_manifest=False`，manifest 派生的
命令只留给走审批的 `run_tests`（见 `tools/testrun.py::_detect`）。

### 为什么结构化工具而不是让模型拼 shell

Git、依赖、检索都有专用工具。换来三件事：参数无法逃逸（逐参数 `shlex.quote`、
包名白名单正则）、等级可判定（不再对命令字符串猜正则）、审计可读。

### 为什么 `.env` 优先于环境变量

通用约定是反过来的，这里刻意偏离。原因是踩过真实的坑：某些工具链会在启动时
往进程环境注入值，用户既看不到来源也删不掉，导致改 `.env` 完全不生效。
项目目录里的 `.env` 是显式写下、看得见、可编辑的意图。详见 [USAGE.md](USAGE.md#配置来源)。

## 8. Orchestrator（独立子图 + 事件驱动 DAG）

> 完整方案与阶段拆解见 [HARNESS.md](HARNESS.md)。

编排层是父图中的一个**独立子图**，默认启用；小任务仍由 runtime 选择
`direct` 快速路径：

```
START ─ route_entry ─┬─ direct → planner → act ...
                     └─ orchestrated
                          └─ orchestrator 子图
                               survey → select_and_plan → scheduler ⇄ steering_dispatch
                                                              ↓
                                                            fan_in → planner
```

- `orchestrator/state.py`：`OrchestratorState` 窄契约，只传 request/cwd/
  repo_profile 与编排结果，不复制父子图的 messages；
- `orchestrator/subgraph.py`：独立子图组装；父图用 `subgraphs=True` 消费内层
  更新与 worker custom 事件；
- `orchestrator/scheduler.py`：事件驱动 DAG（ready 堆 +
  `asyncio.wait(FIRST_COMPLETED)`），完成即解锁依赖、失败立即短路；
  `checkpoint_every` 到达边界时 drain 在飞任务再返回，交给
  `steering_dispatch` 消费 pause/add/replan/cancel；
- `orchestrator/merge.py`：implement 补丁串行合并（check → 解析路径 → 留快照 →
  apply），统一由 `merge.py` 实现；
- `orchestrator/artifacts.py`：fan-in 完整结果落 `.agent/artifacts/`；
  `worker_brief` 给 planner/act，`orchestration_handoff` 保留结构化字段与
  artifact 路径，主 agent 可按需回读；
- `orchestrator/scheduling.py` / `merge_queue.py`：限流退避、token 预算与
  补丁合并队列从 scheduler 主循环中拆出，便于单测；
- `concurrency.py`：worker / 工具 / WSL / LLM 共享的资源池；LLM 按
  provider + 全局双层限流，并根据成功/429 做 AIMD；
- `orchestrator/workflow.py`：规则优先；规则不确定时一次结构化调用同时返回
  workflow + 任务图，避免第二次规划往返。

**写路径**：`implement` worker 仍在独立 git worktree 中修改，导出 patch 后由
宿主串行合并。合并成功后 scheduler 会置 `pending_verification`，保证主 agent
至少对合并结果跑一次自动验证（否则主 agent 只读时 `dirty=False` 会让 verify 短路）。
`write_scope` 现在有**写时校验**：文件工具在 I/O 前拒绝越界路径；事后 diff 校验
保留为第二道防线。worker 工具白名单**不含 `file_delete`**：删除在主 agent 路径
是 L2 审批，不能从不走 approval_gate 的并发 worker 变成无审批通道。写编排要求
`--write`、git 仓库根、干净工作区；`.agent/` 与 `__pycache__/` 不算用户脏改动。

**Steering**：`runtime.steer()` → `SteeringController`（按 thread 隔离）。
`cancel` 在 worker 轮次/工具边界协作式轮询；`pause/add/replan` 只在 scheduler
drain 后的边界消费。pause 用 `interrupt()` + checkpoint，恢复时已完成任务
不会重跑。

**权限与稳定性**：worker 仍只能通过 `WslSandbox` 执行；implement worker 只绑定
文件/检索白名单，不绑定 shell/git/deps/test。`WslSandbox` 增加并发命令信号量与
探测缓存锁；所有 worker 工具调用、合并、超时、scope 违规、取消均写审计。

## 9. 并发与进程模型

### 单 runtime

- **一个事件循环**驱动图、scheduler 与 async 池；同步工具/沙箱经
  `asyncio.to_thread` 下放。
- `run` / `resume` / `resume_steering` 共用 `_stream_active` 闸门：同一
  runtime 不允许并发驱动同一张图（工作区锁是可重入的，不能替代这一层）。
- `AsyncLeasePool` 只在事件循环内变更，临界区不含 `await`；WSL 的
  `SyncLeasePool` 用 `Condition` 做线程侧互斥。
- `ReadCache` 用 `RLock` 保护：同一条消息里的多个只读工具会并发共享它。
- `WslSandbox` 的常驻会话池创建/关闭有锁，池同时跟踪 idle 与 active
  会话；`close()` 能清理正在执行的会话。延迟统计也串行化。

### 多 runtime（Web）

- `AgentRuntime(governor=...)` 允许注入共享 `ConcurrencyGovernor`；
  `web/app.py` 为一个进程创建一份并传给所有会话。这样 WSL / worker /
  tool / LLM 的上限是**进程级**的，不会随会话数相乘。
- 每个 runtime 仍有独立的 `WorkspaceLock`、checkpointer 连接与 steering
  状态；工作区锁保证同一工作区只有一个运行中的 runtime。

### 跨进程

- 工作区锁：占用时 fail closed，只有文件系统明确不支持锁才降级。
- 限流状态、审计 JSONL、steering 队列、工具执行日志统一使用
  `filelock.FileLock`（线程 RLock + OS 文件锁；OS 锁不可用时退化为线程锁）。
- 需要「读-改-写」的 steering 队列在持有文件锁后**强制重读**，避免两个
  `agent steer` 进程互相覆盖。
- SQLite checkpoint 使用 WAL + `busy_timeout`；多个进程共享同一文件时
  写冲突会等待而不是立刻失败。
- 已知边界：资源上限是**单进程**的；多个 `agent` 进程各自有独立池。
  跨进程只共享 LLM 冷却/AIMD 与工作区互斥，不共享 WSL 并发额度。
  **运维建议**：同时开多个终端会话时，按总预算调低
  `AGENT_CONCURRENCY_WSL` 与 `AGENT_PERSISTENT_SHELL_POOL`；需要统一配额时
  应让多个会话共用一个 Web 进程（共享 governor），而不是启动多个独立进程。
- `FileLock` 在 OS 锁不可用时退化为线程锁；`agent doctor` 会探测并给出
  WARN。审计记录做了长度截断，`read_records` 会跳过损坏行，因此降级不会
  破坏安全判定，只是跨进程协调变弱。

### 输出与状态边界

- 用户命令/验证命令的 stdout/stderr 在**捕获阶段**就受
  `AGENT_MAX_CAPTURE_BYTES` 限制：一次性执行用有界读取，常驻会话在沙箱内
  只回传 head+tail。文件工具等内部脚本通过 stdout 传 base64/JSON 协议，
  **不套用该上限**（由调用方显式传入），否则会破坏协议。
- scheduler 的自适应并发/退避状态写入 `OrchestratorState`，跨
  checkpoint 边界保留（墙钟时间戳，进程重启后仍可用）。

## 10. 多用户、公网与不可信仓库

### 认证与租户

- `web/auth.py`：Bearer Token → 用户名；支持 `sha256:<hex>` token；未配置
  token 时只允许 loopback 匿名。
- `tenancy.py`：每个用户独立 workspace（WSL）、audit/checkpoint/steering
  （宿主）、memory（WSL 且仓库外）。用户只能得到
  `workspaces_root/<user>`，Web 不接受任意主机路径。
- 内部 thread id 在多用户模式加 `user:` 前缀；runtime registry 按
  `(user, thread)` 隔离，杜绝跨用户读取/恢复。
- 单 Web 进程：共享 governor；CLI 启动时用工作区锁占住数据根，避免多进程
  绕过进程级配额。

### 安全 profile

- `AGENT_SECURITY_PROFILE=untrusted` 强制：
  - `AGENT_SHELL_SANDBOX=bwrap`（不可用则拒绝执行）；
  - `AGENT_SHELL_NETWORK=false`；
  - `approval_mode=ask`，并清空 `allow_rules`；
  - 不注入仓库 `AGENTS.md`/`CLAUDE.md`；
  - 不读取仓库 `.agent/memory.md`，记忆移到 `AGENT_MEMORY_ROOT`。
- `security/read_policy.py`：默认拒绝 `.env`、私钥/证书、`.npmrc`、`.netrc`、
  `.aws/**`、`.kube/**`、`credentials*.json`、`*.tfstate` 等；文件工具在读
  之前拒绝，shell 显式引用/通配形状/凭据关键词升级为 L2，search 工具过滤命中。
- `security/secrets.py`：审计、shell、search、file_read 回灌前统一脱敏。
- WSL 命令以 `env -i` 启动，宿主环境不继承；`printenv`/`env` 为 L3。
- `production/ops` profile 强制只读 + 禁网 + `deny`，运行时忽略 `--write`。

### 审计与日志

- 每条记录带 `prev_hash`/`hash`，链跨轮转连续；`agent audit --verify`
  可检测本地篡改。
- 审计/工具日志写盘前脱敏；工具日志的 content/artifact 也会 redact。
- 哈希链不是防篡改存储：拥有主机权限的人仍可重写整条链；高保证场景需要
  外部 WORM/远程日志。

### 残余边界

- shell 递归检索仍是词法 best-effort：凭据关键词会升级为 L2，但变换写法
  仍可能绕过；推荐 `search_code`（过滤 + 脱敏）。
- 内容级 secret 识别是模式匹配，不覆盖任意自定义凭据格式。
- bwrap 是 Linux 命名空间隔离，不防内核逃逸或恶意 WSL 发行版；仍未接入
  seccomp / cgroup。
- 不提供 clone；仓库由管理员预置。Web/GUI 提供受控上传，只写入会话工作区的 `.agent/uploads`，不授予模型改源码的能力，单文件/数量（按工作区跨会话）累计上限由 `AGENT_WEB_MAX_UPLOAD_BYTES` / `AGENT_WEB_MAX_UPLOAD_FILES` / `AGENT_WEB_MAX_UPLOAD_TOTAL_BYTES` 控制。
- 不做跨进程 WSL 总配额；Web 按单进程部署。

## 11. 扩展点

### 加一个工具

1. 在 `tools/` 写 `build_xxx_tools(settings, sandbox)`，返回 `BaseTool` 列表
2. 返回值用 `artifacts.pack(text, artifact)` —— 文本给模型，artifact 给事件层与审计层
3. 在 `tools/registry.py::build_tools` 注册
4. 在 `graph/nodes/approve.py::tool_level` **登记风险等级**
   （未登记的按最危险的 L3 处理，这是故意的 fail closed）

路径参数记得走 `ensure_inside`；命令参数逐项 `shlex.quote`。

### 加一个节点

1. 在 `graph/nodes/` 写节点函数：签名 `(state, config) -> dict`，返回**状态增量**
2. 在 `graph/build.py` 接线
3. 在 `graph/routing.py` 加条件边
4. 若产生用户可见的东西，在 `runtime._stream` 里把对应的 `updates` 翻译成事件
5. **回来同步 `estimate_recursion_limit`** —— 那个公式算歪过三次，每次都是漏算了
   某个会「重新开始」的机制
6. 在 `graph/state.py` 声明新字段，并想清楚**谁写入、谁重置**（漏重置会让状态跨步串味；
   重置过头会把跨步需要的东西清掉 —— `review_watermark` 就是后者）
7. 前端（`cli/app.py`、`tui/app.py`）补事件映射

节点里不要 yield 事件、不要写审计 —— 那些是 runtime 的职责。
唯一例外是 `scheduler` 的 `get_stream_writer()` custom 事件（worker 实时进度），
它只推事件载荷，审计仍由 runtime 统一写。

第 4 与第 7 步都有守卫：`test_graph_event_coverage.py` 用 AST 断言「每个图节点要么
有事件翻译、要么在 `SILENT_NODES` 里显式登记」，漏翻译会当场变红；前端漏映射则
**不报错，只是时间线上少一行**，所以那一步得自己盯着。

### 加一个前端

1. 只依赖 `runtime.AgentRuntime` 与 `events`
2. 消费 `AsyncIterator[Event]`，不认识 LangGraph
3. **不要导入 `tools/` 或 `sandbox/`**（架构测试会拦）

参考 `tui/app.py` 的事件映射表，或 `web/app.py` 里 `model_dump_json()` 直接当 SSE 帧的写法。
桌面 GUI（`desktop/`）只是 `web/` 的本地启动壳：随机 token、loopback、单实例锁；
事件与审批仍由同一套 FastAPI/runtime 处理。新增接口必须同时考虑 Web 多用户授权。

## 12. 目标四层架构（Phase A 契约，Phase B–D 已落地）

Phase A 把拆分写成契约；Phase B 抽出控制面，Phase C 统一执行契约，Phase D
接上 Windows/Linux 双平台部署。当前目标拓扑与三种基线均已可运行，细节见
[PHASE_A.md](PHASE_A.md)、[PHASE_B.md](PHASE_B.md)、[PHASE_C.md](PHASE_C.md)、
[PHASE_D.md](PHASE_D.md)。

> 命名：本节用 **A1 前端 / A2 控制面 / A3 编排面 / A4 执行面**。
> 不要用 L1–L4：L0–L3 是命令风险分级，另有一套“文档 L 层”用法。

```text
A1 前端（浏览器 / pywebview / CLI / TUI）
        │ 控制 API + SSE
A2 控制面（认证、租户、会话目录、配额、Provider、审计索引）
        │ 内部运行 API（loopback/socket + 短期 token）
A3 编排面（LangGraph、LLM、审批决定、checkpoint、steering、编排 worker）
        │ 执行请求（同一 host 或本地 socket）
A4 执行面（shell、文件、Git、测试、快照、worktree、上传）

多实例时：A3 → 共享事件传输层（Redis Streams）→ A2 订阅/代理 SSE；
单实例可继续用内存适配器。MySQL 只做索引，不承担热重放。
```

三种部署基线：

| 基线 | A1 | A2 | A3/A4 |
|---|---|---|---|
| Windows 11 桌面 | pywebview/浏览器 | Windows 原生进程 + 本机 MySQL | 专用 WSL2 发行版 |
| Linux | 浏览器 | systemd + MySQL/MariaDB | 原生 Linux（bwrap/cgroup） |
| Standalone | CLI/TUI | 无 | 原生 WSL/Linux，本地文件/SQLite |

Windows 与 Linux 的执行语义都保持 Linux；Windows 不做原生 bash 工具层。
Standalone 是原 Agent 的保留通道：CLI/TUI 直接持有 A3+A4，不要求 A2 或 MySQL。

## 13. A2/A3 边界与数据归属

- **A2 不执行工具**，也不直接读写工作区；文件/Git/上传/快照由 A3/A4 提供并代理。
- **A3 决定审批与命令分级**，**A4 二次强制**路径/能力限制；A2 只转运审批。
- **MySQL 只属于 A2**：用户、租户、会话目录、运行/审批索引、用量汇总、Provider
  档案、设置、上传元数据。**不存** checkpoint、文件内容、API Key、运行状态。
- **A3 拥有** checkpoint、运行状态、pending approval、steering、记忆与本地审计链。
- **A4 拥有**工作区、快照、附件字节、Git/worktree。
- **密钥**由 A2 的 SecretProvider 托管，短期注入 A3 内存；桌面用 OS 密钥环，
  服务器用 Vault/KMS/systemd-creds；WSL 无 Secret Service 时不落盘、不入
  MySQL/日志/前端。
- **审计**：A3 本地哈希链是可信源，A2 只建查询索引，不重写链。
- **事件**：MySQL 只做查询索引；多实例用共享事件传输层按 `seq` 重放，
  单实例保留内存适配器。
- **A2→A3 内部 API**：`/v1/runs|stream|resume|resume_steering|steer|approvals|
  status|history`，Bearer 内部 token + base64 `X-Agent-Context`；运行生命周期
  可走独立 `coding-agent-agent` 进程，未配置时仍同进程。Phase D 增加
  `/v1/meta`（版本/能力握手）、`/v1/cancel_all` 与 `/v1/shutdown`。

对外控制 API 仍以现有 `/api/*` 为基线；事件唯一契约仍是 `events.py`（28 个领域
事件）。SSE 的 `seq`、`Last-Event-ID` 重放、挂起收尾语义冻结在 Phase A：
`ApprovalRequested` 或 `SteeringApplied(pause)` 表示挂起且不发 `RunFinished`。
默认仍是单进程内存缓冲；配置 `AGENT_EVENT_TRANSPORT=redis` 后，事件同时写入
共享传输，A2 在本地流缺失时按 `seq` 重放（当前轮询式，consumer group 待补）。

## 14. 跨平台、安全与性能边界

**安全**：前端不接触工具/沙箱与 API Key；A2 不执行命令；A3 做策略决定；A4
做最终路径/能力校验；内部接口只走 loopback/本地 socket，远程必须 TLS/mTLS；
Windows 专用 WSL 发行版、非 root、`appendWindowsPath=false`，高风险 profile
关闭 interop；Linux 非 root + bwrap/namespace/cgroup，不可用时 fail closed；
MySQL 仅本机、最小权限；审计按租户单写者。

**性能**：A2↔A3 只传控制与事件，工具 I/O 不跨层；MySQL 不进 SSE 热路径；
Windows 工作区必须放 ext4，禁止每次工具调用跨 `/mnt`；A1 长会话用虚拟滚动/
增量渲染，A3 事件批量输出。

## 15. Phase A 状态

- 文档与契约完成，代码行为未改变；
- 关键路由、事件字段、会话字段与工具清单由
  `tests/unit/test_layer_contracts.py` 冻结；
- Phase B 才引入控制面抽取与 MySQL schema，A3/A4 的功能只做接口包装，不删减。
- Phase B 的五项落地决策（A2 原生、SecretProvider 分档、事件共享传输、
  SQLite 一等公民、静态 SPA + esbuild/TS）已记录在
  [ADR-0010](adr/0010-four-layer-phase-b-decisions.md)。
- Phase B 已落地：可选 `AGENT_CONTROL_DB_URL` 的会话目录、SecretProvider
  接口、内存/Redis 事件传输、SSE 按 seq 重放，以及独立 Agent Server 的
  运行生命周期 + 文件/快照/记忆/上传/审计路由（`AGENT_INTERNAL_URL`）。
  A2 provider key 随运行短期注入 A3 内存；审计索引在查询时拉取并幂等写入
  MySQL。默认仍走旧 JSON/内存/同进程。A2/A3 原生可执行目录与 systemd/
  Windows 开机任务已提供；前端源已迁到 `frontend/src` 并由 esbuild 生成
  `static/app.js`。consumer group、SCM 服务包装与严格 TS 类型待后续。
  见 [PHASE_B.md](PHASE_B.md)。

## 16. Phase C：ExecutionService（C0/C1）

A3→A4 只通过 `ExecutionService`：`ExecutionRequest` 带
`op_id/kind/run_id/payload`，`ExecutionResult` 带 `status/seq/receipt`。
`op_id` 幂等由工作区 `.agent/execution/receipts.jsonl` 承载；`seq` 按 run
单调递增且与 Web SSE 序号独立。

当前实现 `run/read_file/write_file/list_dir/stat/git/snapshot/restore/
worktree_create/worktree_remove/patch_apply`。`AgentRuntime` 用
`ExecutionSandbox` 包住 sandbox：既有 shell/文件/Git/快照/worktree 代码仍
调用 `sandbox.run()`，但实际执行统一经过 A4 backend；工具层没有第二条路径。

工具调用由 `run_id + tool_call_id` 上下文生成稳定 op_id，重试/恢复命中执行
回执；`record_unknown/record_cancelled` 提供 unknown/cancelled 终态。
合并路径的 `git apply` 也走 ExecutionService 并写回执，但回执尚未进入
Scheduler 事件。WSL 与 Linux local 共用 `tests/contract/execution_cases.py`；
Linux CI 跑 local，Windows 本地跑 WSL。详见 [PHASE_C.md](PHASE_C.md)。

## 17. Phase D：双平台部署状态

### Windows 桌面

```text
A1 pywebview → Windows A2（127.0.0.1:8780，随机前端 token）
                         │ loopback + 内部 token
                         ▼
                   WSL A3（127.0.0.1:8790）
```

- A3 先启动并返回端口；A2 注入 `AGENT_INTERNAL_URL/TOKEN` 后启动；
- 端口固定，token 只保存在 supervisor 内存，A3 重启复用同一 token/端口；
- `desktop.watchdog.supervise_many` 同时守护 A2/A3；
- A2 每 5 秒探测 A3，A3 重启后自动从 `unavailable` 回到 `ok`；
- `--host-mode` 保留旧单进程路径一个版本；standalone 不受影响。

### Linux

systemd 顺序：`coding-agent-agent` → `coding-agent-control`；A2 通过
`ExecStartPre=... --migrate-only` 在启动前迁移。`packaging/systemd/install.sh`
创建用户/目录/env/单元，支持打包二进制与源码 venv 两种模式。

### 健康、降级与恢复

- A2 `/healthz`：`version / agent_state / db_state`；
  A3 `/healthz`：`version / api_version`；
- `ControlStore` 初始化或写入失败 → 只读降级，写操作 503，运行/SSE 不受影响；
- 迁移前自动备份；SQLite 复制、MySQL `mysqldump`；恢复脚本默认先备份当前库；
- `scripts/phase_d_smoke.py` 覆盖启动握手、会话、A3 崩溃、A2 重启、
  数据库降级、版本不匹配与迁移失败（7/7 通过）。

### 数据边界不变

Phase D 没有把 checkpoint、工作区、API Key 或运行状态迁入 MySQL；A2 仍不执行
工具，A3/A4 仍持有执行语义。桌面 WSL 完整启动链与真实 LLM run 仍需在打包
环境验收。

## 18. Phase E：前端状态

- A1 只消费后端领域事件；`frontend/src/sse.ts` 负责帧解析、`seq` 去重与
  `Last-Event-ID` 对应的 `after` 重放参数；
- 主题令牌集中在 `style.css`：深/浅/跟随系统，字体、字号、密度与快捷键
  持久化；关键弹窗与对话区带 aria 语义；
- 任务卡显示依赖、worker、耗时、token；推理按节点分块且默认不渲染；
- 长会话用 `content-visibility: auto` 降低渲染成本；代码块使用本地
  highlight.js 与复制按钮；
- `static/app.js` 由 esbuild 生成，CI 重建并检查漂移，同时跑 `tsc --noEmit`。

未做：JS 窗口化虚拟列表、浏览器自动化测试（手测清单见
[PHASE_E.md](PHASE_E.md)）。

## 19. Phase F：安全与性能验收

- **密钥边界**：控制面数据库按字节扫描不含 provider key；Provider API 只返回
  `has_key`，不返回明文；日志与审计在落盘/回灌前经过 `redact_secrets`；
- **内部边界**：A2/A3 只绑 loopback，内部接口必须 token，路径越界在文件工具
  与 ExecutionService 两处拒绝；
- **迁移/回滚**：SQLite 与 MySQL 都完成
  `upgrade → 备份 → downgrade base → upgrade → 恢复` 演练；
- **性能基线**：`phase_f_bench.py` 覆盖 ExecutionService 代理、控制面写入与
  A2 代理 SSE 首事件延迟；不设硬阈值，按同平台趋势比较；
- **平台门禁**：WSL 隔离用 `pytest -m wsl`，Linux bwrap 用
  `pytest -m execution` / `test_bwrap.py`；CI 只跑 SQLite 演练与快反馈，
  平台缺口在 [ACCEPTANCE.md](ACCEPTANCE.md) 中显式保留。

P1 已把 A2↔A3 DTO、事件传输与工作区浏览下沉到 `agent_protocol` /
`event_transport` / `workspace`；`agent_server` 不再依赖 `web/` 或
`control.agent_client`。同进程回退已隔离到 `web/local_runtime.py`；
CLI 的沙箱探测已收窄到 `cli/platform.py`。A2 的系统/能力路由已拆到
`web/routes/system.py` / `providers.py` / `sessions.py` / `workspace.py` /
`uploads.py` / `ops.py` / `snapshots.py` / `runs.py` / `desktop.py` /
`compact.py`，A3 的 RunState/SSE 与租户上下文已拆到
`agent_server_support.py` / `agent_context.py`，A3 路由已全部移到
`agent_server_routes/{lifecycle,workspace,state}.py`；
`create_agent_app` 已降到 321 行，只负责装配。A2 的会话目录/Provider/
Agent 代理已下沉到 `web/services.py`，`create_app` 降到 758 行。

详见 [PHASE_F.md](PHASE_F.md)。
