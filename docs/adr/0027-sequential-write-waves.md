# ADR-0027：implement 顺序写波次与 worktree 基线注入

- **Status**：accepted
- **Date**：2026-10-30
- **Deciders**：项目作者
- **Owner**：项目作者
- **Related**：[RFC-0007](../rfc/0007-sequential-write-waves.md)

## Context

并发 implement 的 worktree 都从同一个 `HEAD` 创建，而合并只修改主工作区的
未提交内容、不移动 `HEAD`。因此：

- `validate_tasks` 原先禁止 implement 之间互相依赖；
- 进入第二波时主工作区必然 dirty，`_git_ready` 的"必须干净"检查会拒绝启动；
- 新 worktree 也看不到第一波已合并的改动。

"先重构、再加功能"这类有写依赖的任务只能退回主 agent 串行执行。

## Decision

新增开关 `AGENT_ORCHESTRATOR_SEQUENTIAL_WRITES`（默认 `false`）。开启后：

1. 只允许 implement 依赖 `stage` **严格更小**的 implement；同 stage 仍必须
   互相独立、write_scope 不重叠；跨 stage 允许 scope 重叠。
   跨 stage 依赖自动使用 `depends_mode="any"`：`partial` 表示补丁已合并、
   只是 worker 内部验证未过，下一波仍应继续；合并失败会变成 `failed`。
2. stage>0 的 worker 在新 worktree 创建后注入主工作区已合并改动：
   - `git diff --binary HEAD` + 未跟踪文件逐个 `git diff --no-index --binary`；
   - 文件数/字节数超限 → 失败，不截断；
   - 排除 `.agent/`、`__pycache__/`。
3. 注入后在 worktree 内 `git add -A` + `git write-tree` 记录基线树，
   再 `git reset -q`；**不 commit、不移动 HEAD**。
4. worker 的 `_changed_files` / `_make_patch` 以基线树为 diff 基准，
   避免把注入的改动算成自己的产出。
5. `_git_ready` 对 stage>0 允许 dirty，但脏路径必须 ⊆ 本 run 已合并路径；
   出现无法归属的改动 → fail closed。
6. `merged_paths` 随 scheduler 状态跨 checkpoint 边界传递。

## Consequences

### 正面

- 支持有写依赖的多波任务，同时保留 worktree 隔离、快照与分量原子合并；
- 基线注入不改 HEAD/索引，不产生临时提交；
- 默认关闭，关闭时校验与干净检查完全恢复旧行为。

### 代价与风险

- seed 需要额外的 git 调用与补丁文件，超限时整波失败；
- 未跟踪文件逐个生成 diff，极端数量下成本较高（已有上限）；
- 脏文件归属只做路径集合校验，不比较内容 hash；用户在同一路径上手工改动
  无法被区分——这是 RFC 登记的残余风险；
- 第二波必须等第一波 **合并成功** 才能开始，不能与第一波并行。

## Alternatives

| 方案 | 优点 | 代价 | 结论 |
|---|---|---|---|
| 工作区 diff 注入 | 不改 HEAD，保留隔离 | 未跟踪文件需单独处理 | 选 |
| 每波临时 commit | 实现简单 | 改变 HEAD/undo 语义 | 否 |
| 主工作区串行 | 无基线问题 | 失去隔离与回滚 | 否 |
| 目录同步 | 简单 | 慢且易带入 `.agent`/用户改动 | 否 |
