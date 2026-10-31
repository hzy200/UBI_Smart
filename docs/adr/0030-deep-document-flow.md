# ADR-0030：长文档全文覆盖、map-reduce 与证据写作

- **Status**：accepted
- **Date**：2026-10-31
- **Deciders**：项目作者
- **Owner**：项目作者
- **Related**：[RFC-0009](../rfc/0009-deep-workflows-and-knowledge-artifacts.md)、
  [RFC-0005](../rfc/0005-document-image-and-report-pipeline.md)

## Context

文档管线（RFC-0005/ADR-0024）解决了“能读/能写/能导出”，但长文档仍有三处硬上限：

- PDF 每次 5 页、最多 3 次（15 页）且不报告未覆盖区间；
- 单文件 36k 字符、所有 chunk 一次性送入结构化调用；
- author 只拿 12k 依赖上下文，证据细节进不了写作。

这会让报告“看起来像分析了全文”，实际漏读；跨文档综合也只能靠压缩摘要。

## Decision

新增两个默认关闭的开关，并统一覆盖报告：

1. `AGENT_DOCUMENT_FULL_COVERAGE`（需同时开启
   `AGENT_DEEP_WORKFLOWS_ENABLED`）：取消固定 3 次读取；页数上限由
   `AGENT_DOCUMENT_MAX_PAGES`（默认 200）控制。
2. `AGENT_DOCUMENT_MAP_REDUCE`：按 `AGENT_DOCUMENT_CHUNK_CHARS` 切分，分批
   调用结构化抽取，再合并 summary/points/open_questions；单批失败只影响该批。
3. `WorkerResult.coverage`：每个文档记录 `read_ranges`、`unread`、`status`
   与原因；任一文档 partial 时整体 `status=partial`，并在 findings 中给出
   “覆盖：path 已读…未读…”。
4. **即使开关关闭也报告未覆盖区间**：默认仍只读前 15 页，但不再静默。
5. author 在 deep 模式下使用 `AGENT_AUTHOR_EVIDENCE_MAX_CHARS`（默认 24k）与
   `AGENT_AUTHOR_EVIDENCE_TOP_K`，上下文优先渲染 `claim + quote + anchor`。

## Consequences

**正面**

- 长文档覆盖可证；partial 不再伪装成全文分析。
- 单份长文档可以分批并行抽取，避免一次 prompt 塞爆上下文。
- 写作层拿到的是证据而不是只有摘要，引用更容易落地。

**负面 / 代价**

- full coverage 会增加读取与模型调用次数；由 page cap、chunk 并发与总预算约束。
- map-reduce 的合并是确定性的，不做跨批语义去重；重复结论可能保留。
- OCR 仍默认关闭；扫描页只会被标记为未覆盖。
- `document_max_pages=200` 仍是硬上限；超过部分必须显式 partial。

## Alternatives

| 方案 | 为什么没选 |
|---|---|
| 只调大 `max_calls` | 上下文与成本失控，且没有覆盖报告 |
| 整篇塞进超长上下文模型 | 成本高、不可控；provider 上限不一致 |
| 引入外部文档 RAG 服务 | 数据出本机；与默认隐私边界冲突 |
| 只改提示词“请读全文” | 工具层根本不会读超过 15 页，属于无效指令 |
