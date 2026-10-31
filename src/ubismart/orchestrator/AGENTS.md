<!-- owner: 项目作者 · last_reviewed: 2026-10-22 -->

# orchestrator/ · harness 子图

独立子图：`survey → select_and_plan → write_gate? → scheduler ⇄ steering_dispatch → fan_in`，
结束后把结果交回主 agent 做最终实现与答复。小仓库由 runtime 选 `direct`，不进这里。

## 硬约束（改之前先看 `graph.py::validate_tasks`）

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

## 调度

ready 最小堆 + `asyncio.wait(FIRST_COMPLETED)`，完成即解锁依赖，不等整批。
`checkpoint_every` 只决定何时返回父图，**不再限制每边界能启动几个**。
token 预留用同角色历史用量 EWMA，预算不足时 next-fit 选放得下的任务，
不是把所有 pending 一起标 blocked。
