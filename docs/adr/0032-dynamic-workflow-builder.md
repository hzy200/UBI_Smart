# ADR-0032：复杂度信号驱动的动态图构建器

- **Status**：accepted
- **Date**：2026-10-31
- **Deciders**：项目作者
- **Owner**：项目作者
- **Related**：[RFC-0009](../rfc/0009-deep-workflows-and-knowledge-artifacts.md)、
  [ADR-0031](0031-interface-first-waves.md)

## Context

四个固定模板（direct / parallel_readonly / orchestrated_implement /
document_pipeline）在复杂跨模块变更上仍要模型自行拆 stage；ChangeContract
已经能给出确定性波次，但 workflow 选择层还没有使用它。

## Decision

新增 `orchestrator/workflow_builder.py`：

1. `build_dynamic_plan(request, summary, settings, write_available)` 仅在
   `AGENT_DEEP_WORKFLOWS_ENABLED=true`、可写、且契约有 ≥2 个波次时生效。
2. 直接产出 `architecture_change` 任务图：
   - 每波按 write_scope 生成 implement；同波写范围去重且不重叠；
   - 后一波 `depends_on` 前一波、`depends_mode=any`；
   - 有受影响测试时追加 `test` 任务；
   - 任务数受 `orchestrator_max_tasks` 限制。
3. 仍走现有 `validate_tasks` / scheduler / merge / gate；校验失败按既有规则降级。
4. 信号不足（单波、不可写、未开 deep）返回 None，回退四模板。
5. `architecture_change` 加入 workflow 枚举、路由、write_mode 与 write_gate。

## Consequences

**正面**

- 跨模块任务不再依赖模型“自觉”排序；契约直接变成可执行 DAG。
- 仍复用已验证的调度、恢复与波次 gate。

**负面 / 代价**

- 新工作流增加一个枚举与路由分支，文档/前端需要认识它。
- 任务粒度按写范围分组；非常大的波次会合并成少量宽任务。
- 没有契约时不生效，复杂但无 ArchIndex 的仓库仍走旧模板。

## Alternatives

| 方案 | 为什么没选 |
|---|---|
| 让模型继续自由生成任务图 | 不可复现，跨模块顺序容易错 |
| 再写一套 LangGraph 子图 | 与 scheduler 的恢复/并发语义重复，风险高 |
| 所有任务都强制 architecture_change | 简单任务会多付 survey/契约成本 |
