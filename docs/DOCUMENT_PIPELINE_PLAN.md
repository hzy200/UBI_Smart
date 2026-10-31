---
layer: L3
doc_id: document-pipeline-plan
title: 文档管线落地实施计划（RFC-0005 配套）
owner: 项目作者
status: implemented
last_reviewed: 2026-10-31
review_cadence: 阶段切换或范围变化时
---

# 文档管线落地实施计划（RFC-0005 配套）

> 本文件是 [RFC-0005](rfc/0005-document-image-and-report-pipeline.md) 的**实施计划**，
> 不替代它的设计结论；决策依据见 [ADR-0024](adr/0024-document-pipeline-on-orchestrator.md)
> 与 [ADR-0025](adr/0025-single-report-format-and-system-fonts.md)。
> 所有行号对应 2026-10-30 的工作区快照，实现前需复核。
>
> **后续阶段**：长文档全文覆盖、map-reduce 抽取与按证据写作见
> [RFC-0009](rfc/0009-deep-workflows-and-knowledge-artifacts.md)。
> 2026-10-31 已部分落地：默认仍受“每次 5 页、最多 3 次”限制，但会报告
> `coverage=partial` 与未读区间；开启 `AGENT_DOCUMENT_FULL_COVERAGE` /
> `AGENT_DOCUMENT_MAP_REDUCE` 后按页数上限读全文并分批抽取。
>
> 2026-10-31 后续演进已完成：章节感知切分、引用跨度校验、每篇固定槽位
> 证据矩阵、树状归并、跨文档归属校验、`document → author → report_check`
> 报告评测。详见 §12 与 [ADR-0038](adr/0038-structured-document-matrix-and-attribution.md)。

## 实施状态（2026-10-30）

本计划的 P0–P5 已完成首轮实现，正文中的“零实现/待做”描述保留为历史背景。
最终落地与计划的差异：

- **D2 已由用户决策覆盖**：默认 LLM 直接视为多模态模型，不要求独立
  `vision` provider；端点不支持图片时明确报错。实现见 `llm/multimodal.py`
  与 `tools/vision.py`。
- **前端已引入 pdf.js/mammoth/SheetJS**：PDF 用 pdf.js canvas，DOCX 用
  mammoth + DOMPurify，XLSX/XLS 用 SheetJS；PPTX 由服务端 python-pptx 提取
  形状几何并生成 SVG 版式（不引入 LibreOffice）。
- author 全局单实例、写前快照/FileChanged 审计、report_check 自检与 review、
  角色级并发/优先级、按文档 checkpoint、pipeline 上限、SVG 源码展示、
  Playwright viewer 冒烟与多文档 E2E 已补齐。
- 扫描版 PDF 渲染（`pdf_page_image`）+ vision、引用锚点校验、模板参数、
  markdown-it DOCX、长 PDF 分段读取、角色硬预算、ETag/页数/缓存命中、
  scheduler 级 E2E 与取消测试已补齐。
- PDF SVG 文本层/翻页/缩放、XLSX sheet 标签、DOCX/PPTX 内嵌图片提取、
  ZIP/XXE 攻击测试与 runtime 上传清单测试已补齐。
- PDF 默认只处理文本型：元数据/书签/注释/表单/附件/pdfplumber 表格；
  扫描页默认跳过，`AGENT_DOCUMENT_OCR_ENABLED` 可显式开启视觉链路。
- 显式多格式报告已支持：多个 author 任务串行执行（最多 3 个）。
- PDF viewer 已展示元数据与书签大纲，搜索命中标黄，连续滚动按批懒加载；
  `.agent/documents` 增加容量/TTL 清理；`AGENT_REPORT_LLM_REVIEW` 可选启用
  LLM 证据审查；补超大 XML 与图片炸弹测试；PDF 附件支持递归解析一层正文，
  PPTX 内嵌图片已在前端展示。
- PPTX 版式（`slide_layouts` → `.slide-svg`）、XLSX 公式栏与 CSS 行虚拟化、
  DOCX 页眉/页脚/分节/脚注/批注（`.docx-extras`）已补齐；
  宏/OLE/PDF JavaScript/嵌套 ZIP 攻击样例与 `scripts/bench_documents.py`
  压测脚本已加入并接入 CI。`document_edit`（L1）已提供纯 Python 受限编辑：
  只写新路径、拒绝宏文档与跨格式转换，受 write_scope/字节上限约束。
  收尾完成：依赖上下文 12k 总上限、`report_check` 必需章节校验、
  XLSX 真正行窗口化、viewer 拆分到 `features/viewers/`。
- **上传清单**：`survey` 扫描本会话上传目录；runtime 也会在提示词中追加
  有界的附件元数据清单（REQ-221）。
- **解析缓存**：`document_read` 使用进程内 LRU +
  `.agent/documents/<sha256>/extract.json`，可用 `AGENT_DOCUMENT_CACHE` 关闭。
- 回滚开关 `AGENT_DOCUMENT_PIPELINE_ENABLED` 已落地。

验收锚点见 `docs/acceptance/acceptance-criteria.yaml` 的 REQ-214～REQ-220。

## 0. 需求映射与现状

| 需求 | RFC-0005 对应目标 | 现状（已核实，零实现） |
|---|---|---|
| 1. 前端文档阅读器 + 代码高亮 | G1 / G2 | 只有 `inspector.ts:783 openFile` 的文本预览路；二进制一律落到“（二进制文件，不展示内容）”；md 在文件页是纯文本、不渲染；高亮覆盖 36 种语言 |
| 2. Agent 阅读二进制文档 | G3 | `sandbox/fs.py:227-239` 见 NUL 直接拒绝；`pyproject.toml` 无任何解析库；`FileAccess` 协议（`execution/file_access.py:29-52`）只有 `read_text` |
| 3. 图像理解工作流 | G4 | 全仓无 `image_url` / vision / OCR；`read_bytes` 已存在于 `sandbox/fs.py:179`、`host_file_access.py:99` |
| 4. 提炼要点 + 撰写文档 | G5 / G6 / G7 | 无报告概念；上传文件不进提示词；orchestrator 角色只有 explore/test/review/implement |

## 1. 三个必须先定的决策（阻塞 P2 之后的实施）

### D1：`.doc` / `.ppt`（OLE 旧格式）——用户需求与 RFC 冲突

用户列出的格式包含 `Doc` / `PPT`，RFC-0005 §2 明确“不支持、不引入 LibreOffice”。
两者只能选一条路：

| 选项 | 做法 | 代价 |
|---|---|---|
| **A（推荐，与 RFC 一致）** | 前端与 `document_read` 均返回“请先另存为 docx/pptx”，附 operating hint | 用户需手动转换一次；零新增执行面 |
| B 宿主转换器 | `host_broker/toolchain.py` 注册本机 LibreOffice，`host_ops.py:20-52 HOST_KINDS` 加 `"doc_convert"` | 引入外部进程 + 安装依赖 + 安全面扩大；与 ADR-0024“解析在宿主进程、不启外部进程”冲突，需新 RFC 或改 ADR |
| C 尽力解析 | 纯 Python 读 OLE（`olefile`）取文本流 | 保真度低（丢失表格/图片/顺序），易变成“看起来能读、实际错漏”，与项目 fail-closed 风格冲突 |

**推荐 A**：先上线 A，把 B 登记为技术债；确实高频再走 RFC 变更。

### D2：默认 LLM 不是多模态模型

RFC §4.5 假定“默认 LLM 本身是多模态”，但当前默认 `deepseek_model = "deepseek-chat"`
（`config.py:95`）**不支持图像输入**。所幸 `capabilities` 路由已实现却从未使用：

- `llm/deepseek.py:160-179` 支持 `required_capabilities`，且空 `capabilities` 视为“无所不能”（向后兼容）；
- `llm/router.py:170-189 _supports` 用子集语义过滤；
- `identity/provider_profiles.py:19-31` 的 `capabilities` 是自由 `list[str]`，**加 `"vision"` 不需要改 schema**。

方案：在 `AGENT_LLM_PROVIDERS` 里登记一条双模态 provider 并声明 `"capabilities": ["vision"]`，
`image_inspect` / `vision` worker 用 `build_llm(..., task_type="vision", required_capabilities=("vision",))`
单独建实例，主链路 LLM 不变。**无 vision provider 时必须显式报错**（RFC G4），禁止静默降级。

> 副作用：主路径 `build_llm` 已声明 `required_capabilities=("tools","chat")`
> （`graph/build.py:255-264`），因此**任何**显式声明了 capabilities 的 provider
> 都必须同时写 `tools` 和 `chat`，否则主链路会被过滤掉。需在配置文档里写明。

### D3：上传文件当前不进提示词（需求 2/3/4 的前置缺口）

`runtime_snapshots.py:251-291 store_upload` 把文件写进 `.agent/uploads/<thread>/`，
前端只在 `features/uploads.ts:52-53` 往本地 transcript 塞一条 system 文本。
**Agent 完全不知道用户上传了什么**——非补不可，否则整个管线无从触发。

建议双通道都做：
1. `runtime/core.py:589-594`：`HumanMessage(content=prompt)` 前拼一段有界附件清单（路径 + 大小 + 类型），同时作为 `"request"` 的一部分下游可见；
2. `orchestrator/survey.py:22-104`：由 runtime 显式传入本会话上传目录，产出文档/图片清单作为任务图输入（禁止扫描其他会话目录）。

## 2. 阶段划分

```text
P0 地基（FileAccess.read_bytes / 配置 / 依赖 extra / 上传清单）
 └ P1 前端阅读器 ← 可独立交付，用户最快感知
    └ P2 document_read（Agent 读二进制文档）
       └ P3 图像理解
          └ P4 提炼 + 报告撰写（orchestrator 任务图）
             └ P5 契约、文档、测试收口
```

P1 与 P2 共享 `/v1/file/raw`、`/v1/file/document` 端点，建议串行；P3 可并行开工。

---

## 3. P0 地基（建议先做，约 1 天）

| # | 改动 | 位置 |
|---|---|---|
| 0.1 | `FileAccess` 协议补 `read_bytes(path, *, max_bytes) -> bytes`；`SandboxFileAccess` 委托 `SandboxFs.read_bytes`（已存在）；`HostFileAccess` 已有 | `execution/file_access.py:29-52` |
| 0.2 | 新增 `DocumentError` 层级：`unsupported_format` / `needs_conversion` / `parse_failed` / `too_large` / `protected` / `dependency_missing`，全部可操作文案 | 新文件 `tools/documents/errors.py` |
| 0.3 | 新增配置，全部默认关/兜底：`AGENT_DOCUMENT_PIPELINE_ENABLED`、`AGENT_DOCUMENT_MAX_BYTES`(20MB)、`AGENT_DOCUMENT_CACHE_DIR`、Web read hooks `AGENT_DOCUMENT_*`、视觉 `AGENT_VISION_MAX_BYTES`/`AGENT_VISION_MAX_EDGE`、报告 `AGENT_REPORT_OUTPUT_DIR`/`AGENT_REPORT_FONT_PATH` | `config.py`（放在 `web_max_upload_*` 附近，`:897-908`） |
| 0.4 | `pyproject.toml` 新增 optional extras：`documents = [pypdf, python-docx, openpyxl, xlrd, python-pptx, Pillow, defusedxml]`、`report = [python-docx, markdown-it-py, reportlab]`；不进主依赖 | `pyproject.toml:28-73` |
| 0.5 | 上传清单注入 + survey 输入（见 D3） | `runtime/core.py:589-594`、`orchestrator/survey.py:22-104` |
| 0.6 | **不改 审批等级**，先只落地 `approve.py` 之外的地基 | — |

**验收**：`pytest -m "not wsl and not llm" tests/unit/test_file_access_bytes.py`（新）；
未安装 extra 时 `document_read` 返回 `dependency_missing` 且不影响既有会话。

---

## 4. P1 前端阅读器（需求 1）

### 4.1 后端：两个只读端点

新文件 `src/ubismart/workspace/file_preview.py`：

- `GET /api/file/raw?thread_id&path` —— 原始字节；复用 `workspace/browse.py:61-76 _resolve`
  路径守卫 + `security/read_policy.py:54 protected_read_reason`（**lexical 与 realpath 两处都要查**，
  照抄 `tools/files.py:217-246` 的双校验）；魔数嗅探 + 白名单放行 MIME；`nosniff`、支持 `Range`/`ETag`；上限 20MB。
- `GET /api/file/document?thread_id&path` —— 返回统一 `DocumentModel`（见 §4.2）。

同步：`web/routes/workspace.py:99-122`（现有 `/api/file`）、`servers/agent_server_routes/workspace.py:105`、
`agent_protocol.py` 的 `workspace` 能力加 `file_raw` / `file_document`；host 模式走 `HostFileAccess.read_bytes`。

> **注意**：`MAX_READ_BYTES = 256KB`（`browse.py:19`）不动，`/api/file` 保持原语义，
> 避免破坏现有 `test_frontend_workspace_ui.py` 的标记断言；新端点独立上限。

### 4.2 `DocumentModel`（前后端与 Agent 三方共用形状）

```jsonc
{
  "path": "docs/a.pdf", "kind": "pdf", "mime": "application/pdf",
  "size": 12345, "sha256": "…", "truncated": false,
  "meta": {"pages": 12, "sheets": ["Sheet1"], "slides": 8, "has_text_layer": true},
  "blocks": [
    {"anchor": "page=3", "type": "paragraph", "text": "…"},
    {"anchor": "sheet=Sheet1!A1:F20", "type": "table", "rows": [[…]]},
    {"anchor": "slide=2", "type": "bullets", "items": ["…"], "notes": "…"}
  ]
}
```

锚点是需求 4“可追溯”的根，**前端高亮、Agent 引用、 review 校验共用同一套字符串**。

### 4.3 前端 viewer 分派

新增 `web/frontend/src/features/viewers/`：`registry.ts` 按 MIME+扩展名分派；
`text.ts`（复用 hljs）、`markdown.ts`（marked + DOMPurify，渲染/源码切换）、
`pdf.ts`、`docx.ts`、`sheet.ts`、`slides.ts`（消费服务端结构化结果）、
`image.ts`、`unsupported.ts`（`.doc`/`.ppt` 提示转格式）。
`inspector.ts:783 openFile` 改为调 registry；`fileLanguage :873` 扩映射并加 `highlightAuto` 兜底。

**模板 summon 规则（踩过的坑）**：新 `<section>` 只能用 `data-action` + 零参 getter
（`features/csp.ts:11-65`），否则 `tests/unit/test_frontend_workspace_ui.py:905-929`
的 CSP 表达式 lint 直接红；新状态同时注册到 `state/session-view.ts:53-58 fileView`
与 `features/session-view.ts:18-92 SESSION_VIEW_FIELDS`；新 mixin 加到 `app.ts:285-320`。

### 4.4 构建与产物门禁（新增 vendor 必经五项）

1. `templates/footer.html:57-76` 加 `<script src="/static/vendor/x.min.js" integrity="sha384-…">`，同步更新 `static/vendor/README.md:9-12` 的 SHA-384 表；
2. `static/sw.js:8-22 SHELL` 登记，否则离线失效（`check_frontend_release.py:128-144`）；
3. 新全局类型在 `web/frontend/src/globals.d.ts` 声明；
4. `build_frontend_release.py:36-37 HASH_EXTS` 目前不含 `.wasm` —— **PDF.js v4 无 wasm，暂不需要**；若将来引入需扩 `HASH_EXTS`，否则“未哈希资源”报错；
5. CSP `middleware.py:58-71` 缺 `worker-src`：pdf.js worker **必须作为同源 `/static/vendor/pdf.worker.js` 提供**（`workerSrc` 指同源路径），**不能用 `blob:`**；改动需同步 `scripts/probe_frontend_csp.py:86-90` 的重复常量。
   若确需 `worker-src 'self'`，保持无 `unsafe-eval`（现有 AIpine CSP 构建前提）。

**体积**：`app.js` 当前 428KB 未压缩且**无体积预算**。pdfjs-dist ≈ 1MB 未压缩，
建议 viewer 脚本改为**按需动态插入 `<script src="/static/vendor/…">`**（`script-src 'self'` 允许同源动态加载），
避免拖慢首屏。

**hljs 现状**：vendor 版本只注册 36 种语言，`fileLanguage` 已映射到 `dockerfile` /
`powershell` 但库里没有 → 静默退回转义文本。扩语言需重打 hljs + 重算 SRI + README。

### 4.5 P1 验收

- 新增 `tests/unit/test_workspace_file_preview.py`（路径穿越、秘密文件、ZIP/XML 炸弹、大文件、MIME 白名单）；
- 前端 `tests/viewers.test.ts` 加入 `tests/all.test.ts`；`tests/dom/harness.ts:155-215` 补 `/api/file/raw|document` stub；
- `tests/e2e/a11y.spec.ts` 的 axe（`wcag2a/2aa`）必须仍为 `[]`；
- `python scripts/build_frontend.py` 重生成并提交 `static/app.js|index.html|style.css`
  （这些文件进 git，且被 `test_frontend_workspace_ui.py` 断言）。

---

## 5. P2 Agent 阅读二进制文档（需求 2）

### 5.1 工具

新包 `src/ubismart/tools/documents/`（`base.py` 锚点模型、`pdf.py`、`docx.py`、
`sheets.py`、`slides.py`、`textlike.py`、`cache.py`、`sniff.py`）。

```text
document_read(path, mode="auto", page_range="", sheet="", slide_range="",
              include_tables=True, include_images=False,
              offset=0, max_chars=20000)
```

返回 `[PDF p.3] …` / `[XLSX sheet=Sheet1 A1:F20] …` / `[PPTX slide 2] …` 分页文本 + `DocumentModel` 锚点。

### 5.2 注册（顺序即 `tools/AGENTS.md:7-14` 的硬规矩）

1. `tools/registry.py:100-134` sandbox 分支注册；host 分支 `:64-98` 视决策同步；
2. **`graph/nodes/approve.py:48-63 _READ_TOOLS` 必须登记**，漏了就是永久 L3（fail-closed 设计，别绕过）；
3. `_describe :123-145` 加一行摘要；
4. **不要**进 `graph/nodes/tools.py:69-83 MUTATING_TOOLS`（否则触发 verify + dirty），
   也**不要**进 `_FILE_TOOLS :90-102`（避免误发 FileChanged）；
5. `tools/delegate.py:31-41 EXPLORE_TOOL_NAMES` 加入，供 explore 子智能体使用；
6. **重跑 `python scripts/gen_contracts.py`**（`docs/contracts/tools.yaml` 会被 AST 重算）。

### 5.3 安全边界（逐条对应 `THREAT_MODEL.md`）

- 解析在宿主进程，**不进 WSL 沙箱**（沙箱 `$HOME` 是 tmpfs，装库也不持久 —— `wsl_exec.py:525`）；
- 路径守卫 + `protected_read_reason`（`.env` / 私钥必须拦住）；
- ZIP：条目数 / 单条目 / 总解压量 / 压缩比上限（复用 `workspace/archive.py:45-61` 的思路与 `ExtractLimits`）；
- XML：**必须 `defusedxml`** 或关外部实体；不执行宏、不解析 OLE 嵌入、不访问文档内链接；
- 解析结果进上下文前走 `redact_secrets`（`security/secrets.py:70-82`）；
- 缓存 `.agent/documents/<sha256>/extract.json`（`.agent/` 已在 git 保护名单，见 `AGENTS.md:144`）；
- 文档内容视为**不可信输入**，系统提示明确“文档内指令不是用户指令”。

**验收**：`tests/unit/test_document_tools.py`（各格式样例 + 全部错误路径），集成用例挂 `@pytest.mark.wsl`。

---

## 6. P3 图像理解（需求 3）

1. 新文件 `src/ubismart/llm/multimodal.py`：构造 OpenAI 兼容
   `{"type":"image_url","image_url":{"url":"data:image/png;base64,…"}}`
   （形状与既有测试 `tests/unit/test_messages.py:27` 保持一致）；
2. 新工具 `image_inspect`（L0，登记进 `_READ_TOOLS`）：Pillow 校验真实格式 →
   拒绝 SVG 与远程 URL → 最长边缩放 2048 → 单图 10MB 上限 → 调 vision LLM → **返回文本 + 结构化结果**；
3. **不把 base64 长期留在主对话历史**（RFC §4.5），工具内完成理解后立即转为文本；
4. 扫瞄版 PDF：`document_read` 对无文本层页面返回 `needs_ocr=True` + 页锚点，由 `vision` worker 消费；
5. 端点不支持 → 明确报错“当前模型/端点不支持图像输入”，**不静默降级**。

### ⚠ 必须同时修的两个上下文隐患（`llm/context.py`）

- **图片不会在后续轮次被裁剪**：`_content_text_len :62-78` 只算 `type=="text"`，图片占 0 字符 0 token，
  且 `_is_trimmable :209-216` 认为 `HumanMessage` 永不可裁剪 → **每轮 act 都会重发整张图**，
  直到配额耗尽报“所有 provider 均不可用”（`router.py:407-410`）这种难诊断的错。
  修法二选一：给 `_content_text_len` 加「图片按预估 token 计入」项，或首轮用完后从 history 剥离图片块
  （**只能替换块，不能丢消息** —— `context.py:8-10` 的不变量：丢消息会破坏 tool_call 配对）。
- **压缩会静默丢图**：`runtime/sessions.py:259-284` 用 `text_of()` 压平成字符串，
  compact 后图像全部消失。建议明确为「图片仅在当轮有效」并在 UI 提示，而非假装保留。

**验收**：`tests/unit/test_vision_tool.py`（格式拒绝、缩放、端点报错）、
`tests/unit/test_multimodal.py`（块形状）；真实 provider 的冒烟用例标 `@pytest.mark.llm`。

---

## 7. P4 提炼要点 + 报告撰写（需求 4）

按 ADR-0024，全部落在 orchestrator 子图，**不动主图、不改 `graph/build.py:105 estimate_recursion_limit`**。

| # | 改动 | 位置 |
|---|---|---|
| 7.1 | `TaskRole` 扩 `document`/`vision`/`author`；`WorkflowDecision.workflow` 加 `document_pipeline` | `orchestrator/contracts.py:14,57-60` |
| 7.2 | `rule_workflow` 的文档判定**放在 `file_count` 门槛之前**（`:82` 那条 `return direct` 会先返回，否则小仓库永远走不到） | `orchestrator/workflow.py:73-97` |
| 7.3 | 新 workflow 必须同步进 `route_after_select`，否则被静默忽略掉到 planner | `orchestrator/routing.py:12-18` |
| 7.4 | `TaskSpec` 增 `stage:int` 做阶段屏障，防 author 提前开写 | `orchestrator/contracts.py:18-47` |
| 7.5 | 依赖上下文注入：下游 worker 收到有界 `{summary, artifact_path, citations, document_meta}`（8–12k 字符），完整内容自己 `file_read` artifact | `orchestrator/scheduler_run.py`、`workers.py:91-99` |
| 7.6 | fan-in 额外产出结构化引用索引 | `orchestrator/fanin.py:256-297`、`graph/state.py:148-151` |
| 7.7 | `resolve_output_format(request, context)` 纯函数 + 只产一个交付文件 | 新文件 `tools/report/format.py` |
| 7.8 | `report_export(source, out_path, format, title, template)`：md / docx(markdown-it-py) / pdf(ReportLab) | 新文件 `tools/report/export.py` |
| 7.9 | CJK 字体运行时查找：`AGENT_REPORT_FONT_PATH` → Windows `msyh/simhei/simsun` → Linux/WSL Noto/文泉驿 → macOS PingFang；**找不到就明确失败**，不生成乱码 PDF，不降级 md | 新文件 `tools/report/fonts.py` |
| 7.10 | author 写范围白名单：全局单实例、仅 `.md/.docx/.pdf`、仅用户显式路径或报告目录、禁止源码目录、写入走快照与审计 | `orchestrator/write_gate.py` + `sandbox/policy.py` 之外的白名单模块（**不动 `policy.py` 分级表**） |
| 7.11 | 任务图模板：t1..t4 并发 document/vision（stage 0）→ t5 汇总（stage 1）→ t6 author（stage 2）→ t7 review（stage 3）；单点失败不阻塞，缺口写进“局限性”章节 | `orchestrator/plan.py` |
| 7.12 | `report_check` 确定性校验（锚点存在、章节齐全、docx zip 可开、pdf 页数>0 且字体可用）→ 预算允许再做一次 LLM 证据审查 | `tools/report/check.py` |
| 7.13 | 提示词：SYSTEM_PROMPT 检索建议段（`:22-31`）补 `document_read`；新增 `DOCUMENT_PIPELINE_PROMPT` 并挂到 `compose_system_prompt :406-530` | `llm/prompts.py` |

**注意** `graph/nodes/planner.py:90-91` / `replan.py:214` 用 `last_human_text`，
只能看到图片的文字部分 —— **不要“升级” planner/replan 为多模态**，给它们 vision pass 产出的文本描述即可，
否则会破坏 `with_json_output` 依赖的形状假设（`deepseek.py:29-50`）。

**验收**：`tests/unit/test_orchestrator_document_pipeline.py`（workflow 选择、角色校验、stage 屏障、依赖上下文）、
`tests/unit/test_report_export.py`（格式解析、单文件输出、路径白名单、无字体错误路径）、
`tests/unit/test_report_format.py`。

---

## 8. P5 契约 / 文档 / 测试收口

| 项 | 动作 |
|---|---|
| 契约 | 改了 `approve.py` / `config.py` → **必须** `python scripts/gen_contracts.py`（8 个 `docs/contracts/*` + `frontend/src/contracts/events.generated.ts`）；CI `docs.yml:30` 跑 `--check`，漂移即红 |
| 事件 | 优先复用 `ToolCallStarted/Finished` + `WorkerStarted/Finished`。**若必须新增事件**：`events.py` 加类 → 加进 `:450-479` union → 同步 `tests/unit/test_layer_contracts.py:69-98 REQUIRED_EVENT_TYPES`（`test_event_contract_is_frozen` 是精确相等断言）→ `event_map.py:148-163` 加 handler → `features/events.ts:20` 加 case → 重生成契约 |
| 架构门禁 | `tests/unit/test_architecture.py` / `test_layer_contracts.py` 会拦跨层 import；`tools/documents/` 不得 import `web/` |
| 前端 | `python scripts/build_frontend.py` + 提交产物；`npm test`；`npx playwright test`（含 axe）；注意 `security.yml:45-62` 有 `npm audit --audit-level=high` |
| 文档 | `* RFC-0005`：`status: draft → accepted`，补 §11 决策审批人与真实结果；
        `* ADR-0024 / 0025`：`proposed → accepted`；
        `* `acceptance-criteria.yaml` REQ-214～220：`planned → implemented` 并补 `tests:` 字段；
        `* `docs/contracts/config.md` 的 `AGENT_LLM_PROVIDERS` schema 示例已过时，需补 `capabilities`/`task_types` |

---

## 9. 依赖与体积

| 来源 | 包 | 备注 |
|---|---|---|
| extras `documents` | pypdf、python-docx、openpyxl、xlrd、python-pptx、Pillow、defusedxml | 仅宿主进程；不进主依赖 |
| extras `report` | python-docx、markdown-it-py、reportlab | 同上 |
| 前端 vendor | pdfjs-dist、mammoth、SheetJS | Copy 进 `static/vendor/`，**不 npm install**（它们是 `esbuild` 之外的运行时全局） |
| 许可证/体积 | 无附带字体、无模板二进制资源 | 见 ADR-0025 |

## 10. 回滚

- `AGENT_DOCUMENT_PIPELINE_ENABLED=false` → 退回 direct；
- 未装 extras → 工具返回明确环境错误，不影响既有会话；
- 删 `.agent/documents/`、`agent/artifacts/reports/` 不影响源码；
- 前端 viewer 按格式分派，`/api/file` 旧路径与标记断言保持兼容。

## 11. 风险登记

| 风险 | 缓解 |
|---|---|
| author worker 绕过审批写任意文件 | 扩展名 + 目录白名单 + 全局单实例 + 快照与审计（ADR-0025） |
| 图片重发导致配额击穿（§6） | 图片计入预算或首轮后剥离块；**不是可选项** |
| 文档内提示注入 | 不可信输入标记 + 工具边界；不是完整 DLP |
| 前端体积与 CSP/SRI/sw.js 五道门禁 | §4.4 清单逐项过；缺一项 CI 就红 |
| `fileLanguage` 映射到未注册语言静默失效 | 重打 hljs + `highlightAuto` 兜底 + 单测覆盖映射表 |
| PPTX 保真度 | 只承诺结构化内容（RFC §2），UI 明示 |
| `.doc` 用户仍需手动转格式 | 见 D1，登记技术债；UI 给明确提示而非静默失败 |

---

## 12. 2026-10-31 后续演进：结构化矩阵与树状归并

这一节记录“长文档从能读，到多文档可比较、可归因”的增量；最终决策见
[ADR-0038](adr/0038-structured-document-matrix-and-attribution.md)。

### 12.1 已落地

1. **章节感知切分** `_split_sections`：按编号标题、Abstract/Introduction/
   Method 等、全大写短行切 map 单元；超长章节再分段并保留标题；
   `document_read` 的元信息头在 map 前剥离。
2. **引用跨度校验**：每条证据 quote 在归一化文档文本中定位，记录
   `quote_verified` / `quote_span`；章节号、作者名、`page=13-15`、
   `page=8 Table 2` 等回写成真实 `page=N`；无法定位或锚点不存在的证据
   不进入下游。
3. **每篇固定槽位矩阵** `DocumentMatrix`：
   `problem/method/algorithm/experiments/limitations/innovations/conclusion`；
   空槽位用已验证 claim 按关键词回填。默认关闭
   （`AGENT_DOCUMENT_MATRIX_ENABLED=false`），长文档/报告场景显式开启。
4. **树状归并**：矩阵/摘要作为叶节点，按 `AGENT_DOCUMENT_TREE_FANOUT`
   （默认 5）逐层归并，单次输入受 `AGENT_DOCUMENT_TREE_INPUT_CHARS`
   （默认 16k）约束；最终答案附紧凑 `## 证据矩阵` 和“已解析文档（N 篇）”头。
5. **跨文档归属校验** `knowledge/attribution.py`：路径 stem + 矩阵标题生成
   别名，按句子检查“提到的文档”与引用路径；错配写入
   `WorkerResult.attribution_issues`，评测默认要求 0 条。
6. **报告评测** `tests/eval/papers.py::papers/report`：真实执行
   `document → author → report_check`，校验覆盖、章节、逐篇引用、矩阵槽位、
   引用跨度、归属与长度；`tests/integration/test_paper_documents.py` 提供
   `-m llm` 的 pytest 入口。

### 12.2 10 篇实测与边界

`pdf/` 10 篇、230 页、112MB：

| 用例 | 矩阵 | 槽位覆盖 | 归属错配 | 结果 |
|---|---|---:|---:|---|
| `cross_method` | 10 | 87% | 0 | 通过，600 证据、10/10 引用、judge 100 |
| `report` | 10 | 94% | 0 | 通过，14,002 字、五章节、report_check 与 judge 100 |

代价：10 篇单次约 58–60 万输入、47–55 万输出 token，19–24 分钟；不建议
交互路径默认全量跑。归属校验只能发现“提到 A 却引用 B”，发现不了
“引用 A 但结论属于 B”的语义错误；10 篇报告仍可能出现错误概述，需
LLM judge/人工复核。

### 12.3 配置与回滚

```bash
AGENT_DOCUMENT_MATRIX_ENABLED=false   # 默认关；开启后每篇多一次结构化调用
AGENT_DOCUMENT_TREE_FANOUT=5
AGENT_DOCUMENT_TREE_INPUT_CHARS=16000
```

关闭矩阵即回退到扁平摘要 + 树状归并；关闭 `AGENT_DOCUMENT_MAP_REDUCE`
则退回单次抽取（仍受 `document_max_chars` 上限）。
