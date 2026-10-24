# ADR-0011：宿主侧执行白名单（绕开工具层的确定性路径）

- **Status**：accepted
- **Date**：2026-10-24
- **Owner**：项目作者
- **Tags**：安全 · 编排 · 合并 · worktree

## Context

模型发起的命令必须走 `tools/` → 审批 → 沙箱（ADR-0001、ADR-0004）。但编排层
自己也需要执行一些命令：把 implement worker 的补丁合并回主工作区、创建/清理
worktree、生成补丁、做合并前的仓库画像。这些操作不是模型直接发起的，也没有
"给模型一个 `git apply` 工具"的选项 —— 那会把一次内部原子操作暴露成可被模型
组合、重试、越权的工具调用，直接放大攻击面。

因此必须存在一条**宿主侧内部执行**通道。问题是：这条通道如果没有任何约束，
就会变成事实上的"第二执行层"——任何新代码都能以"内部需要"为名绕过命令分级，
而审计里看不出差别。

可选方案：

1. **把所有内部 git 操作也做成工具，让模型调用**。否决：合并必须由宿主保证
   串行、原子、失败可回滚；交给模型意味着它可以选择不合并、重复合并或改参数。
2. **内部执行完全不加约束，靠代码评审**。否决：评审不会跟着新调用点持续生效，
   而且审计与静态检查都无法回答"这条命令为什么可以绕过审批"。
3. **显式标记 + kind 白名单 + 命令前缀校验**。采纳。

## Decision

新增 `execution/host_ops.py` 作为宿主内部执行的**唯一入口**：

```python
host_run(sandbox, command, *, origin, kind, cwd)
```

- `origin` 必须是字面量 `"host"`；其他值（包括缺省/来自模型/来自配置）一律
  抛 `HostExecutionDenied`。
- `kind` 必须在 `HOST_KINDS` 白名单里，且命令前缀必须与 `kind` 匹配。
- 允许剥离 `GIT_INDEX_FILE='...'` 这类**带引号的环境变量前缀**再匹配，
  以支持 3-way 预演的临时 index。

当前白名单（与 ADR 同 PR 增删）：

| kind | 命令前缀 | 用途 |
|---|---|---|
| `git_apply` | `git apply` | 合并/回滚/`--check`/`--numstat`/`--3way` |
| `git_read_tree` | `git read-tree` | 3-way 临时 index 预演 |
| `git_reset` | `git reset` | 3-way 后恢复未暂存语义 |
| `git_worktree` | `git worktree` | worktree 创建/移除/list/prune |
| `git_patch_prepare` | `git add`、`git diff` | 生成 worker 补丁 |
| `git_probe` | `inside=$(git` | 写 worker 前置检查脚本 |
| `worktree_prepare` | `mkdir -p` | worktree 父目录 |
| `worktree_cleanup` | `rm -rf` | 取消/失败时清理 worktree |
| `merge_temp_cleanup` | `rm -f` | 清理 3-way 临时 index |
| `snapshot_read` | `python3 -` | 合并前批量读取主工作区文件 |
| `repo_survey` | `echo "AGENT_DIRS"` | 编排前只读仓库画像 |

约束落地范围：

- `orchestrator/merge.py`：`git apply --check/--numstat/--3way/apply/-R`、
  `read-tree`、`reset`、`rm -f`、快照读取；
- `orchestrator/implement.py`：`git worktree add/remove/prune`、`mkdir -p`、
  `rm -rf`、`git add/diff`、前置检查脚本；
- `orchestrator/survey.py`：只读仓库画像脚本。

这些路径的上游保障：

- **审批已在上游发生**：只有 `status=success` 且拿到审批结果的 implement
  worker 才会产生补丁；合并阶段不再接受模型输入。
- **确定性**：命令由宿主拼接（路径经 `shlex.quote`），参数来自
  `WorkerResult`/任务图，不是模型自由文本。
- **原子性**：同组补丁先全部留快照，失败反向 apply，必要时回退快照。
- **审计**：worker 的工具调用、合并结果与快照 id 仍进审计；宿主 git 内部命令
  本身不伪装成 `ToolCallFinished`。

## Consequences

### 好的

- 新增宿主执行必须显式登记 kind 并更新 ADR，评审有明确检查点；
- 静态测试禁止 `orchestrator/` 再出现 `sandbox.run`，第二执行层不会悄悄扩散；
- 未知 `origin`/`kind`/命令不匹配时 fail-closed，测试可直接覆盖；
- 合并/worktree 的 git 语义仍集中在一处，便于保证原子与回滚。

### 坏的 / 代价

- **每次新增内部命令都要改白名单**。这是有意的摩擦，类似未登记工具按 L3 处理；
- 白名单只校验命令前缀，不是完整的 shell 解析器；它保证"不会混进明显无关的
  命令"，但不能证明脚本内部没有别的副作用 —— 脚本本身仍是可信代码；
- 环境变量前缀需要白名单识别，写法受限（目前只支持带引号的赋值）；
- 宿主 git 命令不产生 `FileChanged`，改动靠合并后的快照与验证覆盖。

### 中性 / 需要留意

- `cli/platform.py` 是前端层唯一触达 `WslSandbox` 的窄接口（只读探测），
  不属于本 ADR 的合并通道；它由 `test_architecture.py` 锁定。
- `agents/policy.py`、`codeintel/`、`instructions.py`、`memory/longterm.py`、
  `tenancy.py` 仍有只读/建目录的宿主调用；它们不参与补丁合并，暂不纳入
  `host_run`。若未来在其中引入写操作，应一并迁移并登记 kind。

## 复核

- **Verified by**：
  `pytest -m "not wsl and not llm" tests/unit/test_host_ops.py tests/unit/test_atomic_merge.py tests/unit/test_architecture.py`
- **Revisit when**：需要新增宿主写操作、引入新的执行后端，或把合并迁到独立
  进程/服务时。若宿主通道开始承载模型可控参数，应改为走工具与审批，而不是
  扩充本白名单。
