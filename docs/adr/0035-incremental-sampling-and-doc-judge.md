# ADR-0035：增量索引、多语言抽样与文档答案 judge

- **Status**：accepted
- **Date**：2026-10-31
- **Deciders**：项目作者
- **Owner**：项目作者
- **Related**：[RFC-0009](../rfc/0009-deep-workflows-and-knowledge-artifacts.md)、
  [ADR-0034](0034-deep-workflow-evals.md)

## Context

P2 剩余三项都围绕“大规模/长期使用”：

- 每次 survey 都重新解析全部 Python 文件，重复工作；
- 只索引 `.py`，大型多语言仓库的架构视图不完整；
- 文档评测只有覆盖率/引用比例，缺“答案是否有证据”的自动判断。

## Decision

### 1. 增量 Python 索引

- `codeintel/python_index.py` 暴露 `parse_python_source` /
  `build_index_from_entries`；
- `knowledge/index_cache.py` 按**仓库根**缓存
  `.agent/artifacts/knowledge/index/<ns>.json`，每个文件记录内容哈希与解析结果；
- `build_index_incremental` 只重解析变化文件；缓存读写失败回退全量解析；
- 统计 `changed/reused/cache_hit` 进入 `repo_summary.index_stats`。

### 2. 多语言与规模抽样

- `knowledge/polyglot.py` 定义 python/web/go/rust/jvm 语言组；
- 先 `count_files` 一次拿到各语言文件数，再按比例分配
  `AGENT_ARCH_MAX_FILES` 配额，只对存在的语言组调用 `collect_sources`；
- 非 Python 用轻量正则提符号；import/调用边仍只对 Python 建图；
- `ArchIndex.sample` 记录 `languages/sampled/total_files/truncated`；
  **`truncated=True` 时 `impact_analysis.fallback=True`**，契约与分波必须保守。

### 3. 文档答案 judge

- `knowledge/judge.py` 提供 `deterministic_judgement`：
  - 证据 citation 的 anchor/quote 比例；
  - 答案中 `path#anchor` 是否能对应到已知证据；
  - 输出 0–100 分与 citation_issues。
- `judge_document_answer(llm=None, ...)`：无 LLM 时纯确定性；有 LLM 时要求
  逐条判断结论是否有证据；LLM 失败回退确定性并记录 error。
- `scripts/eval_documents.py --judge/--judge-llm` 接入。

## Consequences

**正面**

- 大仓库二次 survey 只解析变化文件；多语言仓库有基本的符号视图。
- 抽样/无法解析/动态 import 都会显式 `fallback=True`，不会假装影响面完整。
- 文档 judge 可离线运行；LLM judge 失败不会把结果误判成“答案很差”。

**负面 / 代价**

- 增量缓存按仓库根存放，路径变化会重新建缓存；跨机器不共享。
- 非 Python 只有符号，没有 import/调用边；跨语言影响分析仍不完整。
- 抽样会漏文件，`fallback=True` 要求调用方保守，可能降低并行收益。
- 确定性 judge 只看引用形式，不判断语义正确性；LLM judge 仍有成本。

## Alternatives

| 方案 | 为什么没选 |
|---|---|
| 每次全量 AST 解析 | 大仓库重复 CPU/IO，survey 变慢 |
| 只索引 Python | 多语言仓库的架构视图缺失 |
| 抽样失败静默继续 | 影响分析会假装完整，跨模块契约可能漏调用方 |
| 只用 LLM judge | 成本高、失败无回退；形式指标仍应先兜底 |
