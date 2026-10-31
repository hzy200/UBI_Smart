# ADR-0031：ChangeContract 与接口优先的顺序写波次

- **Status**：accepted
- **Date**：2026-10-31
- **Deciders**：项目作者
- **Owner**：项目作者
- **Related**：[RFC-0009](../rfc/0009-deep-workflows-and-knowledge-artifacts.md)、
  [ADR-0029](0029-knowledge-artifacts.md)、[ADR-0027](0027-sequential-write-waves.md)

## Context

RFC-0009 已落地 `ArchIndex` / `impact_analysis` / `KnowledgeStore`，但规划层还没有
把“受影响模块与调用方”变成可执行的写顺序：

- `validate_tasks` 默认要求同 stage 的 implement 互相独立、写范围不重叠；
- 跨模块接口变更（先改定义、再改调用方）要么被模型拆错 stage，要么退化 direct；
- 即使有多个 stage，也没有“上一波验证通过才放开下一波”的 gate。

## Decision

1. **确定性契约** `build_change_contract(arch, request)`：
   - 用 `impact_analysis` 得到 seed 文件/模块、受影响文件/模块/测试；
   - 提取 seed 文件中的公共符号作为 `interfaces`（带 callers）；
   - 以 seed 模块为第 0 波，按反向 import 逐层展开 `waves`，每波带
     `files/modules/gate_tests`；无法静态建图时 `fallback=True`。
2. **确定性分波** `apply_change_contract(tasks, contract)`：
   - 只调整 implement 的 `stage`（取命中的最大波次），不增删任务、不改依赖；
   - 同 stage 写范围冲突仍由 `validate_tasks` 拒绝（fail-closed）。
3. **规划接线**：
   - survey 在有契约时把 `change_contract` 放入 state 与 `repo_summary`；
   - plan 在 validate **之前**应用契约；契约有多个波次时自动启用顺序写
     （`allow_sequential_write=True`），stage 上限取
     `max(orchestrator_max_write_stages, deep_max_write_stages, 波次数)`。
4. **波次 gate**（`AGENT_INTERFACE_FIRST_WRITES=true`）：
   - 每一波 implement 全部 success/partial 并合并后，运行
     `run_verification(allow_manifest=False, only=gate_tests)`；
   - `ok` / `not_configured` / `skipped` 视为通过（没有测试可跑时不应假装有）；
   - 失败或该波有 failed/blocked/cancelled：阻塞所有更高 stage 的 pending 任务，
     记录 `wave_gates`，停止派发；已合并的上一波不回滚。
5. **持久化与检索**：契约作为 `contract` kind 写入 KnowledgeStore，
   `knowledge_read/search` 可读取。

默认关闭：需要 `AGENT_DEEP_WORKFLOWS_ENABLED=true` +
`AGENT_ARCH_WORKFLOW_ENABLED=true` 生成契约；gate 额外要求
`AGENT_INTERFACE_FIRST_WRITES=true`。

## Consequences

**正面**

- 跨模块变更有了可解释的“先接口、后调用方”顺序，而不是靠模型自觉。
- gate 把错误挡在波次边界，避免后续波次基于坏接口继续写。
- 契约同时是可检索产物，排查时能看到 seed/影响面/波次。

**负面 / 代价**

- 契约依赖 import 图；`unparsed/dynamic` 时 `fallback=True`，分波可能不完整。
- gate 会额外跑一次受影响测试；无测试时按 `not_configured` 放行。
- 契约只按写范围匹配分波；一个任务跨多波时会被放到较晚波次。
- 不解决同一 stage 内的语义冲突，仍需 review/patch review 兜底。

## Alternatives

| 方案 | 为什么没选 |
|---|---|
| 让模型自行排序 stage | 不可复现，且经常把调用方放前面 |
| 把所有跨模块改动强制 direct | 长程任务失去并行与恢复能力 |
| 每个文件一个波次 | 过细，wall-clock 与 gate 成本过高 |
| gate 失败回滚已合并波次 | 会丢弃已完成且已验证的成果；只停止后续波次更稳 |
