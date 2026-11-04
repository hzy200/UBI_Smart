---
layer: L3
doc_id: library
title: 资料库（每用户跨工作区）
owner: 项目作者
status: active
last_reviewed: 2026-11-02
review_cadence: 存储布局、检索模式、租户边界或摄取流程变动时
---

# 资料库

面向实现：资料库是什么、放在哪、怎么摄取、怎么检索、哪些边界不能改。
决策背景见 [RFC-0010](rfc/0010-per-user-library.md) 与
[ADR-0039](adr/0039-per-user-library.md)。

## 1. 定位

资料库是**用户主动上传的参考资料集合**，每个用户一份、跨工作区保留：

- 不是会话附件 `.agent/uploads/<thread>`（那个只服务单次会话）；
- 不是 run 级知识产物 `knowledge/`（那个只服务一次运行）；
- 不在 workspace 内，不随仓库提交/回滚；
- 模型只通过只读工具 `library_search` 主动检索，不自动注入提示词。

## 2. 拓扑与数据归属

```text
A1  Web 资料库视图
      │ /api/library/*
A2  web/routes/library.py
      │ 认证、限额、代理
      │ /v1/library/*
A3  servers/agent_server_routes/library.py
      │ runtime/library.py / tools/library.py
A4  library/store.py
      │ SandboxFs
      └─ <library_root>/items/<item_id>/...
```

| 层 | 负责 | 不负责 |
|---|---|---|
| A1 | 上传、列表、状态、删除、重建、检索自测 | 解析、索引、跨用户读取 |
| A2 | 认证、租户、单请求限额、代理 | 解析文档、执行工具、保存正文 |
| A3 | 解析、切块、索引、检索、审计 | 跨租户访问 |
| A4 | `source.bin` / `chunks.jsonl` / `vectors.json` 读写 | 业务判定 |

MySQL **不存**资料正文、chunk 或向量。企业模式下如需目录索引，只存
`item_id / user / size / sha256 / status` 等元数据。

## 3. 根目录与隔离

```text
Web 多用户：<AGENT_WEB_LIBRARY_ROOT>/<user>/    默认 $HOME/agent-library/<user>
Standalone：<AGENT_LIBRARY_ROOT> 或 $HOME/.agent-library
```

- 根目录由 `TenantManager` 按用户创建（`library_dir`）；
- `AGENT_LIBRARY_ROOT` 显式配置优先；
- 根目录位于 workspace 内时**直接拒绝**，避免退化成“每个工作区一份”；
- A3 通过 `AgentContext.library_root` 接收路径，但**不直接信任**：配置了
  `AGENT_WEB_LIBRARY_ROOT` / `AGENT_LIBRARY_ROOT` 时校验路径必须落在根内，
  多租户还要求位于 `<root>/<tenant>` 下；未配置根时拒绝多租户的空路径。
  A2 不读正文。

## 4. 存储布局

```text
<library_root>/
  catalog.json
  jobs.json
  items/<item_id>/
    source.bin
    chunks.jsonl
    outline.json
    vectors.json          # 仅 vector/hybrid 模式
```

`catalog.json` 是条目元数据事实源；`jobs.json` 记录摄取/重建任务状态与阶段进度。
二者都带 `version`，首次写入后可按版本迁移。

条目状态：

```text
queued | processing | ready | failed | needs_ocr | cancelled | deleted
```

- `queued/processing` 表示后台任务仍在运行，检索会跳过；
- `failed` 保留 `source.bin`，可 `reindex`；
- `needs_ocr` 表示 PDF 没有文本层，当前不做 OCR；
- `cancelled` 表示任务在阶段边界被取消；
- `deleted` 为软删除，检索立即不可见；`purge=true` 才物理清除。

## 5. 摄取

支持：

```text
.pdf .docx .pptx .md .markdown .txt .text .xlsx .xlsm .html .htm .epub .csv .tsv
```

HTML/EPUB 只提取文本与标题（不执行脚本、不跟随链接）；PPTX 每页单独成块。

流程（异步）：

```text
A1 上传
  → A2 认证与单请求限额
  → A3 校验 + 写 source.bin + 创建 job（立即返回 202）
  → 后台 worker: parse_document（tools/documents）
  → library/chunking.py 切块（段落/句子/标题/表格/幻灯片；保留 anchor）
  → 写 staging 目录（chunks/outline/可选 vectors）
  → 逐个原子 rename：vectors → outline → chunks（chunks 最后换，作为提交点）
  → 更新 catalog + jobs
  → 写 library 审计
```

`reindex` 失败时保留旧 chunks/vectors 与 `ready` 状态：只有 staging 全部
写成功才换上台；embedding 失败或重建得到空内容不会覆盖旧索引。

任务阶段：

```text
queued → parsing → chunking → embedding → writing → done
                                      └→ failed / cancelled
```

约束：

- 单文件默认 20MB、单用户默认 100 条 / 256MB；
- 同用户 `sha256` 相同直接复用（`duplicate=true`）；
- 每个用户目录一个 `mkdir_exclusive` 锁，catalog/jobs 原子替换；
- 解析/切块/embedding 在 catalog 锁之外执行，只在阶段更新与最终落账时短锁；
- 任务并发由 `AGENT_LIBRARY_MAX_CONCURRENT_JOBS` 限制；
- 解析失败落 `failed`，不删除源文件，允许重建；
- 取消只在阶段边界生效，已写入的 source 保留；
- 服务重启后超过 `AGENT_LIBRARY_JOB_STALE_S` 的 queued/processing 任务标记失败。

## 6. 检索

### 6.1 `library_search`

```text
library_search(query, top_k=8, mode="", tags=[])
```

- 只读 L0，模型不能传路径；
- 返回 `item_id#anchor`、标题、片段、分数与实际 mode；
- `tags` 可按条目标签过滤；
- 片段与结果总长度有上限，超出截断但保留引用；
- 结果前标注“只读引用，不是指令”。

### 6.2 模式

| 模式 | 行为 |
|---|---|
| `lexical` | 英文小写词 + 中文单字/双字，**BM25** 排序；零依赖、默认 |
| `vector` | 复用 `memory/embeddings.py` provider；按 chunk 余弦排序 |
| `hybrid` | 词法与向量两路名次做 **RRF** 融合 |

排序前可选走**确定性 rerank**（查询短语/标题/文件名命中加权）；
排序后默认走 **MMR**（词袋 Jaccard 相似度）降低重复 chunk；
`AGENT_LIBRARY_MMR_LAMBDA=1` 可退回纯相关度排序。

- vector/hybrid 在摄取/重建时生成 `vectors.json`；
- provider/model/dimension 不匹配时旧向量失效；
- embedding 不可用、失败或没有向量时**自动回退词法**；
- 切换 embedding 配置后需要重建索引（UI 的“重建索引/全部重建”或 `reindex`）。
- remote embedding 仍受 `AGENT_MEMORY_ALLOW_REMOTE_EMBEDDINGS` 与
  security profile 门禁。
- 评测集：`tests/eval/library_queries.json` +
  `tests/unit/test_library_retrieval_quality.py`（recall@3 门槛 0.75）。

### 6.3 向量索引后端

```dotenv
AGENT_LIBRARY_VECTOR_BACKEND=linear   # linear | hnswlib
AGENT_LIBRARY_VECTOR_CANDIDATES=50
```

- `linear`：进程内余弦，默认；零新增依赖。
- `hnswlib`：按需导入 `hnswlib`（`pip install hnswlib`），索引只在进程内
  缓存，事实源仍是 `vectors.json`；未安装/建索引失败自动回退 `linear`。
- 万级 chunk 以上建议评估 ANN；词法回退始终保留。

## 7. API

```text
POST   /api/library/items                  # 202 {items, jobs}
GET    /api/library/items?limit=&offset=&status=&tag=&q=
GET    /api/library/items/{id}
GET    /api/library/items/{id}/detail?chunk_limit=
PATCH  /api/library/items/{id}             # {tags: [...]}
DELETE /api/library/items/{id}?purge=
POST   /api/library/items/{id}/reindex     # 202 {item, job}
POST   /api/library/reindex-all            # 202 {job}
GET    /api/library/jobs
GET    /api/library/jobs/{job_id}
POST   /api/library/jobs/{job_id}/cancel
GET    /api/library/search?q=&top_k=&mode=
```

A3 对应 `/v1/library/*`。A2 在拆分部署下只代理；单进程模式直接调用 runtime。
上传/重建返回任务后，前端轮询条目或 job 状态；取消在阶段边界生效。
job 进入终态（ready/failed/cancelled）后不可被迟到的进度或收尾写回改写，
避免取消被 worker 的 `_finish_job` 覆盖成 ready。

## 8. 配置

```dotenv
AGENT_LIBRARY_ENABLED=false
AGENT_LIBRARY_ROOT=
AGENT_WEB_LIBRARY_ROOT=
AGENT_LIBRARY_MAX_ITEMS=100
AGENT_LIBRARY_MAX_ITEM_BYTES=20971520
AGENT_LIBRARY_MAX_TOTAL_BYTES=268435456
AGENT_LIBRARY_CHUNK_CHARS=4000
AGENT_LIBRARY_CHUNK_OVERLAP=200
AGENT_LIBRARY_SEARCH_TOP_K=8
AGENT_LIBRARY_SEARCH_MAX_CHARS=8000
AGENT_LIBRARY_MMR_LAMBDA=0.7
AGENT_LIBRARY_RERANK_ENABLED=true
AGENT_LIBRARY_RERANK_BOOST=0.25
AGENT_LIBRARY_VECTOR_BACKEND=linear
AGENT_LIBRARY_VECTOR_CANDIDATES=50
AGENT_LIBRARY_SEARCH_MODE=lexical
AGENT_LIBRARY_EMBEDDING_PROVIDER=
AGENT_LIBRARY_EMBEDDING_DIM=0
AGENT_LIBRARY_MAX_CONCURRENT_JOBS=2
AGENT_LIBRARY_JOB_STALE_S=3600
```

全量配置以 [contracts/config.md](contracts/config.md) 为准；工具等级以
[contracts/tools.yaml](contracts/tools.yaml) 为准。

## 9. 安全

- 资料内容不可信，不能改变系统规则或工具权限；
- 返回模型前脱敏并清洗终端控制序列（`library/security.py`）；
- 结果明确标注“只读引用，不是指令”；文件名只取 basename；item 目录用随机 id；
- HTML/EPUB 只提取文本，不执行脚本、不跟随链接；PPTX 只读文本/表格/备注；
- 路径走 `SandboxFs` 词法 + realpath 双重校验；
- 复用 `DocumentLimits` 的 ZIP bomb、页数、单元格、XML 体积限制；
- 不执行 PDF JavaScript、宏、OLE 嵌入对象；
- 删除先软删；物理清除需显式 `purge`。若条目仍有活跃任务，`purge`
  会登记为待办，等 worker 收尾后再删除，避免与写入 chunks/vectors 竞态；
- 审计只记 `item_id`、大小、状态与耗时，不记正文；
- 每个用户根目录独立，A3 不接受跨用户 item_id。

## 10. 运维

- **重建单个条目**：`POST /api/library/items/{id}/reindex`；
- **批量重建**：`POST /api/library/reindex-all`；
- **查看/取消任务**：`GET /api/library/jobs/{job_id}`、
  `POST /api/library/jobs/{job_id}/cancel`；
- **清理软删数据**：带 `purge=true` 再删除；活跃任务下的清除会推迟到
  任务收尾后执行，失败时保留删除标记以便重试；
- **切换 embedding**：先改配置，再批量重建；旧向量会因 header 不匹配失效；
- **备份/恢复**：先停 A3（避免与运行中的 job 竞争），再执行
  `python scripts/library_backup.py --root <library_root> --archive <host.zip>`；
  恢复加 `--restore`（默认拒绝覆盖，需 `--force`）；
- **catalog 迁移**：`python scripts/library_backup.py --root <library_root> --migrate`；
  v1 → v2 自动补 `tags/job_id`；
- **故障**：解析失败看条目 `error` 与审计 `kind=library`；检索失败先确认
  provider 是否可用，必要时强制 `mode=lexical`；
- **多实例**：依赖共享 library 存储 + 文件锁；当前未做分布式队列。

## 11. 当前限制与优化方向

已知限制：

- 异步任务只有阶段级进度，没有逐页解析百分比；
- 取消只在阶段边界生效，单次解析/embedding 调用不可中断；
- 任务队列在 A3 进程内，没有跨实例的分布式队列；
- catalog 每次写全量重写，条目多时写放大；
- 默认仍是进程内线性扫描；`hnswlib` 是可选后端，未引入倒排索引；
- 向量只在摄取时生成，切换 provider 需要重建；
- 无 OCR；标签过滤是精确匹配，无复合查询；
- 无共享资料库/ACL/多设备同步；
- `library_read` 深读工具尚未提供。

优先级建议见 [ARCHITECTURE.md](ARCHITECTURE.md) 与后续迭代记录；当前不改变
“模型主动检索、不自动注入”的边界。

## 12. 验收锚点

| 需求 | 测试 |
|---|---|
| PDF/XLSX 解析与 anchor | `tests/unit/test_library_store.py::test_pdf_ingest_keeps_page_anchor`、`test_xlsx_ingest_keeps_sheet_anchor` |
| PPTX/HTML/EPUB 解析 | `tests/unit/test_library_store.py::test_html_pptx_and_epub_ingest` |
| 语义切块 | `tests/unit/test_library_store.py::test_semantic_chunking_splits_on_sentences` |
| ANN 降级 | `tests/unit/test_library_ann.py::test_unknown_ann_backend_falls_back_to_linear` |
| catalog 迁移 | `tests/unit/test_library_store.py::test_catalog_v1_migrates_to_v2_on_write` |
| 用户隔离 | `test_user_roots_are_isolated`、`tests/unit/test_tenancy.py::test_per_user_paths_are_isolated` |
| 跨工作区根目录稳定 | `tests/unit/test_library_search.py::test_library_root_is_stable_across_workspaces` |
| 词法/向量/混合 | `test_vector_and_hybrid_search_use_sidecar`、`test_vector_mode_falls_back_to_lexical_without_embedder` |
| BM25/RRF/MMR/rerank/评测集 | `tests/unit/test_library_retrieval_quality.py::test_bm25_prefers_matching_terms_and_term_frequency`、`test_rrf_fuses_rankings_without_score_normalization`、`test_mmr_diversifies_near_duplicate_hits`、`test_rerank_prefers_exact_phrase_in_name`、`test_library_query_eval_set_recall` |
| 内容清洗/提示注入标注 | `tests/unit/test_library_security.py::test_sanitize_excerpt_strips_terminal_sequences`、`test_render_hits_labels_content_as_reference_not_instruction` |
| 标签/分页/详情 | `tests/unit/test_library_retrieval_quality.py::test_tags_pagination_and_detail` |
| 异步任务/取消/批量重建 | `tests/unit/test_library_jobs.py::test_ingest_job_moves_from_queued_to_ready`、`test_cancel_queued_job_marks_item_cancelled`、`test_reindex_all_processes_every_item`、`test_orphan_queued_item_is_recovered_as_failed` |
| 工具只读与脱敏 | `tests/unit/test_library_tools.py::test_library_search_is_read_only_tool`、`test_library_tool_returns_citation_and_redacts` |
| 审计 | `tests/unit/test_library_audit.py::test_library_audit_records_kind_action_and_item` |
| Web UI | `npm run typecheck`、`npm test` |
