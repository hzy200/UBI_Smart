---
layer: L2
doc_id: rfc-0006
title: implement 补丁按连通分量增量合并
owner: 项目作者
status: accepted
created: 2026-10-30
last_reviewed: 2026-10-30
review_cadence: 状态变更或范围变化时
---

# RFC-0006：implement 补丁按连通分量增量合并

> 让已完成的 implement 补丁在其它 worker 仍在运行时按连通分量合并，
> 消除"一个慢 worker 阻塞整批补丁及其下游"的队头阻塞；不改变补丁的
> 快照、回滚与崩溃登记语义。

## 1. 目标

- **G1 去队头阻塞**：当已完成的 implement 补丁与所有在飞 implement 的写路径
  不相交时，立即合并，不必等待整批结束。
- **G2 保持分量原子性**：同一连通分量（共享文件）的补丁仍整组留快照、
  顺序 apply、失败整组回滚；不同分量互不影响。
- **G3 保持崩溃可对账**：每次增量 flush 都先写 merge journal，再 apply；
  journal 支持连续多个批次。
- **G4 默认安全回退**：拿不到补丁真实路径、开启 `allow_overlap`、或任何
  判定不确定时，自动退回现有"无在飞 implement 才 flush"的行为。
- **G5 可观测**：审计与（可选）事件能看出每次合并批次包含哪些任务、
  为什么某个已完成补丁还在等待。

可验证目标（实现后写入
[`../acceptance/acceptance-criteria.yaml`](../acceptance/acceptance-criteria.yaml)）：

- `pytest -m "not wsl and not llm" tests/unit/test_scheduler.py tests/unit/test_atomic_merge.py tests/unit/test_merge_journal.py`
- `pytest -m "not wsl and not llm" tests/unit/test_merge_helpers.py`
- 真实 WSL 路径：`pytest -m wsl tests/integration/`（worktree + patch 用例）

## 2. 非目标

- 不做跨连通分量的两阶段提交，也不承诺"整批全成功或全回滚"。
- 不改变 `AGENT_ORCHESTRATOR_ALLOW_OVERLAP=true` 时共享文件的 3-way 预演/合并语义。
- 不引入后台常驻合并线程、队列或跨进程协调。
- 不改变 write_scope 校验、审批、沙箱或路径守卫。
- 不自动 commit，不移动 HEAD，不改变 `agent undo` 的快照语义。
- 不把 `checkpoint_every` 的边界语义与合并时机耦合。
- 不新增前端必需事件；事件是可选增强，缺失时前端仍能工作。

## 3. 背景与证据

### 3.1 当前行为

- `SchedulerRun.drain_completed` 在 implement worker 成功/部分成功后，
  把状态置为 `merging` 并放入 `MergeQueue`：
  `src/ubismart/orchestrator/scheduler_run.py:729-750`。
- 主循环只在**没有任何 implement 在飞**时 flush：
  `src/ubismart/orchestrator/scheduler_run.py:855-864`。
- `MergeQueue.has_active_implement` 也明确只认 `in_flight`：
  `src/ubismart/orchestrator/merge_queue.py:49-66`。
- 结果：一个长跑 implement 会让同批已完成补丁一直停在 `merging`，
  review/test 若依赖这些任务（`depends_mode=any`）也只能等待。

### 3.2 合并层已经是"按分量独立事务"

`merge_implement_patches_atomic` 先把每条补丁 `git apply --check` 并解析
真实路径，再按共享文件分成连通分量：

- `src/ubismart/orchestrator/merge.py:512-589`
- `_patch_components`：`src/ubismart/orchestrator/merge.py:339-385`
- `_merge_component`：各自留快照、顺序 apply、失败只反向撤销本分量：
  `src/ubismart/orchestrator/merge.py:388-509`

因此"等整批"是**调度层**的保守选择，不是合并层的能力限制。
本 RFC 只调整 flush 时机，不重写合并算法。

### 3.3 文档表述需要同步

`src/ubismart/orchestrator/AGENTS.md:19-35` 写的是"同一批补丁是一个事务"。
按 3.2 的实际实现，准确的表述是"同一**连通分量**是一个事务；分量之间
互不回滚"。实现本 RFC 时应一并修正这段文字，避免后来者把整批原子性
当成既有不变量。

### 3.4 路径信息已经可得

- 补丁真实路径解析：`_patch_paths` / `_merge_paths`，
  `src/ubismart/orchestrator/merge.py:81-113`、`:319-336`。
- implement 结果自带改动文件清单：`src/ubismart/orchestrator/implement.py:760-808`
  （作为路径未知时的保守兜底，不作为唯一依据）。

### 3.5 相关技术债

- [`TD-041`](../tech-debt.yaml)：合并批次粒度过粗。
- [`TD-003`](../tech-debt.yaml)：共享文件并发写默认关闭。
- [`TD-043`](../tech-debt.yaml)：顺序写波次（与本 RFC 有依赖关系，
  但由单独的 RFC-0007 决策）。

## 4. 受影响面

| 项 | 内容 |
|---|---|
| 模块 | `orchestrator/merge_queue.py`、`orchestrator/scheduler_run.py`、`orchestrator/merge.py`、`orchestrator/merge_journal.py`（仅可选用途） |
| 契约 | 新增配置 `orchestrator_incremental_merge`（默认 false）；可选新增合并事件 |
| 数据 | 无迁移。journal 仍是单个文件，但会出现"连续多个批次依次 open/close" |
| 前端 | 不强制；若新增 `MergeBatch` 事件需同步生成的 TS 契约 |
| 权限 | 不新增写权限；writescope、审批、路径守卫不变 |

## 5. 安全与隐私

- **不扩大写范围**：只合并已经通过 `git apply --check` 的补丁；路径仍由
  `_patch_paths` 归一化并拒绝工作区外路径（`merge.py:81-113`）。
- **判定不确定即回退**：任何补丁拿不到真实路径、或开启 `allow_overlap`，
  都退回旧行为，不猜测"应该不相交"。
- **快照先于 apply**：每个分量在应用任何补丁前留快照；失败回滚本分量。
- **崩溃登记先于 apply**：每次增量批次在 apply 前写 journal；
  进程被 kill 时下一次运行仍能从 `MergeJournal.notice()` 得到线索。
- **不新增对外暴露面**：合并仍只发生在宿主侧，模型无法触发或跳过。
- **`.agent/**` 排除**：补丁 pathspec 与 journal 位置保持现状；增量合并
  不得把 `.agent/` 纳入任何批次。
- 对照 [`../THREAT_MODEL.md`](../THREAT_MODEL.md)：本改动不新增资产或对手，
  但改变了"主工作区何时变脏"的时间点；审计需能回答"某文件由哪一批合并"。

## 6. 兼容与回滚

- **默认关闭**：`AGENT_ORCHESTRATOR_INCREMENTAL_MERGE=false` 时行为与当前
  完全一致（所有记录都不可增量，等价于 `take_mergeable` 恒返回空）。
- **无数据迁移**：不新增持久化字段；journal 格式不变。
- **回滚方式**：关闭开关并重启即可。已合并的补丁不会因回滚而撤销；
  需要撤销时仍走 `agent undo` 或快照恢复。
- **兼容窗口**：先以开关灰度 ≥1 个版本，观察合并失败率与审计对账，
  再考虑默认开启。
- **可回滚性**：每次 flush 的分量失败只回滚本分量，不回滚其它已完成
  分量；这与现有 `merge_implement_patches_atomic` 行为一致。

## 7. 可观测性

- **审计**：每次 flush 记录 `batch_id`（可用递增序号或时间戳）、
  `task_ids`、`paths`、`merged`、`merge_error`。失败时能回答
  "哪些任务合并了、哪些还没".
- **journal**：`open_batch` 写入当前批次的 task/path；建议额外写
  `merge_paths`（真实路径）而不是只有 `files`（worker 自报），
  以便崩溃后对账更准。
- **事件（可选）**：`merge_batch_started` / `merge_batch_finished`
  （含 `task_ids`、`merged`、`deferred_reason`）。若新增，必须同步
  `events.py`、`runtime/event_map.py` 与生成的 `events.generated.ts`。
- **等待原因**：对未合并的已完成补丁，在 fan-in handoff 的
  `merge_error` 或新增 `merge_wait_reason` 中写明"与在飞任务共享文件"
  或"路径未知".
- **指标**：合并等待时间（worker 完成到 merged 的墙钟）、
  增量批次大小、回退旧行为的次数。

## 8. 验收标准

```text
Given 两个 implement 写不同文件，其中一个 worker 仍在运行
When  另一个 worker 完成，补丁 git apply --check 通过
Then  已完成的补丁立即按连通分量合并并写入最终状态；
      在飞 worker 不受影响；合并前留快照；未完成补丁不进入本批
锚点：REQ-224
tests: tests/unit/test_scheduler.py::test_disjoint_completed_implement_merges_while_another_is_in_flight
```

```text
Given 两个 implement 补丁命中同一文件，且另有 worker 在飞
When  开启增量合并
Then  两个共享文件的补丁都不提前应用（或按 allow_overlap 的 3-way 预演
      整组处理）；拿不到补丁真实路径时回退到"无在飞 implement 才 flush"
锚点：REQ-225
tests: tests/unit/test_scheduler.py::test_shared_file_implement_still_waits_for_batch
       tests/unit/test_scheduler.py::test_unknown_patch_paths_fall_back_to_batch_flush
```

补充验收（实现时一并落地，是否登记新 REQ 由评审决定）：

- 连续两个增量批次后 journal 为空，且第二批崩溃时只保留第二批登记。
- 关闭开关时，所有既有 `test_scheduler.py` / `test_atomic_merge.py` 用例行为不变。
- `test_atomic_merge.py::test_failure_only_rolls_back_its_component` 仍然通过。

验收锚点已写入
[`../acceptance/acceptance-criteria.yaml`](../acceptance/acceptance-criteria.yaml)
的 `REQ-224` / `REQ-225`（当前 `status: planned`，实现后改为 `implemented`
并补上 `tests:`）。

## 9. 替代方案与代价

| 方案 | 优点 | 代价 / 风险 | 为什么没选 |
|---|---|---|---|
| A 按连通分量增量 flush（本 RFC） | 消除队头阻塞；复用现成分量原子性；语义增量小 | 需要提前解析补丁真实路径；flush 次数变多 | 选 |
| B 维持整批等待（现状） | 实现最简单；一次批量校验 | 慢 worker 阻塞全批与下游；吞吐随最长任务退化 | 否 |
| C 每条补丁完成即合并 | 延迟最低 | 共享文件必须 3-way；顺序/冲突语义变化大；与默认不重叠约束冲突 | 否 |
| D 后台合并线程/队列 | 调度主循环更干净 | 引入生命周期、取消、崩溃恢复与并发读工作区的新问题 | 否 |
| E 只调大 `MAX_PENDING_MERGES` | 无代码改动 | 不解决队头阻塞，只是允许更多在飞 | 否 |

### 9.1 开放问题

1. **路径预解析的开销**：每条 implement 完成时多一次 `git apply --numstat`。
   在 WSL 上这是额外进程启动。是否只在"队列中已有其它已完成补丁"或
   "仍有 implement 在飞"时才预解析？
   - 建议：MVP 阶段只在确实可能增量时解析；否则延迟到 flush。
2. **未知路径的保守度**：一条未知路径是否应阻塞所有增量，还是只阻塞
   自身？建议只阻塞自身，但如果它可能与他人共享文件，无法证明不相交，
   实现上按"整批回退"更安全。需要在实现时用测试锁定选择。
3. **事件是否进入 v1**：建议 v1 只加审计，事件留到有前端消费需求时。

## 10. 决策与后果

- 最终选择：方案 A（按连通分量增量 flush），默认关闭，灰度后启用。
- 批准人 / 日期：项目作者 / 2026-10-30。
- 真实结果与预期差异：实现后再补。
- 新增技术债：
  - 未知路径一律回退整批 flush（保守但会损失部分增量收益）；
  - 路径预解析在 WSL 上多一次进程启动，需用压测确认可接受。
- 关联 ADR：[`0026-incremental-implement-merge`](../adr/0026-incremental-implement-merge.md)。

## 11. 任务分解与验证计划

| # | 任务 | 负责模块 | 验证方式 |
|---|---|---|---|
| 1 | 暴露补丁路径预解析（复用 `_patch_paths`），失败返回未知 | `orchestrator/merge.py` | `test_merge_helpers.py::test_patch_paths_parses_numstat`、新增未知路径用例 |
| 2 | 实现分量选择：pending 内先分组，再与在飞路径求不相交 | `orchestrator/merge_queue.py` | 新增 `test_merge_queue.py::test_take_mergeable_*` |
| 3 | `flush` 支持指定子集；journal 每批次 open/close | `orchestrator/merge_queue.py` | `test_merge_journal.py::test_two_incremental_batches_leave_no_registration` |
| 4 | worker 完成时缓存 `merge_paths`；失败/未知不阻塞其它分量 | `orchestrator/scheduler_run.py` | `test_scheduler.py::test_unknown_patch_paths_fall_back_to_batch_flush` |
| 5 | 主循环每轮尝试增量 flush；收尾仍强制 flush 全部 | `orchestrator/scheduler_run.py` | `test_scheduler.py` 新增 2 条、既有合并用例全绿 |
| 6 | 配置开关、默认关闭 | `config.py`、`.env.example` | `gen_contracts.py --check`、`test_config_sources.py` |
| 7 | 审计字段；可选事件 | `scheduler_run.py`、`events.py`、`event_map.py` | `test_events.py`、`test_runtime_orchestrator.py` |
| 8 | 修正 AGENTS 表述；补 ADR；更新技术债 | `orchestrator/AGENTS.md`、`docs/adr/`、`docs/tech-debt.yaml` | `check_docs.py`、`check_acceptance` |
| 9 | 灰度与压测 | CI / `scripts/phase_f_bench.py` | 对比合并等待时间与失败率 |

### 11.1 实施顺序

1. **Phase 0（无行为变化）**：路径预解析 + 分量选择 + 单元测试，开关关闭。
2. **Phase 1（开关内）**：主循环增量 flush；补 journal 多批次测试；
   跑真实 WSL 合并用例。
3. **Phase 2（可观测）**：审计字段；按需加事件。
4. **Phase 3（灰度）**：在长任务集上对比"合并等待时间 / 下游启动时间 /
   合并失败率"；无退化后考虑默认开启。

### 11.2 完成定义

- 开关关闭时与现状行为等价，全部既有测试通过。
- 开关打开时，REQ-224/225 对应测试存在且通过。
- `ruff check src tests`、`pytest -m "not wsl and not llm"`、
  `python scripts/gen_contracts.py --check`、`python scripts/check_docs.py`
  全绿。
- 至少一次真实 WSL 演练：两个 implement、一个慢 worker、一个共享文件场景，
  确认快照、回滚与 journal 对账符合预期。

## 12. 与 RFC-0007（顺序写波次）的关系

本 RFC 不改变"worktree 基于 HEAD"这一事实，因此**不能**单独支持
`implement depends_on implement`。顺序写还要求把上一波合并后的工作区
改动注入新 worktree（见 [`TD-043`](../tech-debt.yaml)），那属于 RFC-0007
的范围。两项可以独立实施：先做增量合并降低延迟，再做顺序写扩展能力。
