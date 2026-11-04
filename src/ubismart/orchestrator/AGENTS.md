<!-- owner: 项目作者 · last_reviewed: 2026-10-22 -->

# orchestrator/ · harness 子图

独立子图：`survey → select_and_plan → write_gate? → scheduler ⇄ steering_dispatch → fan_in`，
结束后把结果交回主 agent 做最终实现与答复。小仓库由 runtime 选 `direct`，不进这里。

## 硬约束（改之前先看 `graph.py::validate_tasks` 与 `plan_review.py`）

- **契约先于任务图**：`plan_tasks` 先归一化 `TaskContract`，再交给
  `plan_review.review_plan`。`unknown` 非空或评审失败时路由直接去 `synthesize`，
  **不进入 scheduler**；不要为了“跑通”把它们降级成警告。
- 契约至少一条 `auto=True` 的 acceptance；每个任务必须映射到至少一条
  acceptance（`acceptance_refs` 或自身 `acceptance`）。不可验证
  （`verifiable=false`）与无界分支（`branching=unbounded`）必须拆分/加上界。
- 任务 `id` 唯一、`goal` 非空；`depends_on` 必须存在、无自依赖、无环。
- implement 必须有非空 `write_scope`。默认 **implement 之间不能互相依赖**；
  仅当 `AGENT_ORCHESTRATOR_SEQUENTIAL_WRITES=true` 时，允许依赖 `stage`
  严格更小的 implement（worktree 会注入前一波基线）。同一 stage 的并发
  implement 仍必须两两不重叠、不能互相依赖。跨 stage 依赖用
  `depends_mode="any"`：`partial` 可能已经合并，下一波仍需继续。
- 只读角色不能声明 `write_scope`。implement 不允许 `depends_mode=any`。
- 运行中只允许追加**只读**任务（explore/test/review）；worker 提议也只能是只读，
  且每个 worker 最多 2 个，经 host 校验（角色/去重/依赖/总量/无环）才入队。
- 校验失败 → **整图降级**（写模式退化为单个保守 implement，只读模式退化为单 explore），
  并显式标记 `degraded`。**不要静默截断任务数**。

## 写路径（风险最高）

implement 在独立 `git worktree` 中修改，完成后导出 patch，宿主在主工作区
**顺序** `git apply`：先全部 `--check` → 解析路径 → 按共享文件分连通分量
→ 每个分量留快照 → 顺序 apply → 任一失败只反向撤销本分量。**分量是事务，
分量之间互不回滚**。增量合并开启时，无交集的已完成分量可提前合并。

前置条件：`AGENT_ORCHESTRATOR_WRITE_ENABLED=true` + 会话 `--write` +
工作区是 git 仓库根且干净（`.agent/`、`__pycache__/` 不计入脏改动）。
顺序写 stage>0 与修复轮允许 dirty，但脏路径必须 ⊆ 本 run 已合并路径。

## 文档 worker（`document_workers.py`）

- 多文档输入上限 64；证据点上限随文档数放大，最终 sources 按文档轮转，
  不要让一份长文档占满上限。
- map-reduce 用 `_split_sections`（章节感知），不要退回纯字符切分；
  `document_read` 的元信息头在 map 前剥离。
- 每条证据的 quote 必须经 `_verify_source_quotes` 定位；`quote_verified=False`
  或 `anchor_valid=False` 的证据不能进入 author/judge。
- 每篇矩阵（`DocumentMatrix`）是 author 的固定槽位输入；空槽位用已验证
  claim 回填。矩阵默认关闭（`AGENT_DOCUMENT_MATRIX_ENABLED=false`）。
- 多文档归并用 `_tree_reduce`（fanout + 单次输入上限），不要把所有摘要
  塞进一次调用；最终答案附紧凑矩阵与“已解析文档（N 篇）”头。
- 跨文档归属用 `knowledge/attribution.py`；`attribution_issues` 是硬信号，
  但只能发现路径错配，不能替代语义 judge。
- 报告复核只检查显式报告路径，不要把上游 PDF 当成报告。

## 已知易错点（都出过 P0）

- worktree/patch 落在主工作区 `.agent/` 下，**脏检查必须排除 `.agent/` 与
  `__pycache__/`**，否则第二个 worker 会把它们误判成用户改动。
- implement 结果进入合并队列时**立即置 `merging` 并离开 pending**，
  否则 `refresh_ready` 会把它重新排进 ready，同一补丁被重复合并。
- 合并错误要写进审计 detail（`merge_error`），否则事后无法定位。
- 并发区**不弹审批、不调用 `interrupt()`**；审批属于主图或编排边界。

## 调度与预算

ready 最小堆 + `asyncio.wait(FIRST_COMPLETED)`，完成即解锁依赖，不等整批。
`checkpoint_every` 只决定何时返回父图，**不再限制每边界能启动几个**。
token 预留用同角色历史用量 EWMA，预算不足时 next-fit 选放得下的任务，
不是把所有 pending 一起标 blocked。

`budget.py::BudgetGuard` 是四维预算（steps / tool_calls / tokens / wall_ms）的
唯一口径：70% 发 `BudgetWarning`，100% 停止派发并输出
`budget_abort`（已完成部分 + 卡点 + 建议）。模型给的
`max_tokens` / `timeout` / `priority` 一律在派发时夹到父预算，
**不能信任计划里的预算字段**；`TaskSpec.max_tokens` 不是放大切片的许可。

`estimator.py` 在 S5 产出 Estimation Card：规则先验 + `task_type × role × model`
历史分桶；只读步骤图/历史，不调用执行工具。`gate.checkpoint_granularity=fine`
会把 `checkpoint_every` 收紧到 1，`watchdog_ms` 会作为外层硬超时上限。
不要让模型直接报一个耗时数字。

steering 新增 `abort` / `rollback(n)` / `patch`：`patch` 只改 contract 的
`goal/constraints/known/unknown` 并递增 `plan_version`，然后走重规划；
`rollback` 只负责停止当前运行，由 runtime 在流结束后调用 `rewind` 回退并等用户
resume 继续。不要把 rollback 实现成在流式执行中直接改 checkpoint。

阶段门与轻量控制检查点在 `control_checkpoint.py`：`phase_gates` 开启且确实
有多阶段时，每阶段结束写一个**只含契约/任务/产物引用/预算/早停**的 JSON，
不存全量消息；阶段失败或阶段预算耗尽只停止后续阶段，前序检查点保留。
`control_checkpoints` 会进入 fan-in handoff；`stage_done` 事件/卡片是中间交付点。

进程兜底在 `platform/process_runner.py`：只给可 pickle 的顶层函数用；spawn 子进程
超时直接 terminate/kill。当前接入点是文档解析（PDF/Office/EPUB）；普通工具仍走
`to_thread`。不要把需要 sandbox/长连接的调用塞进进程池。

嵌套编排（`nested.py` + `SubTask/SubResult/SubSignal`）默认关闭，开启后
`MAX_DEPTH=1`、单层扇出有限。子编排器是**同一套控制面子图的递归实例**：
父只发 SubTask、收 SubResult；子 `suppress_events=True`，不推前端卡片；
子任务强制只读，因此审批仍只在 Root。子 `TokenBudget` 以父切片为上限，
`actual_cost` 回填父预算。不要把 `agents/explore` 这类自治 while 循环当成
子编排器；也不要在子图里再派生子图。
