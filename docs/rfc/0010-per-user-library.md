---
layer: L2
doc_id: rfc-0010
title: 每用户跨工作区资料库
owner: 项目作者
status: implemented
created: 2026-11-02
last_reviewed: 2026-11-02
review_cadence: 作用域、存储边界或安全模型变化时
---

# RFC-0010：每用户跨工作区资料库

> 让用户在 Web 多用户环境中维护一份跨工作区可复用的参考资料库，
> 由模型按需调用 `library_search` 检索，而不是把文档塞进每次提示词。

## 1. 目标

- 每个用户一份资料库，跨工作区保留；
- 支持 PDF / DOCX / Markdown / TXT / XLSX / CSV；
- 上传后解析为带 anchor 的 chunk，写入 `catalog.json`；
- 只暴露 `library_search` 只读工具，模型主动调用；
- 检索支持 lexical/vector/hybrid，向量失败回退词法；
- 用户之间严格隔离；团队能力先体现为多用户隔离，不做共享。

## 2. 非目标

- 多人/多设备共享资料库、ACL、团队资料库；
- 自动把检索结果注入 system prompt；
- OCR、扫描件识别；
- 读取或索引 `host` 工作区文件；
- `library_read` 深读工具。

## 3. 背景与证据

现有两条路径都不适合复用：

- `.agent/uploads/<thread>` 是会话附件，不跨会话、不建索引；
- `knowledge/` 是 run 作用域的代码/文档产物，run 结束即失去上下文。

资料库需要独立生命周期、独立根目录与独立配额，但可以复用
`tools/documents` 的解析能力、`SandboxFs` 的路径守卫与 `knowledge/`
的分词/打分思路。

## 4. 受影响面

| 项 | 内容 |
|---|---|
| 模块 | 新增 `library/`、`tools/library.py`、`runtime/library.py`、A2/A3 路由 |
| 契约 | 新增 `AGENT_LIBRARY_*` 配置、`library_search` 工具等级、`library` 审计 kind |
| 数据 | 正文与索引落 A4 的 `<library_root>/users/<user>/`；MySQL 不存正文 |
| 前端 | 本期只提供 REST API；Web 页面后续补 |
| 权限 | 只读工具 L0；上传/删除/重建走 A2 认证与 A3 执行 |

## 5. 安全与隐私

- 资料库内容是不可信引用，不是系统指令；工具输出明确标注；
- 结果返回前走 `security/secrets.redact_secrets`；
- 每个用户根目录独立，A3 不接受跨用户 item_id；
- 文件名只取 basename，item 目录用随机 id，路径走 `SandboxFs` 双重校验；
- 复用 `DocumentLimits` 的 ZIP bomb、页数、单元格与 XML 体积限制；
- 删除先软删，物理清除需显式 `purge`；
- 审计只记 item_id、大小、状态与耗时，不记正文。

## 6. 兼容与回滚

- `AGENT_LIBRARY_ENABLED=false` 时行为完全不变；
- 关闭开关不删除既有数据，重新开启后仍可检索；
- 无数据库迁移；目录格式带 `catalog.version`，后续可迁移；
- 回滚只需关闭开关或回退代码，已写入的目录不影响 workspace。

## 7. 可观测性

- 新增 `library` 审计 kind：`ingest` / `reindex` / `delete`；
- `library_search` 的调用由现有 `tool_call` 审计覆盖；
- 列表接口返回 `usage`（条目数、字节数）。

## 8. 验收标准

```text
Given 用户 A 开启资料库并上传一个 PDF 与一个 XLSX
When  A 在工作区 W1 调用 library_search 检索 PDF 文本与表格单元格
Then  返回带 page= / sheet= anchor 的片段，且 catalog 中两条均为 ready

Given 资料库以 vector/hybrid 模式建立索引
When  调用 library_search 且 embedding provider 不可用
Then  检索回退词法并返回结果，不因向量失败中断

Given 用户 B 与 A 在同一台服务器
When  B 调用 library_search
Then  检索不到 A 的条目

Given 用户 A 删除一个条目
When  再次检索
Then  该条目不再出现
```

## 9. 替代方案与代价

| 方案 | 优点 | 代价 / 风险 | 为什么没选 |
|---|---|---|---|
| 复用 `.agent/uploads` | 改动最小 | 不跨会话、不索引、无 anchor | 不满足“资料库”语义 |
| 扩展 `knowledge/` | 复用检索 | run 生命周期与资料库重叠，开关耦合 | 生命周期与信任模型不同 |
| 纯客户端资料库 | 正文不出本机 | Web A3 读不到；sandbox 工作区不可用 | 与本期部署目标冲突 |
| MySQL 存正文/chunk | 查询方便 | 违反控制面数据边界，迁移与备份成本高 | 正文属于执行面 |

## 10. 决策与后果

- 最终选择：服务器侧、每用户、文件式 catalog + JSONL chunk；
- 批准人 / 日期：项目作者 / 2026-11-02；
- 已落地：PDF/DOCX/Markdown/TXT/XLSX/CSV 摄取、anchor 切块、catalog、
  BM25 词法、RRF 混合、确定性 rerank、MMR 去重、`library_search`、A2/A3 API、
  Web 视图、审计与隔离测试；标签/分页/详情、内容清洗与小型评测集也已接入；
  P2 已补 PPTX/HTML/EPUB、语义切块、可选 hnswlib、指标与备份/迁移脚本；
- 异步摄取已落地：上传立即返回 job，后台完成解析/切块/embedding，
  支持阶段进度、取消与批量重建；
- 真实结果与预期差异：
  - 异步任务只做阶段级进度，不是逐页百分比；
  - 任务队列在 A3 进程内，尚无跨实例分布式队列；
  - catalog 为整文件重写，尚未做分片/数据库化；
  - 切换 embedding provider 后需要显式重建索引；
  - 尚无 OCR、共享资料库、多设备同步与 `library_read`；
- 关联 ADR：[ADR-0039](../adr/0039-per-user-library.md)。

## 11. 任务分解与验证计划

| 任务 | 负责模块 | 验证方式 |
|---|---|---|
| 配置与租户根目录 | `config.py`、`identity/tenancy.py` | `tests/unit/test_tenancy.py`、配置契约 |
| 解析/切块/存储 | `library/` | `tests/unit/test_library_store.py` |
| 词法检索与引用 | `library/search.py`、`tools/library.py`、`approve.py` | `tests/unit/test_library_search.py`、`tests/unit/test_library_tools.py` |
| BM25/RRF/MMR 与评测集 | `library/search.py`、`library/store.py` | `tests/unit/test_library_retrieval_quality.py`、`tests/eval/library_queries.json` |
| 向量/混合检索 | `library/embedding.py`、`library/store.py` | `tests/unit/test_library_store.py` |
| 标签/分页/详情 | `library/store.py`、A2/A3 routes、Web | `tests/unit/test_library_retrieval_quality.py`、`test_library_api.py` |
| 异步任务/取消/批量重建 | `library/store.py`、A2/A3 routes | `tests/unit/test_library_jobs.py` |
| A2/A3 API | `web/routes/library.py`、`servers/...` | `tests/unit/test_library_api.py` |
| Web 资料库页面 | `web/frontend/src/features/library.ts`、`sidebar.html` | `npm run typecheck`、`npm test` |
| 文档与契约 | `docs/`、`scripts/gen_contracts.py` | `python scripts/check_docs.py` |
