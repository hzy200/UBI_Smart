---
layer: L2
doc_id: rfc-0009
title: 复杂长文档与代码架构的深层工作流
owner: 项目作者
status: accepted
created: 2026-10-31
last_reviewed: 2026-10-31
review_cadence: 状态变更或范围变化时
---

# RFC-0009：复杂长文档与代码架构的深层工作流

> 当前系统强在“安全、可恢复、可并行分派”，弱在“深度理解与跨源综合”。
> 本 RFC 不再靠调并发或提示词，而是引入**可持久化的知识产物层**，并在其上
> 为长文档与代码架构各建一条专用动态图。效果优先，成本次之，耗时第三。
>
> 本 RFC 不替代 [RFC-0005](0005-document-image-and-report-pipeline.md)（文档管线已实现）；
> 它解决的是 RFC-0005 之后仍未覆盖的“长文档全文覆盖 + 跨源综合”和
> “代码架构理解 + 跨模块变更”。

## 1. 目标

- **G1 全文覆盖**：长文档不再静默只读前 15 页；覆盖范围必须显式可查
  （complete / partial + 未覆盖区间）。
- **G2 可检索证据**：写作层按章节/问题检索证据，而不是只拿到 12k 汇总文本。
- **G3 架构可复用**：代码库的模块、符号、import 依赖、测试拓扑形成可复用产物，
  后续所有 worker 共享，而不是各自从零探索。
- **G4 跨模块变更可控**：接口优先的顺序波次 + 波次 gate，支持跨模块改动。
- **G5 动态图**：按任务复杂度选择/组合阶段，而不是四个固定模板硬套。
- **G6 可度量**：长文档与架构任务各有可运行的效果评测，优化前先能量化。

非目标见 §2；所有能力由总开关控制，默认关闭时现有行为完全不变。

## 2. 非目标

- 不引入新的调度引擎；仍复用 `orchestrator/scheduler_run.py` 的 ready 堆、
  stage、依赖、额度与取消语义。
- 不做多人实时协同编辑、CRDT/OT。
- 不默认调用外部 embedding / 云端索引；远程 provider 必须显式开启并审计。
- 不改变命令分级、审批、`write_scope`、路径守卫与 fail-closed 语义。
- 不在本期实现完整调用图；P0 只做 import 图 + 符号索引 + 影响分析，
  call graph 作为 P2 选项。
- 不承诺所有格式：旧 OLE（.doc/.ppt）仍按 RFC-0005 的非目标处理。

## 3. 背景与证据

### 3.1 长文档链路的硬上限

| 位置 | 现状 | 后果 |
|---|---|---|
| `orchestrator/document_workers.py:180-185` | PDF 每次 5 页、最多 3 次 | 超过 15 页的部分**静默不读** |
| `document_workers.py:261-343` | 单文档顺序读、batch 拼接 | 单份长文档没有页级并行 |
| `document_workers.py:35` | 单文件最多 36k 字符 | 长文档被截断 |
| `document_workers.py:366-375` | 所有 chunk 一次性送入结构化调用 | 无 map-reduce |
| `document_workers.py:557-570` | author 只拿 12k 依赖上下文 | 写作层看不到细节证据 |
| `orchestrator/fanin.py:146-148` | fan-in 文本 8k | 跨文档综合被压缩 |
| `config.py` 文档并发 | document=2、author=1 | 并发在文档级，不在 chunk 级 |

已有 `DocumentPoint{claim, evidence_path, anchor, quote, confidence}`
（`document_workers.py:39-45`）是很好的证据模型，但写作层没有利用它做检索。

### 3.2 代码架构链路的硬上限

| 位置 | 现状 | 后果 |
|---|---|---|
| `orchestrator/survey.py` | 只采集顶层目录/文件、git 状态 | 架构画像浅 |
| `codeintel/python_index.py::build_repo_map` | 文件 → 顶层符号，4k 截断 | 无 import 边、接口边界、调用关系 |
| `codeintel/python_index.py` | 已有 import 图与 `importers_of` | 信息未进入 planner/worker |
| `agents/explore.py` | 8 轮、120s、无状态 | 多个 worker 的架构理解不累积 |
| `orchestrator/fanin.py` | 8k 文本汇总 | 深层架构认知丢失 |
| `config.py` | implement 12 轮/240s；同 stage 不可依赖；`sequential_writes=false`；`max_write_stages=2` | 跨模块接口变更难以表达 |
| `orchestrator/implement.py` | 受影响测试子集 + 合并后全量 verify | 缺接口/import 层架构验证 |

### 3.3 评测缺口

`tests/eval/` 有通用编码任务，但没有：

- 长文档：全文覆盖、多跳问答、引用精确率/召回率、数字/表格保真；
- 架构：跨模块改动的隐藏测试、接口一致性、编译/类型/import 检查。

没有这组指标，无法判断“更努力”是否等于“更有效”。

## 4. 总体方案

三层，按顺序落地：

```text
┌─────────────────────────────────────────────────────────┐
│ 3. 动态图构建器（选择/组合阶段，复用 scheduler）          │
├─────────────────────────────────────────────────────────┤
│ 2. 专用工作流：长文档 map-reduce；代码架构 impact/waves   │
├─────────────────────────────────────────────────────────┤
│ 1. 知识产物层：DocIndex / ClaimStore / ArchIndex / Brief  │
│    + 专用只读工具 + run 级存储 + worker journal 复用      │
└─────────────────────────────────────────────────────────┘
```

关键原则：

1. **产物优先于文本**。跨 worker 传递的是可查询的结构化知识，不是越来越长的摘要。
2. **阶段复用现有机制**。新工作流仍产出任务图（`TaskSpec` + `stage` + `depends_on`），
   由现有 scheduler/merge/review 执行；不新增第二套并发运行时。
3. **覆盖与证据可校验**。每个结论都能回到 `path#anchor`；每个文档都有覆盖报告。
4. **默认关闭、可回滚**。总开关关闭时旧路径逐字节不变。

## 5. 知识产物层

### 5.1 数据模型

```text
KnowledgeArtifact (base)
  artifact_id, run_id, kind, version, created_at, source_hashes[]
  retention: run | session

DocIndex
  doc_id, path, sha256, pages, format
  sections[]: {section_id, title, anchor, order}
  chunks[]:   {chunk_id, section_id, anchor, text, tokens, embedding_ref}
  entities[]: {entity_id, name, type, mentions[]}
  coverage:   {status: complete|partial, unread: [anchor...], reason}

ClaimStore
  claim_id, text, doc_ids[], evidence[]:
    {doc_id, anchor, quote, chunk_id}
  confidence, created_by

ArchIndex
  modules[], files[], symbols[]
  edges[]: {src, dst, kind: import|call|inherit, confidence}
  tests[]: {path, covers[]}
  entrypoints[], hotspots[]

ArchBrief
  summary, subsystems[], boundaries[], risks[], open_questions[]
  based_on: {arch_index_id, git_head, generated_at}

ChangeContract
  request_id, affected_files[], affected_modules[]
  interfaces[]: {name, current, target, callers[]}
  acceptance[], affected_tests[]
  waves[]: {stage, tasks[], gate[]}
```

### 5.2 存储与访问

- 位置：`.agent/artifacts/knowledge/<run_id>/`，与现有 `ArtifactStore` 同域；
  不进版本控制，沿用 `.agent/` 的脏检查排除规则。
- 写入：只由宿主/worker runner 调用 `knowledge_write`（L1，受 run 作用域限制），
  模型不能自由写任意路径。
- 读取：新增只读工具 `knowledge_read`、`knowledge_search`：
  - `knowledge_read(artifact_id, selector)`：按 section/chunk/edge 读取；
  - `knowledge_search(artifact_id, query, top_k)`：向量或关键词检索。
  - 路径由宿主拼装，工具只接受 artifact_id + 选择器，拒绝 `..` 与绝对路径。
- 隐私：artifact 是明文文档/代码摘要，权限等同 `.agent/`；默认 7 天 TTL，
  可用 `AGENT_KNOWLEDGE_ARTIFACT_TTL_DAYS` 调整。
- Embedding：默认 `hash`（仅测试）或 `local`（fastembed）；`remote` 必须显式开启
  `AGENT_EMBEDDING_PROVIDER=remote` 并写入审计（内容会离开本机）。
- 复用：与已实现的 `session/worker_journal.py` 对接；产物带 `source_hashes`
  与 `git_head`，命中相同指纹才复用。

### 5.3 生命周期

```text
build → validate → use → update（源码/文档变更时）→ expire
```

- build/update 都是普通 worker 任务，走现有预算、超时、重试与审计。
- 产物带 `version`；schema 变更只增字段，读取方忽略未知字段。

## 6. 长文档工作流

### 6.1 阶段

```text
0 ingest/parse     全文解析，产出 DocIndex 骨架（不做 15 页截断）
1 chunk            语义分段（标题/页码/表格边界优先），带稳定 anchor
2 map              每 chunk 抽取 claim/实体/表格（便宜模型，并发，有界）
3 reduce           按 section / document 合并 ClaimStore，去重、冲突标记
4 outline          强模型生成报告大纲（章节、必答问题、覆盖约束）
5 draft            每节一个 writer；按 query 检索 top-k 证据后写作
6 verify           锚点存在性 + LLM groundedness + 数字/表格一致性
7 assemble/format  report_export → report_check → 可选 LLM review
```

### 6.2 覆盖契约（G1）

- `document_full_coverage=true` 时，PDF 读取次数由**页数与 token 预算**决定，
  不再被 `document_read_max_calls=3` 截断。
- 若因预算/OCR/解析失败未能读完，必须产出 `coverage.status=partial`
  与 `unread` 区间，并进入 `open_questions` / 报告“局限性”。
- 禁止“读了 15 页但报告看起来像读了全文”。

### 6.3 证据检索（G2）

- `author` 不再只接收 `_context_text()` 的 12k 摘要；每节写作调用
  `knowledge_search(doc_index_id, section_query, top_k=author_evidence_top_k)`，
  拿回 claim + quote + anchor。
- 报告末尾的“参考来源”由写入的 citation 自动生成；`report_check` 校验锚点存在。
- 证据不足时明确写“证据不足”，不编造。

### 6.4 并发与成本

- map 阶段在 worker 内部用有界 `asyncio.gather`（`document_map_concurrency`），
  单份长文档也能并行。
- 模型分层：map/抽取用 worker 便宜模型；outline/draft/verify 用主模型或 review 模型。
- 失败隔离：单 chunk/section 失败只重试该片，不重跑整篇。

### 6.5 动态性

- 1 篇短文档 → 直接 extract + draft（不建索引）；
- ≥N 页或多文档 → 建 DocIndex + ClaimStore；
- 跨文档综合 → 先 reduce 再 outline；
- 判定阈值来自页数、文档数、问题是否需要跨源。

## 7. 代码架构工作流

### 7.1 阶段

```text
0 arch survey   并发只读任务：
                模块清单 / 入口 / import 图 / 公共接口 / 测试拓扑 / 热点
                → ArchIndex + ArchBrief（产物，不是 8k 文本）
1 impact        对本次请求做确定性影响分析：
                PythonIndex.importers_of + symbol refs + 测试拓扑
                → ChangeContract（文件、接口、验收、受影响测试）
2 waves         按依赖顺序生成波次：
                interface-first：先改接口/契约，再改调用方
                同一 wave 内并发；wave 之间设 gate
3 gate          编译 / import / 受影响测试 / 可选类型检查
4 integrate     合并后全量 verify + review + architecture delta
```

### 7.2 产物如何进入 worker

- `ArchBrief` 以压缩文本 + `knowledge_read` 句柄两种形式提供：
  - system prompt 放 ≤4k 的摘要（谁依赖谁、边界、风险）；
  - 需要细节时 worker 用 `knowledge_search` 查模块/符号/边。
- planner 拿到的不再是“top_dirs 列表”，而是 `affected_modules` 与 `interfaces`。

### 7.3 跨模块写策略（G4）

- 当 `ChangeContract` 检测到“接口变更 + 多个调用方”或受影响模块 >1 时，
  自动启用顺序波次（不要求用户手动开 `AGENT_ORCHESTRATOR_SEQUENTIAL_WRITES`）。
- 每个波次携带契约；wave 内仍要求写范围不重叠。
- 波次 gate 失败只回滚/修复该波次，不重跑已完成波次；沿用现有 merge 分量事务。
- `max_write_stages` 在 deep 模式下提高到 4（可配置），仍受全局预算与 deadline 约束。

### 7.4 角色

- 新增只读角色 `architect`：维护 `ArchIndex/ArchBrief`，不修改源码。
- 复用现有 `explore/review/test/implement`；不新增调度语义。
- `architect` 的工具集与 explore 相同（只读），避免扩大攻击面。

## 8. 动态图构建器

- 节点库（都是现有能力的组合）：
  `ingest / extract / index / analyze / contract / implement / verify / review / synthesize`。
- 选择信号：
  - 文档：页数、文档数、是否跨源；
  - 代码：文件数、import 边密度、受影响模块数、是否跨接口；
  - 请求：交付物数量、是否要求报告/架构说明。
- 输出仍是 `TaskSpec[]`，经 `validate_tasks` 校验；校验失败按现有规则降级，
  不静默截断。
- 回退：任何信号不足或构建失败 → 现有四模板（direct/parallel_readonly/
  orchestrated_implement/document_pipeline）。
- 预算：每个阶段有独立 token/时间上限；总预算仍由
  `orchestrator_total_tokens` 与会话预算约束。

## 9. 契约与配置

### 9.1 配置（全部默认关闭/保守）

| 配置 | 默认 | 作用 |
|---|---|---|
| `AGENT_DEEP_WORKFLOWS_ENABLED` | `false` | 总开关 |
| `AGENT_KNOWLEDGE_ARTIFACTS_ENABLED` | `false` | 产物层 |
| `AGENT_KNOWLEDGE_ARTIFACT_TTL_DAYS` | `7` | 产物保留 |
| `AGENT_DOCUMENT_FULL_COVERAGE` | `false` | 取消 15 页硬上限 |
| `AGENT_DOCUMENT_MAP_REDUCE` | `false` | 分批抽取 + 合并 |
| `AGENT_DOCUMENT_CHUNK_CHARS` | `6000` | chunk 大小 |
| `AGENT_DOCUMENT_MAP_CONCURRENCY` | `3` | chunk 并发 |
| `AGENT_DOCUMENT_MAP_MODEL` | `""` | map 阶段模型 |
| `AGENT_AUTHOR_EVIDENCE_TOP_K` | `12` | 每节检索证据数 |
| `AGENT_ARCH_WORKFLOW_ENABLED` | `false` | 架构工作流 |
| `AGENT_ARCH_BRIEF_MODEL` | `""` | ArchBrief 生成模型 |
| `AGENT_ARCH_IMPACT_MAX_MODULES` | `40` | 影响分析上限 |
| `AGENT_INTERFACE_FIRST_WRITES` | `false` | 自动接口优先波次 |
| `AGENT_DEEP_MAX_WRITE_STAGES` | `4` | deep 模式波次上限 |

### 9.2 状态字段

新增（均可为空，旧 checkpoint 兼容）：

```text
knowledge_run_id: str
doc_index_id: str
doc_coverage: dict          # {status, unread[], reason}
arch_index_id: str
arch_brief_id: str
change_contract_id: str
wave_gates: list[dict]
```

### 9.3 事件与审计

- P0 复用现有 `WorkerFinished` / `TaskGraphCreated` / `TaskBlocked`，
  产物 ID 写入 `findings`/审计 detail，避免事件契约扩张。
- P1 再评估新增 `KnowledgeArtifactCreated` / `WaveGateFailed`；
  新增事件必须同步 `events.py` 与 `gen_contracts.py`。

## 10. 安全与隐私

- **新增资产**：文档正文、代码摘要、claim/evidence、embedding。
- **存储**：`.agent/artifacts/knowledge/`，不进版本控制；目录 `0700`；
  沿用 `.agent/` 的脏检查排除；`git_add` 仍拒绝暂存。
- **访问**：`knowledge_*` 工具只接受宿主签发的 `artifact_id` 与选择器；
  拒绝绝对路径、`..`、符号链接逃逸；实现复用现有 `ensure_inside`/`realpath`。
- **模型输入**：证据文本回灌前走现有脱敏（`redact_secrets`），与审计同口径。
- **外部依赖**：远程 embedding 默认关闭；开启后写入审计，明确“内容会离开本机”。
- **不改变安全边界**：命令分级、审批、`write_scope`、fail-closed 全部不变；
  产物层不是新的执行通道。

## 11. 兼容与回滚

- 所有开关默认 `false`；关闭后行为与当前完全一致。
- 新增字段均为可选；旧 checkpoint 读取时缺失字段按空处理。
- 产物 schema 只增不改；读取方忽略未知字段。
- 回滚：关闭 `AGENT_DEEP_WORKFLOWS_ENABLED`；产物目录可直接清理，
  不影响 checkpoint 与审计。
- 不涉及 MySQL 迁移，不改变 A2/A3/A4 契约。

## 12. 可观测性

建议指标（先写审计/事件，再考虑仪表盘）：

| 指标 | 含义 |
|---|---|
| `doc_coverage_percent` | 已读页/总页；partial 时必须暴露 |
| `doc_map_calls` / `doc_reduce_calls` | map-reduce 调用量 |
| `invalid_citation_ratio` | 锚点校验失败比例 |
| `evidence_top_k_hit` | 检索命中率（有证据的节占比） |
| `arch_impact_modules` | 影响分析模块数 |
| `wave_gate_failures` | 波次 gate 失败次数 |
| `arch_brief_reuse` | 简报/索引复用命中 |

## 13. 验收标准

机读锚点见 `docs/acceptance/acceptance-criteria.yaml`
（REQ-230～REQ-235）。摘要：

1. 长文档：全文覆盖或显式 `partial` + 未读区间。
2. 长文档：超过单次上下文时仍分批抽取并合并，不把整篇塞进一次调用。
3. 写作：每节按证据检索，引用锚点可校验。
4. 架构：`ArchIndex` 含 import 边，`ArchBrief` 可被后续 worker 复用。
5. 跨模块：自动接口优先波次 + gate；gate 失败只影响该波次。
6. 安全：知识产物访问受 run 作用域与路径守卫；远程 embedding 需显式开启。

## 14. 替代方案与代价

| 方案 | 优点 | 代价 / 风险 | 为什么不选 |
|---|---|---|---|
| 只调大上下文/提示词 | 改动最小 | 长文档仍超限；成本高；覆盖不可证 | 效果不可控 |
| 继续用 15 页 + 12k 汇总 | 成本低 | 长文档静默漏读；报告不可信 | 与“效果优先”冲突 |
| 引入外部 RAG 服务 | 检索能力强 | 数据出本机；运维/合规成本 | 默认不允许 |
| 一次性重写调度器 | 结构最干净 | 破坏已验证的并发/恢复语义 | 风险过高 |
| 只加固定模板（不动态） | 实现简单 | 复杂任务仍要人挑模板 | 不满足 G5 |
| **本方案（产物层 + 专用图 + 复用 scheduler）** | 效果/风险平衡 | 新增产物与评测工作量 | 推荐 |

## 15. 任务分解与验证计划

### P0：效果优先（不改安全边界）

| 任务 | 模块 | 验证 |
|---|---|---|
| P0-1 全文覆盖 + 覆盖报告 | `document_workers.py`、`config.py` | `tests/unit/test_document_workers.py` |
| P0-2 map-reduce 抽取 | `document_workers.py` | `tests/unit/test_document_workers.py` |
| P0-3 证据检索写作 | `document_workers.py`、`knowledge_*` 工具 | `tests/unit/test_document_workers.py` |
| P0-4 ArchIndex/Brief | `codeintel/python_index.py`、`orchestrator/architecture.py` | `test_arch_index.py`、`test_arch_brief.py` |
| P0-5 影响分析 + 契约 | `orchestrator/impact.py` | `test_impact_analysis.py` |
| P0-6 接口优先波次 | `orchestrator/plan.py`、`scheduler_run.py` | `test_interface_first_waves.py` |

退出条件：默认关闭；开启后长文档覆盖可证、跨模块改动可在 2+ 波次内完成，
现有快反馈测试全绿。

### P1：动态图与验证

| 任务 | 模块 | 验证 |
|---|---|---|
| P1-1 动态图构建器 | `orchestrator/workflow_builder.py` | `test_workflow_builder.py` |
| P1-2 groundedness/覆盖校验 | `document_workers.py`、`tools/report.py` | `test_groundedness.py` |
| P1-3 架构 delta 与 review | `orchestrator/architecture.py` | `test_arch_delta.py` |
| P1-4 事件/指标 | `events.py`、`runtime/event_map.py` | 契约漂移 + 事件覆盖测试 |

### P2：成本与增量

- 增量更新 DocIndex/ArchIndex（按 `git_head`/文件哈希）；
- 远程 embedding（opt-in）与缓存;
- 大规模仓库的抽样/分层索引。

## 16. 风险与技术债

| 风险 | 缓解 |
|---|---|
| 产物明文含敏感文档 | `.agent` 权限 + TTL + 不进版本控制 + 脱敏 |
| map-reduce 成本上升 | 便宜模型 + 有界并发 + 只对长文档启用 |
| 架构索引过期 | `git_head` + 文件哈希；过期即重建 |
| 动态图引入不确定性 | 信号不足回退四模板；`validate_tasks` fail-closed |
| 波次过多拖长耗时 | 波次上限 + deadline + 预算；耗时排第三 |

## 17. 决策与后果

- 最终选择：批准，按 P0 分段落地。
- 批准人 / 日期：项目作者，2026-10-31。
- 已落地（2026-10-31）：
  - 长文档全文覆盖 + 覆盖报告（`WorkerResult.coverage`），默认仍 15 页但不再静默；
  - 文档 map-reduce 分批抽取与合并（`AGENT_DOCUMENT_MAP_REDUCE`）；
  - author 证据上下文（claim + quote + anchor，deep 模式下 24k/12 条）；
  - `ArchIndex` / `arch_brief` / `impact_analysis`，接入 survey 与规划提示；
  - `KnowledgeStore` 持久化 `DocIndex`/`ClaimStore`/`ArchIndex`/`ArchBrief`；
  - `knowledge_read` / `knowledge_search` 只读工具（run 作用域 + artifact_id 安全化）；
  - `ChangeContract` 与接口优先顺序波次 + 波次 gate（ADR-0031）；
  - `architecture_change` 动态图构建器，信号不足回退四模板（ADR-0032）；
  - 向量/hybrid 检索，复用 memory embedding provider 与远程门禁（ADR-0033）；
  - `arch` 评测套件、文档覆盖/引用指标与手动评测脚本（ADR-0034）；
  - 增量 Python 索引、多语言/规模抽样索引、文档答案确定性 + LLM judge（ADR-0035）；
  - 跨语言调用图、内容寻址的跨机器索引缓存（ADR-0036）；
  - 动态/反射/宏调用识别与 `fallback`、缓存 TTL/LRU 淘汰（ADR-0036）；
  - judge 阈值校准、24 条种子标注集、大规模构建/校验/切分工作流（ADR-0037）；
  - 宏定义→调用边、可选 LSP definition 动态解析、缓存跨进程锁（ADR-0036）。
- 未落地（非目标/后续）：编译器级宏展开、LSP 调用层级/类型推断、
  强一致分布式锁、真实超大规模人工标注。
- 关联 ADR：[ADR-0029](../adr/0029-knowledge-artifacts.md)、
  [ADR-0030](../adr/0030-deep-document-flow.md)。
