---
layer: L2
doc_id: rfc-0007
title: implement 顺序写波次与 worktree 基线注入
owner: 项目作者
status: accepted
created: 2026-10-30
last_reviewed: 2026-10-30
review_cadence: 状态变更或范围变化时
---

# RFC-0007：implement 顺序写波次与 worktree 基线注入

> 允许后一波 implement 基于前一波已合并结果继续修改，覆盖"先重构、再加功能"
> 这类有写依赖的任务；核心是决定如何把主工作区的未提交合并结果注入新 worktree，
> 同时保持 write_scope、快照、回滚与审批边界不变。

## 1. 目标

- **G1 顺序写**：允许 `implement` 依赖另一个 `implement`，但仅当依赖方 `stage`
  严格更小；同 stage 仍要求互相独立、write_scope 不重叠。
- **G2 基线可见**：stage=N 的 worktree 必须看到 stage<N 已合并到主工作区的改动
  （包括新增文件），否则顺序写在语义上不成立。
- **G3 隔离不变**：仍使用独立 `git worktree`；不自动 commit、不移动 HEAD。
- **G4 事务不变**：每一波仍按连通分量原子合并；后一波失败不回滚前一波。
- **G5 fail-closed**：基线注入拿不到完整改动、超过文件/字节上限、或脏文件
  无法证明来自本 run 的合并时，拒绝启动该 worker，而不是带着不完整基线继续。
- **G6 默认关闭**：`AGENT_ORCHESTRATOR_SEQUENTIAL_WRITES=false` 时恢复
  "implement 之间不得互相依赖"的现状。

可验证目标：

- `pytest -m "not wsl and not llm" tests/unit/test_orchestrator.py tests/unit/test_scheduler.py tests/unit/test_implement_runner_unit.py`
- `pytest -m wsl tests/integration/`（真实 worktree + 两波合并）

## 2. 非目标

- 不做任意 DAG 的写依赖：只允许按 stage 递增的波次，禁止同 stage 依赖。
- 不自动 commit、不创建临时分支、不移动主工作区 HEAD。
- 不改变 RFC-0006 的分量合并语义；也不要求两波之间使用 3-way。
- 不把主工作区改成"每波都干净"；相反，它会在波次之间保持 dirty。
- 不新增审批/沙箱/路径守卫规则；write_scope 仍是三重防线之一。
- 不支持并发波次；同一时刻只有一个 stage 在写。

## 3. 背景与证据

### 3.1 当前限制

- `validate_tasks` 禁止 implement 依赖 implement：
  `src/ubismart/orchestrator/graph.py:192-203`。
- 规划提示明确要求 implement 互相独立：
  `src/ubismart/orchestrator/plan.py:84-90`。
- `orchestrator/AGENTS.md:8-16` 把这条写成硬约束。

### 3.2 worktree 看不到未提交合并

- `implement.py` 用 `git worktree add --detach HEAD` 创建隔离目录：
  `src/ubismart/orchestrator/implement.py:467-517`。
- `git apply` 只修改主工作区，不移动 HEAD；因此新 worktree 看不到上一波
  已合并但未提交的改动。
- `_git_ready` 要求工作区干净：`src/ubismart/orchestrator/implement.py:168-211`。
  顺序写进入第二波时主工作区必然是 dirty，这条检查会直接拒绝启动。

### 3.3 依赖解锁点已经存在

- implement 的 `success` 只在合并成功后写入：`scheduler_run.py` 的
  `drain_completed` / `finalize` / `merge_queue.flush`。
- `deps_met` 要求依赖 `success`（或 `any` 模式的 `partial`）：
  `src/ubismart/orchestrator/scheduler_plan.py:22-30`。
- `stage_ready` 已经提供低 stage 收敛屏障：
  `src/ubismart/orchestrator/scheduler_run.py:222-245`。

因此调度层不需要新节点，主要工作是"放宽校验 + 注入基线 + 调整干净检查"。

### 3.4 相关债务

- [`TD-043`](../tech-debt.yaml)：顺序写波次。
- [`TD-044`](../tech-debt.yaml)：编排失败回流依赖本 RFC。
- [`TD-003`](../tech-debt.yaml)：共享文件并发写默认关闭。

## 4. 受影响面

| 项 | 内容 |
|---|---|
| 模块 | `orchestrator/graph.py`、`plan.py`、`implement.py`、`scheduler_run.py`、`scheduler_plan.py`、`AGENTS.md` |
| 契约 | 新增配置：`orchestrator_sequential_writes`、`orchestrator_max_write_stages`、`orchestrator_seed_max_files`、`orchestrator_seed_max_bytes` |
| 数据 | 无迁移；worktree/patch 仍在 `.agent/` 下；快照按波次保留 |
| 前端 | 可选：任务图显示 `stage`；无强制契约变化 |
| 权限 | 不新增；write_scope 在 I/O 前仍强制 |

## 5. 安全与隐私

- **基线来源限定**：seed 只允许来自本 run 已合并的补丁；用调度器记录的
  `merged_paths` 与快照 id 交叉验证。
- **排除项**：`.agent/`、`__pycache__/`、快照/worktree 目录永远不进入 seed。
- **上限**：seed 文件数、总字节数、单文件字节数都有上限；超限 fail closed。
- **路径守卫**：seed 生成的路径仍经现有 patch pathguard；不允许绝对路径或 `..`。
- **审批**：不改变审批语义；seed 不是用户新指令，只是把已合并结果复制到隔离区。
- **脏文件**：第二波启动前若主工作区存在不属于本 run 合并结果的改动，拒绝该
  worker，并在审计中列出差异，避免把用户未提交的修改带进 worker。
- 对照 [`../THREAT_MODEL.md`](../THREAT_MODEL.md)：本 RFC 扩大的是"worker 可见
  范围"，不是"可写范围"。

## 6. 兼容与回滚

- **默认关闭**：关闭时 `validate_tasks` 恢复"implement 不得互相依赖"；
  `_git_ready` 恢复"必须干净"。
- **无数据迁移**：配置开关即可回退；已合并的波次不会自动撤销。
- **回滚方式**：关闭开关后新任务不再生成跨波依赖；已有补丁仍按快照/审计处理。
- **兼容窗口**：先限制为 ≤2 波、每波 ≤2 个 implement；稳定后再放宽。

## 7. 可观测性

- **审计**：每个 implement 记录 `stage`、`seed_files`、`seed_bytes`、`seed_digest`、
  `baseline_source`（第几波合并 + 快照 id）。
- **事件**：可选 `task_wave_started`；不新增也可从 `WorkerStarted` 的 task 元数据
  读取 stage。
- **失败诊断**：seed 失败、脏文件无法归类、超限时，错误信息必须列出具体路径
  与触发规则。
- **对账**：merge journal 仍按批次记录；崩溃后能看到"哪一波合到哪一步"。

## 8. 验收标准

```text
Given 任务 A(stage=0) 与 B(stage=1)，B depends_on A，写范围允许重叠
When  A 合并成功
Then  B 的 worktree 包含 A 的全部改动（含新增文件），B 才能开始
锚点：REQ-226
tests: tests/unit/test_scheduler.py::test_second_wave_worktree_sees_first_wave_changes
       tests/unit/test_implement_runner_unit.py::test_worktree_is_seeded_from_dirty_workspace
```

```text
Given 同 stage 的两个 implement 写范围重叠
When  校验任务图
Then  仍整图降级，不因为"顺序写"而放宽同 stage 约束
锚点：REQ-227
tests: tests/unit/test_orchestrator.py::test_same_stage_overlap_still_rejected
```

补充验收：

- 关闭开关时，`test_implement_tasks_cannot_depend_on_each_other` 保持通过。
- 第 2 波合并失败只回滚第 2 波；第 1 波改动与快照不变。
- seed 超限 / 脏文件无法归属时 worker 失败，且不产生部分 worktree。

验收锚点写入
[`../acceptance/acceptance-criteria.yaml`](../acceptance/acceptance-criteria.yaml)
的 `REQ-226` / `REQ-227`（当前 `status: planned`）。

## 9. 替代方案与代价

| 方案 | 做法 | 优点 | 风险 | 结论 |
|---|---|---|---|---|
| A 工作区 diff 注入（推荐） | `git diff --binary HEAD` + 未跟踪文件补丁，在新 worktree apply | 不改 HEAD/索引；保留隔离与快照 | 未跟踪/二进制文件需单独处理；patch 可能较大 | 选 |
| B 每波临时 commit | 合并后 commit，再 worktree from commit | 实现简单 | 改变 HEAD/undo/脏状态语义，违背"不自动提交" | 否 |
| C 主工作区串行 | 顺序波次不用 worktree | 无基线问题 | 失去隔离、补丁与整批回滚 | 否 |
| D 每波同步整个工作区目录 | rsync/cp | 简单 | 慢、易把 .agent/用户改动带进去 | 否 |

### 9.1 推荐实现细节

1. **生成 seed patch**（主工作区）：
   - `git diff --binary HEAD -- . ':(exclude).agent'`
   - 未跟踪文件：`git ls-files --others --exclude-standard -- . ':(exclude).agent'`，
     逐个 `git diff --no-index --binary /dev/null <path>` 追加；
   - 检查文件数/字节数上限。
2. **在新 worktree 应用**：
   - `git -C <worktree> apply --binary <seed.patch>`；
   - 失败则删除 worktree 并让该任务失败（fail closed）。
3. **干净检查调整**：
   - stage=0 仍要求主工作区干净；
   - stage>0 允许 dirty，但要求 `git status --porcelain` 的路径集合
     ⊆ 本 run 已合并路径集合（排除 `.agent/`）；
   - 不属于该集合的改动 → 拒绝启动并审计。
4. **校验**：
   - `implement.depends_on` 中的每个 implement 必须 `stage` 严格更小；
   - 同 stage 的 implement 两两不得依赖、write_scope 不得重叠；
   - 跨 stage 允许 scope 重叠；
   - 依赖方必须在被依赖方合并成功后才 ready（现有 `deps_met` 已满足）。
5. **合并**：
   - 建议与 RFC-0006 同时启用，否则后一波要等所有 in-flight implement 结束；
   - 波次间仍按连通分量原子合并。
6. **依赖口径**：implement→implement 依赖使用 `depends_mode="any"`。
   `partial` 在合并层表示"补丁已合并、worker 内部验证未过"，下一波仍应
   基于这个合并结果继续；合并失败会变成 `failed`，不会误放行。

## 10. 决策与后果

- 最终选择：方案 A（工作区 diff + 未跟踪文件补丁注入 worktree）。
- 批准人 / 日期：项目作者 / 2026-10-30。
- 真实结果与预期差异：实现后补。
- 新增技术债：
  - 未跟踪文件超过上限时整波失败（保守，不截断）；
  - 脏文件归属只做路径集合校验，不比较内容 hash。
- 关联 ADR：[`0027-sequential-write-waves`](../adr/0027-sequential-write-waves.md)。

## 11. 任务分解与验证计划

| # | 任务 | 负责模块 | 验证方式 |
|---|---|---|---|
| 1 | 校验放宽：stage 严格递增、同 stage 仍不重叠 | `orchestrator/graph.py` | `test_orchestrator.py` 新增 3 条 |
| 2 | 规划提示与默认任务 stage | `orchestrator/plan.py` | `test_orchestrator.py` |
| 3 | seed patch 生成与应用 | `orchestrator/implement.py` | `test_implement_runner_unit.py` |
| 4 | 干净检查按 stage/已合并路径调整 | `implement.py`、`scheduler_run.py` | `test_implement_runner_unit.py`、`test_scheduler.py` |
| 5 | 调度解锁与 stage 断言 | `scheduler_run.py`、`scheduler_plan.py` | `test_scheduler.py::test_second_wave_*` |
| 6 | 配置开关与默认值 | `config.py`、`.env.example` | `gen_contracts.py --check` |
| 7 | 审计/事件/文档/ADR | `scheduler_run.py`、`orchestrator/AGENTS.md`、`docs/adr/` | `check_docs.py` |
| 8 | 两波真实 WSL 演练 | `scripts/phase_f_drill.py` / integration | `pytest -m wsl` |

### 11.1 实施顺序

1. Phase 0：配置开关 + 校验放宽 + 单元测试（关闭时行为不变）。
2. Phase 1：seed patch 生成/应用 + 失败 fail closed。
3. Phase 2：干净检查与调度解锁；≤2 波灰度。
4. Phase 3：与 RFC-0006 一起压测；稳定后放宽波次/任务数。

### 11.2 完成定义

- 开关关闭时全部既有测试通过。
- 开关打开时 REQ-226/227 测试存在且通过。
- 至少一次真实 WSL 两波演练：第一波合并、第二波基于它修改、第二波失败只回滚
  第二波。
- `ruff`、快反馈测试、契约/文档检查全绿。

## 12. 开放问题

1. **未跟踪二进制文件**：`git diff --no-index --binary /dev/null file` 是否在所有
   目标 git 版本上稳定？是否需要限制为文本文件并明确拒绝二进制？
2. **脏文件归属**：用"路径 ⊆ 已合并路径"判定是否足够？是否还要比较 blob hash？
3. **波次上限**：默认 2 波还是 3 波？建议先 2 波，压测后再放宽。
4. **与失败回流（TD-044）的关系**：本 RFC 完成后，verify 失败重入 implement
   才有可靠基线；届时再单独设计回流的轮次与失败签名。
