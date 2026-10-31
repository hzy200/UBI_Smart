# ADR-0029：知识产物层——架构索引、简报与后续文档索引

- **Status**：accepted
- **Date**：2026-10-31
- **Deciders**：项目作者
- **Owner**：项目作者
- **Related**：[RFC-0009](../rfc/0009-deep-workflows-and-knowledge-artifacts.md)

## Context

orchestrator 的 worker 之间只通过文本汇总传递理解：

- fan-in 默认 8k 字符（`orchestrator_context_chars`）；
- author 默认 12k 依赖上下文；
- repo map 只有“文件: 顶层符号”，4k 截断；
- explore worker 无状态，多个 worker 对同一架构的认知不累积。

`codeintel/python_index.py` 已有 import 图、`importers_of` 与测试影响分析，但这些
信息只用于测试选择，没有进入规划提示。结果是：复杂代码库的理解每次从零开始，
跨模块变更看不到调用方与接口边界；长文档写作也拿不到足够的证据细节。

## Decision

引入 **知识产物层**，先落确定性、可验证的部分：

1. 新增 `src/ubismart/knowledge/architecture.py`：
   - `ArchIndex`：文件、符号、import 边、反向 import、module→file、测试、入口、
     高被依赖文件、无法静态解析/动态 import 的文件。
   - `arch_brief()`：把索引压成 ≤4k 的确定性简报。
   - `impact_analysis()`：显式文件/符号 → 反向 import 闭包 → 受影响文件/模块/测试；
     遇到 `unparsed`/`dynamic` 时返回 `fallback=True`，要求调用方保守处理。
2. survey 在 `AGENT_DEEP_WORKFLOWS_ENABLED=true` 且
   `AGENT_ARCH_WORKFLOW_ENABLED=true` 时构建 `ArchIndex`，把 `arch_brief` 与
   `arch_impact` 写入 `repo_summary`；`compact_repo_summary` 将它带进规划提示。
3. 产物默认不落盘；后续 `DocIndex` / `ClaimStore` / `KnowledgeRead/Search` 工具
   统一放 `.agent/artifacts/knowledge/<run_id>/`，需显式开启
   `AGENT_KNOWLEDGE_ARTIFACTS_ENABLED`。
4. 产物带 `git_head` 指纹；与 worker journal 的复用口径一致。
5. 远程 embedding 默认关闭；开启必须显式配置并写审计。

## Current scope（2026-10-31）

- 已实现：`ArchIndex` / `arch_brief` / `impact_analysis`，并接入 survey 与规划提示。
- 已实现：`KnowledgeStore` 落盘 `.agent/artifacts/knowledge/<run_id>/`，
  `DocIndex`（doc chunks）与 `ClaimStore`（claim + quote + anchor）；
  `knowledge_read` / `knowledge_search` 只读工具，run 作用域由 ContextVar 注入，
  artifact_id 经过安全化，不能传路径。
- 未实现：向量检索/远程 embedding、动态图构建器、效果评测集。
  `ChangeContract` 与接口优先波次见 [ADR-0031](0031-interface-first-waves.md)。

## Consequences

**正面**

- 架构理解先由确定性代码产出，可审计、可测试，再决定是否让模型总结。
- 规划提示能看到 import 反向边、热点与受影响测试，不再只有 top_dirs。
- `fallback=True` 明确暴露“无法静态建图”，避免假装影响面很小。
- 开关默认关闭，现有行为不变。

**负面 / 代价**

- survey 在大仓库上多一次 AST 索引构建；已有 `collect_python_sources` 上限
  （2000 文件 / 32MB）兜底。
- 产物目前只在内存/state 中，不跨 run 持久化；长文档产物仍是后续工作。
- import 图不是调用图；跨模块语义仍需要 worker 读取具体文件。

## Alternatives

| 方案 | 为什么没选 |
|---|---|
| 继续放大 fan-in / repo_map 文本 | 成本线性上升，仍无法表达反向依赖与接口边界 |
| 直接接外部代码索引服务 | 数据出本机、运维与合规成本高；默认不允许 |
| 只用 LLM 总结架构 | 不可复现，无法验证“看到了什么” |
| 一次性实现完整调用图 | 解析成本与语言覆盖风险高；先用 import 图拿到主要收益 |
