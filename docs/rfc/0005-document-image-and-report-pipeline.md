---
layer: L2
doc_id: rfc-0005
title: 二进制文档与图像理解、报告撰写能力（文档管线）
owner: 项目作者
status: accepted
created: 2026-10-30
last_reviewed: 2026-10-30
review_cadence: 状态变更或范围变化时
---

# RFC-0005：二进制文档与图像理解、报告撰写能力（文档管线）

> 在现有 A1/A2/A3/A4 分层内补齐 PDF/Office 文档阅读、图像理解、关键信息提炼与
> 报告撰写能力。工作流以 orchestrator 动态任务图为主干，不新增主图节点，不改审批与
> 沙箱的安全语义。

## 1. 目标

- **G1 前端可读**：Web/Desktop 能只读预览 PDF、DOCX、XLSX、XLS、PPTX、Markdown、
  文本与常见图片；代码文件继续带语法高亮与行号。
- **G2 旧格式边界明确**：`.doc` / `.ppt` 不做转换，统一提示“请先转为 docx/pptx”，
  不引入 LibreOffice 等外部进程。
- **G3 Agent 可解析**：新增只读工具 `document_read`，在宿主进程解析二进制文档，
  返回带页码/工作表/幻灯片锚点的分页文本与表格。
- **G4 图像可理解**：默认多模态 LLM 直接理解图片；端点不支持时明确报错，
  不静默退回纯文本。
- **G5 动态任务图**：新增 `document_pipeline` workflow 与 `document` / `vision` /
  `author` 角色，复用现有 orchestrator 子图、scheduler 与 fan-in。
- **G6 提炼可追溯**：每条关键结论必须带证据锚点；汇总阶段生成统一引用索引。
- **G7 报告可交付**：未指定格式时只产出一个文件（md 或 docx，判断不了用 md）；
  指定 PDF 时生成 PDF，但不随包附带字体或资源文件。
- **G8 安全 fail-closed**：路径守卫、秘密文件保护、ZIP/XML/图片炸弹防护、
  文档提示注入隔离、author 写路径白名单。

可验证目标（实现后写入
[`../acceptance/acceptance-criteria.yaml`](../acceptance/acceptance-criteria.yaml)）：

- `pytest -m "not wsl and not llm" tests/unit/test_document_tools.py`
- `pytest -m "not wsl and not llm" tests/unit/test_orchestrator_document_pipeline.py`
- `pytest -m "not wsl and not llm" tests/unit/test_workspace_file_preview.py`
- `pytest -m "not wsl and not llm" tests/unit/test_report_export.py`
- 前端 `node test.mjs` 与 `node test-dom.mjs`，以及 Playwright 冒烟。

## 2. 非目标

- 不做 PDF/图片编辑、批注与跨格式转换；Office 仅提供受限编辑并写入新文件
  （见 `document_edit`），不覆盖源文件。
- 不支持 `.doc` / `.ppt`；不引入 LibreOffice、pandoc、antiword 等外部转换器。
- 不默认引入 OCR 引擎（Tesseract/RapidOCR）；扫描件优先交给多模态 LLM。
- 不引入独立 vision provider；视觉能力视为默认 LLM 的属性。
- 不新增主图节点，不修改 `graph/build.py::estimate_recursion_limit`。
- 不随仓库/发布包附带字体、模板或其他二进制资源。
- 不承诺 PPTX 像素级还原，只承诺文本、表格、备注与图片的结构化呈现。
- 不把文档正文写入审计或长期记忆。
- 不改变审批、沙箱、路径守卫、密钥注入的既有语义。

## 3. 背景与证据

### 3.1 现状

- 前端是 TypeScript + Alpine CSP，文件预览集中在
  `src/ubismart/web/frontend/src/features/inspector.ts`：`openFile` 在 `:783`，
  语言映射在 `:873`，高亮在 `:910`。
- `/api/file` 只返回文本或 2MB 内图片 data URL：
  `src/ubismart/workspace/browse.py` 的 `MAX_READ_BYTES=256KB`、
  `MAX_IMAGE_BYTES=2MB`，`read_text_file` 在 `:123`；二进制文件不返回内容。
- A3 对应端点在 `src/ubismart/servers/agent_server_routes/workspace.py:105`；
  host 工作区走 `HostFileAccess`。
- Agent 的 `file_read` 基于 `SandboxFs.read_text`，遇到 NUL/非 UTF-8 直接拒绝
  （`src/ubismart/sandbox/fs.py:227`）。
- `FileAccess` 协议只有 `read_text`
  （`src/ubismart/execution/file_access.py:16`），但 `HostFileAccess.read_bytes`
  已存在（`src/ubismart/execution/host_file_access.py:99`），
  `SandboxFs.read_bytes` 也存在（`src/ubismart/sandbox/fs.py:179`）。
- `pyproject.toml` 没有任何 PDF/Office 解析库。
- orchestrator 的 `TaskSpec` 只有 `explore/test/review/implement`
  （`src/ubismart/orchestrator/contracts.py`）；依赖只控制启动，不向下游传递结果
  （`src/ubismart/orchestrator/workers.py`）。
- LLM 是 OpenAI 兼容 `ChatOpenAI`
  （`src/ubismart/llm/deepseek.py`），当前没有图像消息构造路径。
- CSP 为 `default-src 'self'; script-src 'self'; img-src 'self' data:`
  （`src/ubismart/web/middleware.py`）。

### 3.2 需求差距

1. 用户上传或工作区中的 PDF/Office 文档无法在界面阅读，Agent 也无法解析。
2. 图片只能显示缩略图，无法理解内容、图表或扫描页。
3. 多文档信息提炼没有可追溯的锚点体系，无法校验报告是否“有据可依”。
4. 报告撰写没有格式选择规则，也没有确定的交付物边界。
5. 现有 orchestrator 无法把文档提取结果传给汇总/撰写任务，也没有阶段屏障防止
   author 在提取未收敛时提前启动。

## 4. 设计

### 4.1 总体架构

```text
A1 Web / Desktop
  │  /api/file/raw           原始字节（Range）
  │  /api/file/document      结构化文档
  ▼
A2 control_server + web/routes
  │  agent_protocol
  ▼
A3 agent_server
  │  /v1/file/raw、/v1/file/document
  ▼
A4 tools
  ├─ document_read      PDF/DOCX/XLSX/XLS/PPTX/MD/文本
  ├─ image_inspect      PNG/JPG/WEBP/GIF/BMP
  └─ report_export      md/docx/pdf（按需）

Orchestrator 子图（现有结构，不新增主图节点）
survey → select_workflow(document_pipeline) → plan_tasks
      → steering_plan → write_gate → scheduler ⇄ steering_dispatch
      → fan_in → END
```

### 4.2 Orchestrator 动态任务图

#### 4.2.1 新 workflow：`document_pipeline`

在 `src/ubismart/orchestrator/contracts.py` 的 `WorkflowDecision` 中增加
`document_pipeline`，并在 `src/ubismart/orchestrator/workflow.py::rule_workflow`
中把判定放在 `file_count` 门槛之前：

- 请求含 PDF / Word / Excel / PPT / 图片 / 报告 / 提炼 / 总结等意图；
- 或 survey 发现本会话上传了文档/图片；
- 或工作区文档数量达到阈值。

满足则走 `document_pipeline`，不要求仓库文件数达到
`orchestrator_min_files`。`src/ubismart/orchestrator/routing.py::route_after_select`
同步识别新 workflow；`src/ubismart/orchestrator/survey.py` 增加文档/图片清单
（路径、扩展名、大小、可选 sha256），上传目录由 runtime 明确传入，不扫描其他会话。

#### 4.2.2 新任务角色

`TaskRole` 扩展为：

```text
explore / test / review / implement / document / vision / author
```

| 角色 | 工具 | 写权限 | 职责 |
|---|---|---|---|
| `document` | `document_read`、`file_list`、`find_files` | 否 | 单文档提取大纲、关键点、表格、引用锚点 |
| `vision` | `image_inspect`、`document_read` | 否 | 图片、图表、扫描页理解 |
| `author` | `file_read`、`file_write`、`file_edit`、`report_export` | 是 | 生成报告 |
| `review` | 只读工具 + `report_check` | 否 | 引用、章节、格式校验 |

动态任务提议（worker 的 `proposed_tasks`）只允许 `document` / `vision`；
`author` / `implement` 不允许由 worker 自行追加，保持 fail-closed。

#### 4.2.3 任务图模板

```text
t1 document  doc1 提取大纲/关键点/锚点 ─┐
t2 document  doc2 提取大纲/关键点/锚点 ─┼─ stage 0 并发
t3 vision    img1 图表理解             ─┤
t4 vision    img2 截图/扫描页理解       ─┘
t5 document  汇总、去重、证据索引        ← stage 1
t6 author    生成单一交付文件            ← stage 2
t7 review    引用/章节/格式校验          ← stage 3
```

- `t5` 对 `t1..t4` 使用 `depends_mode="any"`：单文档失败不阻塞整体，
  但必须记录缺失来源。
- `t6` 对 `t5` 使用 `full`。
- `t7` 对 `t6` 使用 `any`，允许检查 partial 报告。
- 文档/图片数超过 `orchestrator_max_tasks` 时按文档分组，不允许静默丢弃。

#### 4.2.4 调度器改造

**依赖上下文传递。** `WorkerRunner` 目前只拿到 `task`、`parent_request`、
`session_brief`。在 `src/ubismart/orchestrator/scheduler_run.py` 为下游任务注入
有界上下文：

```json
{
  "task_id": "t1",
  "role": "document",
  "status": "success",
  "summary": "...",
  "artifact_path": ".agent/artifacts/.../result.json",
  "citations": [{"path": "a.pdf", "anchor": "page=3", "quote": "..."}],
  "document_meta": {"pages": 12, "sheets": 2, "slides": 8}
}
```

- 只注入摘要、引用和 artifact 路径；单条 summary/finding/source 分别截断，
  整体再受 `AGENT_ORCHESTRATOR_DEPENDENCY_CONTEXT_CHARS`（默认 12k 字符）约束。
- 完整内容由 worker 通过 `file_read` 读取 artifact。
- 对 document/vision/author/implement 都生效。

**阶段屏障。** `TaskSpec` 增加 `stage: int`：

- 高 stage 的 join 任务必须等所有更低 stage 任务不再 pending；
- 动态追加任务继承提议者的 stage；
- 防止 author 在文档提取尚未收敛时开写。

**其他优化：**

| 优化 | 说明 |
|---|---|
| 角色级并发 | document 限制解析线程；vision 走 provider 并发；author 全局最多 1 个 |
| 角色级预算 | vision、document、author 分别统计 token 与轮次 |
| 解析缓存 | `.agent/documents/<sha256>/extract.json`，重试/多任务不重复解析 |
| 检查点 | 大文档按“每完成一个文档”触发 steering 边界 |
| 优先级 | document 提取 > vision > 汇总 > author > review |
| 失败隔离 | 单文档失败 → 汇总记录缺口 → 报告中明确“未覆盖来源” |
| fan-in | 除文本汇总外，输出结构化引用索引到 `orchestration_handoff` |

#### 4.2.5 降级路径

- 无写权限：只跑 document/vision/汇总，报告由主 agent 在收尾阶段直接回复，不落盘。
- 只有一个文档：任务图退化为 `document → 汇总 → author → review`。
- 不支持格式：任务返回 `unsupported_format`，附“请先转为 docx/pptx”，
  不中断整张图。
- 动态任务图校验失败：退回单个 document/explore 任务，并显式标记 degraded。

### 4.3 前端阅读器

#### 4.3.1 后端端点

新增 `src/ubismart/workspace/file_preview.py`：

1. `GET /api/file/raw?thread_id&path`
   - 原始字节，支持 `Range`、`ETag`、`Content-Type`、`nosniff`；
   - 用于 pdf.js、mammoth、SheetJS；
   - 必须走 `_resolve` 路径守卫并调用 `protected_read_reason`；
   - MIME 魔数嗅探，仅放行预览白名单；
   - 默认上限 20MB，可配置。

2. `GET /api/file/document?thread_id&path`
   - 返回统一 `DocumentModel`；
   - block 带锚点：PDF `page`、docx `paragraph`、xlsx `sheet!range`、
     pptx `slide`、md `heading`。

同步修改：

- `src/ubismart/web/routes/workspace.py`
- `src/ubismart/servers/agent_server_routes/workspace.py`
- `src/ubismart/agent_protocol.py` 的 `workspace` 能力增加
  `file_raw`、`file_document`
- host 模式走 `HostFileAccess.read_bytes`

#### 4.3.2 viewer 分派

实现集中在三个前端模块，避免为每种格式再拆一套组件：

| 文件 | 能力 |
|---|---|
| `features/viewer-kind.ts` | 纯函数分派：image / pdf / document / markdown / unsupported / code |
| `features/vendor-loader.ts` | 按需加载 pdf.js、mammoth、SheetJS，带 SRI |
| `features/inspector.ts` | 生成 fileView；PDF canvas + SVG 文本层、翻页/缩放/搜索；DOCX HTML + 页眉页脚/批注；XLSX sheet 标签 + 公式栏；PPTX SVG 版式 |

`openFile` 调用 `detectViewerKind` 后分派；`fileLanguage` 映射与
`highlightCode` 兜底保留。

#### 4.3.3 格式支持矩阵

| 格式 | 前端 | Agent |
|---|---|---|
| md/txt/csv/log/json/yaml/代码 | 渲染/高亮 | `file_read` |
| pdf | pdf.js | pypdf |
| docx | mammoth | python-docx（读 + `document_edit`） |
| xlsx | SheetJS | openpyxl（读 + `document_edit`） |
| xls | SheetJS | xlrd（只读） |
| pptx | 服务端结构化提取 | python-pptx（读 + `document_edit`） |
| `.doc` / `.ppt` | 不支持，提示转 docx/pptx | 不支持，返回转换提示 |
| 图片 | 原生预览 | `image_inspect`（只读） |

`document_edit` 只在 `allow_write` 时暴露（L1），输出必须是新路径，拒绝
宏文档与跨格式转换；PDF 与图片不提供编辑。

#### 4.3.4 安全与构建

- pdf.js 设置 `isEvalSupported: false`；PDF worker 从同源 `/static/vendor/` 加载。
- PDF 文本层用 SVG `<text>` 定位，不写内联样式，满足 `style-src 'self'`。
- DOCX HTML 经 DOMPurify；XLSX/PPTX 只用文本节点渲染，不拼接未清洗 HTML。
- SVG 不直接渲染；HTML 不作为文件预览执行。
- CSP 增加 `worker-src 'self'` 与 `frame-src 'self' blob:`；保持无 `unsafe-eval`。
- vendored：pdfjs-dist 3.11.174、mammoth 1.8.0、SheetJS 0.18.5；
  `vendor-loader.ts` 与 `vendor/README.md` 记录 SRI。
- Service Worker 缓存新增 vendor 文件；阅读器只读，不提供二进制编辑。

### 4.4 `document_read`

新增 `src/ubismart/tools/documents/`：

```text
document_read(path, mode="auto", page_range="", sheet="", slide_range="",
              include_tables=True, include_images=False,
              offset=0, max_chars=20000)
```

返回带锚点的分页文本：

```text
[PDF p.3] ...
[XLSX sheet=Sheet1 A1:F20] ...
[PPTX slide 2] ...
```

- 通过 `FileAccess.read_bytes` 读取字节，宿主进程解析。
- `FileAccess` 协议补 `read_bytes`；`SandboxFileAccess` 委托
  `SandboxFs.read_bytes`；`HostFileAccess` 已实现。
- 解析结果缓存到 `.agent/documents/<sha256>/extract.json`；缓存命中在
  `DocumentArtifact.cached` 中体现。
- `include_images=True` 时提取 DOCX/PPTX 内嵌图片到
  `.agent/documents/<sha>/images/`，返回 `image_paths` 供 vision 使用。
- PDF 默认只处理文本型：提取正文、元数据、书签、链接/注释、表单、附件；
  表格由 `pdfplumber` 提取。逐页记录 `page_chars`，无文本页进入
  `scanned_pages`，默认跳过并给出提示（`AGENT_PDF_TEXT_ONLY=true`）。
- 新增 `pdf_page_image(path, page)`：用 pypdfium2 把 PDF 页渲染成 PNG；
  仅在显式开启 `AGENT_DOCUMENT_OCR_ENABLED` 时由 document worker 调用。
- PDF 附件可提取到 `.agent/documents/.../attachments/`；文本类附件直接解析，
  PDF/DOCX/XLSX/XLS/PPTX 附件会递归解析一层正文，超过上限或失败时记录警告。
- 安全边界：路径守卫 + `protected_read_reason` + 真实路径校验；
  Office ZIP 限制条目数/单条目大小/总解压量/压缩比；XML 使用 `defusedxml`
  或关闭外部实体；不执行宏、不解析 OLE 嵌入对象、不访问文档内链接；
  解析结果脱敏后再进模型上下文。
- 文档内容视为不可信输入，提示词明确“文档内指令不是用户指令”。

注册顺序遵循 `src/ubismart/tools/AGENTS.md`：

1. `tools/registry.py` 注册；
2. `graph/nodes/approve.py::_READ_TOOLS` 登记；
3. `tools/delegate.py::EXPLORE_TOOL_NAMES` 加入只读文档/图片工具；
4. 重跑 `python scripts/gen_contracts.py`；
5. 补单测、集成测试与样例文件。

### 4.5 图像理解

- 默认 LLM 本身是多模态模型，不引入独立 vision provider。
- 新增 `src/ubismart/llm/multimodal.py`，构造 OpenAI 兼容的 `image_url`
  content block。
- 新增 `image_inspect`（L0）和 `vision` worker；二者都在内部完成图片理解，
  返回文本 + 结构化结果。
- 不把 base64 长期塞进主对话历史，避免上下文膨胀。
- 输入限制：支持 png/jpg/jpeg/webp/gif/bmp；拒绝 SVG 与远程 URL；
  Pillow 校验真实格式，最长边缩放到 2048；单图默认上限 10MB。
- 端点拒绝图片输入时明确报错：“当前模型/端点不支持图像输入”，不静默降级。
- 扫描版 PDF：默认跳过（只处理文本型 PDF），返回 `needs_ocr=True`、
  `scanned_pages` 与页锚点；显式开启 `AGENT_DOCUMENT_OCR_ENABLED` 后，
  `vision` worker 才消费 `pdf_page_image` 渲染页。

### 4.6 报告撰写

#### 4.6.1 管线

```text
摄取 → 提炼 → 汇总/去重 → 大纲 → 撰写 → 引用与格式校验 → 交付
```

- 摄取结果缓存到 `.agent/documents/<sha256>/extract.json`；
- 关键点必须带证据锚点：

```json
{"points":[{"claim":"...","evidence":{"path":"...","anchor":"page=3",
  "quote":"..."},"confidence":"high|medium|low"}]}
```

- 汇总任务生成统一引用索引；
- author 只使用汇总后的证据，不重新解析全文；
- review 做确定性校验，必要时再做一次 LLM 证据审查。

#### 4.6.2 输出格式解析

新增纯函数 `resolve_output_format(request, context)`：

| 判定 | 输出 |
|---|---|
| 明确 PDF | `pdf` |
| 明确 Word/docx | `docx` |
| 明确 Markdown/md/笔记/README | `md` |
| 未明确，但上下文是正式交付/汇报/需要 Word | `docx` |
| 未明确，代码仓库、说明、总结、聊天交付 | `md` |
| 仍无法判断 | **`md`** |

规则：

- 默认只产出一个交付文件；不再默认同时产出 md + docx。
- 用户明确要求多格式时，才生成多个。
- 内部可用 Markdown/结构化 `ReportModel` 作为中间产物，放在
  `.agent/artifacts/`，不作为交付物。
- 用户指定路径必须在工作区内；否则拒绝或落到默认报告目录。

#### 4.6.3 author worker

- 全局最多 1 个。
- 工具：`file_read`、`file_write`、`file_edit`、`report_export`。
- 写范围：仅允许 `.md` / `.docx` / `.pdf`；仅允许用户显式报告路径或配置的报告
  目录；禁止写源码目录；不经过 git worktree，但写入走快照与审计。
- 未指定格式且无法判断 → md。
- 生成后自检：引用锚点存在、必需章节齐全、输出文件可打开、
  缺失来源写入“局限性”章节。

#### 4.6.4 `report_export` 与 PDF 字体

```text
report_export(source, out_path, format="md|docx|pdf", title="", template="")
```

- `md`：直接写 Markdown。
- `docx`：python-docx + `markdown-it-py` token 渲染（标题、段落、列表、
  代码块、表格）。
- `pdf`：ReportLab 从 Markdown 行/表格生成，运行时注册系统 CJK 字体。
- `template`：可传工作区模板路径或内联模板，支持 `{{title}}`、
  `{{content}}`、`{{format}}`；缺少 `{{content}}` 时拒绝导出。
- **PDF 不附带字体文件**，运行时按顺序查找系统 CJK 字体：
  1. `AGENT_REPORT_FONT_PATH`；
  2. Windows：`msyh.ttc`、`simhei.ttf`、`simsun.ttc`；
  3. Linux/WSL：Noto CJK、文泉驿等；
  4. macOS：PingFang。
- 找不到系统 CJK 字体 → 明确报错并给出配置方式；不生成乱码 PDF，
  不静默降级为 md。
- Python 依赖通过可选 extra 安装，不向仓库/发布包添加字体或模板资源。

#### 4.6.5 review

先跑确定性 `report_check`：

- 报告文件存在、格式可打开（DOCX ZIP / PDF 页数/结尾）；
- 解析报告中的 `path#anchor` 引用，逐条回读来源文档并校验锚点存在；
- 输出格式与用户指定一致。

`report_check` 已接入 author 自检与 document pipeline 的 review 角色。
`AGENT_REPORT_LLM_REVIEW=true` 时，review 会在确定性校验后再调用一次 LLM，
判断报告结论是否能被引用锚点/上游证据支持；失败只降级为 partial，不否定
确定性校验结果。

## 5. 受影响面

| 项 | 内容 |
|---|---|
| 模块 | `orchestrator/`、`tools/`、`workspace/`、`web/routes/`、`servers/agent_server_routes/`、`web/frontend/`、`llm/`、`execution/` |
| 契约 | 新增工具等级；`agent_protocol` 能力；`config.py` 新配置；新增前端 type 无需新事件；若新增事件需重跑 `gen_contracts.py` |
| 数据 | 无数据库迁移；新增 `.agent/documents/`、`.agent/artifacts/reports/` 缓存与产物目录；均可清理 |
| 前端 | Web/Desktop 新增 viewer；CLI/TUI 不受影响 |
| 权限 | 新增 L0/L1 工具；author 写路径白名单；不改审批与沙箱语义 |
| 依赖 | 可选 extra：`documents`（pypdf、python-docx、openpyxl、xlrd、python-pptx、Pillow、defusedxml、pypdfium2）；`report`（python-docx、markdown-it-py、ReportLab） |

## 6. 安全与隐私

对照 [`../THREAT_MODEL.md`](../THREAT_MODEL.md)：

- **新增攻击面**：恶意 PDF/Office 文件、ZIP 炸弹、XXE、图片解压炸弹、
  文档内提示注入、报告路径越界。
- **路径与秘密文件**：raw/document 端点必须复用路径守卫与
  `protected_read_reason`；禁止读取 `.env`、私钥等秘密文件。
- **解析隔离**：不执行宏、不启动外部进程、不访问文档内链接；
  XML 关闭外部实体。
- **资源限制**：字节数、页数、单元格数、解压总量、压缩比、图片尺寸、
  解析超时。
- **提示注入**：文档内容一律标记为不可信输入，系统提示明确其不构成指令。
- **author 写权限**：只允许报告扩展名与白名单目录；全局单实例；
  写入走快照与审计；不写源码。
- **日志脱敏**：审计只记录路径、格式、大小、页数/表数/幻灯片数、耗时、
  是否命中缓存；不记录正文与图片内容。
- **人工批准**：不新增需要逐条批准的 L2 操作；`report_export` 为 L1，
  由 `--write` 一次性授权。

## 7. 兼容与回滚

- **破坏性变更**：无。新能力默认由配置控制。
- **回滚方式**：
  - `AGENT_DOCUMENT_PIPELINE_ENABLED=false` 关闭新 workflow，退回 direct；
  - 未安装 `documents` / `report` extra 时，相关工具返回明确的环境错误，
    不影响现有会话；
  - 删除 `.agent/documents/` 与 `.agent/artifacts/reports/` 不影响源码；
  - 前端 viewer 按格式分派，旧文件预览路径保持兼容。
- **回滚是否可回滚**：缓存与报告产物可重建；无数据库变更。

## 8. 可观测性

- 审计字段：文档路径、格式、字节数、页数/表数/幻灯片数、解析耗时、
  是否命中缓存、报告格式、输出路径、字体来源、PDF 页数。
- 事件：优先复用现有 `ToolStarted/Finished`、`WorkerStarted/Finished`；
  若前端需要文档级进度，再新增事件并重生成契约。
- 失败定位：worker 结果、artifact 与 fan-in 引用索引共同说明“做到哪一步、
  缺了哪些来源”。

## 9. 验收标准

```text
Given 用户上传一份 PDF、一份 DOCX 和两张图片
When  请求“提炼要点并写一份报告”，未指定格式
Then  只生成一个 report.md；引用可定位到 page/paragraph；
      单份文档解析失败时报告“局限性”章节列出缺失来源
```

```text
Given 用户明确要求 Word 交付
When  运行 document_pipeline
Then  只生成一个 report.docx；不额外生成 md/pdf
```

```text
Given 用户明确要求 PDF，且系统存在 CJK 字体
When  运行 author
Then  生成可打开的 report.pdf，页数 > 0
```

```text
Given 用户明确要求 PDF，但系统没有 CJK 字体
When  运行 author
Then  任务失败，错误信息包含 AGENT_REPORT_FONT_PATH 配置指引；
      不生成乱码 PDF，不静默降级为 md
```

```text
Given 用户请求预览 .doc 或 .ppt
When  前端打开文件或 Agent 调用 document_read
Then  返回“请先转为 docx/pptx”，不启动任何外部转换进程
```

验收锚点已写入
[`../acceptance/acceptance-criteria.yaml`](../acceptance/acceptance-criteria.yaml)
的 REQ-214 至 REQ-220（`status: planned`）；实现完成后补上对应的
`tests:` 字段与测试用例。

## 10. 替代方案与代价

| 方案 | 优点 | 代价 / 风险 | 为什么没选 |
|---|---|---|---|
| 在 WSL 沙箱内解析 | 与执行环境一致 | 需要给沙箱装解析库；破坏轻量边界 | 宿主解析已具备 `read_bytes` |
| 新增主图节点承载文档流程 | 直观 | 必须同步 recursion limit 与主图不变量 | 复用 orchestrator 子图风险更低 |
| 引入 LibreOffice 转换旧格式 | 保真度高 | 外部进程、安装体积、安全面扩大 | 明确提示转格式更可控 |
| 独立 vision provider | 可灵活选模型 | 多一套配置与密钥 | 默认 LLM 已是多模态 |
| 默认同时产出 md + docx | 用户省事 | 交付物冗余、格式选择不明确 | 改为按上下文二选一，兜底 md |
| 随包附带 CJK 字体 | 开箱即用 | 发布包体积增大、许可证管理 | 运行时查找系统字体 |
| PPTX 客户端渲染（pptxjs） | 不依赖服务端 | 依赖链脆弱、CSP/保真度差 | 服务端结构化提取更稳 |
| 报告由主 agent 直接写 | 实现简单 | 无法复用动态任务图与引用校验 | author 角色 + 屏障更可控 |

## 11. 决策与后果（批准/实现后填）

- 最终选择：按本 RFC 第 4 节实施，进入实现阶段。
- 批准人 / 日期：项目作者 / 2026-10-30。
- 真实结果与预期差异（2026-10-30）：
  - 前端已引入 vendored pdf.js 3.11.174、mammoth 1.8.0、SheetJS 0.18.5：
    PDF 按页渲染到 canvas，DOCX 转 HTML 后经 DOMPurify，XLSX/XLS 由 SheetJS
    解析；PPTX 仍按设计使用服务端结构化提取。三者按需加载并带 SRI。
  - `document_read` 使用进程内 LRU + `.agent/documents/<sha256>/extract.json`
    落盘缓存（`AGENT_DOCUMENT_CACHE` 可关闭）。
  - `orchestrator/survey.py` 已扫描本会话上传目录并生成文档/图片清单；
    runtime 也会在提示词中追加上传文件的元数据清单（REQ-221）。
  - 其余目标（宿主解析、stage 屏障、依赖上下文、单格式报告、系统字体 PDF）
    已按设计落地，验收锚点见 REQ-214～REQ-221。
  - 补充落地：author 全局单实例、写前快照与 FileChanged 审计、
    author/review 自动 `report_check`、角色级并发与优先级、按文档 checkpoint、
    pipeline 文档/图片上限、SVG 只按源码展示、Playwright viewer 冒烟、
    多文档全链路 E2E。
  - `report_export` 已支持模板文件/内联模板（`{{title}}`、`{{content}}`、
    `{{format}}`）；DOCX 导出改用 `markdown-it-py` token 渲染。
  - 扫描版 PDF：新增 `pdf_page_image` + `vision` worker 链路；
    `report_check` 会逐条校验引用锚点；document worker 对长 PDF 先取大纲再分段读取。
  - 角色级硬预算、author 硬单实例、超限文档/图片提示、ETag、导出页数、
    缓存命中和 scheduler 级多文档 E2E/取消测试已补齐。
  - PDF viewer 已增加 SVG 文本层、翻页与缩放；XLSX 支持 sheet 标签切换；
    `document_read.include_images` 已提取 DOCX/PPTX 内嵌图片并接入 vision；
    ZIP 炸弹、XXE、runtime 上传清单均已有专项测试。
  - 多格式报告已支持：显式指定多种格式时创建多个 author 任务，串行执行
    （最多 3 个）；默认仍只产出一个文件。
  - 可选 LLM 证据审查已实现（`AGENT_REPORT_LLM_REVIEW`）；
    `.agent/documents` 已有容量/TTL 清理；PDF viewer 已展示元数据与书签。
  - PDF viewer 已支持全文搜索、连续滚动、元数据/书签与 SVG 文本层；
    PPTX viewer 可展示内嵌图片；PDF 附件支持提取到缓存并解析文本类附件。
  - PDF 搜索已高亮命中文本；连续滚动改为按批懒加载，不再固定 20 页上限；
    PDF 附件支持递归解析一层正文。
  - PPTX 版式：`_pptx()` 用 python-pptx 提取 slide 尺寸、形状几何/旋转/填充、
    段落与 run 字体、表格与图片，生成绝对定位 SVG（`slide_layouts`）；
    前端 `.slide-svg` 直接展示，图片按需内联（最多 10 张、单张 ≤2MB）。
    仍不承诺像素级还原（无字体度量、阴影/渐变/动画），但版式位置已可视化。
  - XLSX：SheetJS `cellFormula` 解析公式，点击单元格在 `.xlsx-formula` 显示
    公式或值；表格行用 `content-visibility: auto` 做浏览器级行虚拟化。
  - DOCX：在 mammoth HTML 之外补充服务端页眉/页脚/分节（页面尺寸、方向、
    页边距）、脚注与批注（含作者），前端以 `.docx-extras` 展示。
  - 攻击样例：新增宏文档（`.docm`）、OLE 旧格式（`.xls`）、PDF JavaScript、
    docx 内嵌套 ZIP 的拒绝/不执行测试。
  - 压测脚本：`scripts/bench_documents.py` 覆盖大 PDF 单份/并发解析、
    tracemalloc 峰值与二次解析耗时；`--check --factor` 已接入 CI（20 页 ×
    2 份，数量级回归才红）。
  - 收尾项：依赖上下文增加全局字符上限
    （`AGENT_ORCHESTRATOR_DEPENDENCY_CONTEXT_CHARS`，默认 12k）；
    `report_check` 支持 `required_sections` 章节完整性校验；
    XLSX 改为真正的滚动窗口渲染（占位高度走 CSSOM，规避 CSP 对
    `style` 属性的拦截）；viewer 拆分到 `features/viewers/`。
  - `document_edit`（L1）已实现纯 Python 受限编辑：docx 文本替换/追加段落/
    设置页眉页脚、xlsx 设置单元格值或公式、pptx 文本替换/新增幻灯片文本；
    只写新路径（拒绝覆盖与同路径）、不允许跨格式、拒绝宏文档；
    受 `write_scope`、写保护 glob、字节上限与 `AGENT_DOCUMENT_EDIT_MAX_OPS`
    约束，并通过 `DocumentEditArtifact` 发出 FileChanged 事件。
  - 剩余差异：PPTX 不做像素级渲染（设计如此，SVG 版式不含字体度量与阴影）；
    PDF 文本层仍是近似定位；PDF/图片仍不可编辑，Office 编辑不做跨格式转换
    与原地覆盖。管理端全量 Playwright 在英文浏览器环境下的语言断言仍未
    收口（与本功能无关）。
- 新增技术债（登记到 [`../tech-debt.yaml`](../tech-debt.yaml)）：待定。
- 关联 ADR：[ADR-0024](../adr/0024-document-pipeline-on-orchestrator.md)
  （文档管线与动态任务图扩展）、
  [ADR-0025](../adr/0025-single-report-format-and-system-fonts.md)
  （报告格式与 PDF 字体策略）。

## 12. 任务分解与验证计划

| 任务 | 负责模块 | 验证方式 |
|---|---|---|
| 新 workflow、角色、`stage`、依赖上下文 | `orchestrator/` | `test_orchestrator_document_pipeline.py` |
| 角色级并发/预算/优先级 | `orchestrator/scheduler_*` | 调度单测 + 现有 orchestrator 回归 |
| `document_read` 与解析缓存 | `tools/documents/` | `test_document_tools.py`，样例 PDF/DOCX/XLSX/PPTX |
| `image_inspect` 与多模态消息 | `tools/vision.py`、`llm/multimodal.py` | `test_vision_tool.py`，端点不支持错误路径 |
| raw/document API 与 host/A3 路径 | `workspace/`、`web/routes/`、`servers/` | `test_workspace_file_preview.py` |
| 前端 viewer 与 CSP/SRI | `web/frontend/` | `node test.mjs`、`node test-dom.mjs`、Playwright |
| `report_export` 与 PDF 字体查找 | `tools/report.py` | `test_report_export.py`，无字体错误路径 |
| 引用校验与 review | `orchestrator/`、`tools/report.py` | 端到端多文档报告用例 |
| 文档与契约同步 | `docs/`、`contracts/` | `python scripts/check_docs.py`、`gen_contracts.py --check` |
