# ADR-0025：报告只产出一个格式，PDF 不附带字体

- **Status**：accepted
- **Date**：2026-10-30
- **Deciders**：项目作者
- **Owner**：项目作者

## Context

报告撰写工作流需要决定两件事：默认交付什么格式，以及 PDF 如何保证中文可读。
候选做法：

1. 默认同时产出 Markdown 与 DOCX，用户再自行选择；
2. 默认始终产出 Markdown，DOCX/PDF 仅在用户指定时生成；
3. 未指定时按上下文在 md/docx 中二选一，无法判断时用 md；
4. PDF 随包附带 CJK 字体，保证开箱即用；
5. PDF 运行时查找系统字体，找不到就明确失败。

同时，`author` 角色需要写工作区。若不加约束，worker 可以绕过主 agent 的
审批链路写任意文件。

## Decision

选择第三种 + 第五种：

### 交付格式

- 新增纯函数 `resolve_output_format(request, context)`：
  - 用户明确指定 PDF / Word / Markdown → 输出对应格式；
  - 未指定，但上下文是正式交付/汇报/需要 Word → `docx`；
  - 未指定，代码仓库、说明、总结、聊天交付 → `md`；
  - 仍无法判断 → **`md`**。
- **默认只产出一个交付文件**；只有用户明确要求多格式时才生成多个。
- 内部可以使用 Markdown 或结构化 `ReportModel` 作为中间产物，放在
  `.agent/artifacts/`，不作为交付物。

### author 写权限

- 全局最多 1 个 author 任务；
- 只允许 `.md` / `.docx` / `.pdf`；
- 只允许用户显式报告路径或配置的报告目录；
- 禁止写源码目录；
- 不经过 git worktree，但每次写入走快照与审计。

### PDF 字体

- 不随仓库、wheel 或发布包附带字体/模板等资源文件；
- 运行时按顺序查找：`AGENT_REPORT_FONT_PATH` → Windows 系统字体 →
  Linux/WSL Noto CJK/文泉驿 → macOS PingFang；
- 找不到 CJK 字体时**明确失败**，给出配置指引；不生成乱码 PDF，
  不静默降级为 md。

不选方案 1：双份交付会让“报告到底以哪个为准”变得模糊，用户还要自己删冗余文件。
不选方案 2：始终 md 会忽略“正式 Word 交付”这类明确上下文，用户仍需二次转换。
不选方案 4：字体文件会显著增大发布包，并引入许可证与版本管理成本；
系统字体在三个平台上都足够常见，缺失时也能给出可操作错误。
不选 LibreOffice/pandoc 生成 PDF：它们会引入外部进程与安装依赖，扩大执行面，
且与“旧格式不转换”的边界冲突。

## Consequences

### 好的

- 交付物唯一、可预期，验收只需检查一个文件；
- 用户指定格式时不会额外产生文件；
- 发布包不增加字体体积，许可证面更小；
- author 的写路径/扩展名白名单把 worker 写权限限制在报告范围内；
- PDF 缺字体时错误可诊断，不会产出看似成功但乱码的文件。

### 坏的 / 代价

- 上下文判断格式可能出错，用户需要重新指定一次；
- 系统没有 CJK 字体时 PDF 导出不可用，需要用户配置字体或改要 md/docx；
- DOCX/PDF 生成依赖可选 extra，未安装时只能明确失败；
- author 不经过 worktree，写入不是并发隔离的，只能靠“全局单实例 + 白名单”约束；
- ReportLab/fpdf2 的排版能力有限，复杂版式不如 Word/LibreOffice。

### 中性 / 需要留意

- PDF 的“字体可用”只保证能渲染，不保证与用户本机字体完全一致；
- `AGENT_REPORT_OUTPUT_DIR` 与用户显式路径需要做工作区边界校验；
- 多格式请求仍允许，但必须由用户显式提出，不作为默认行为。

## 复核

- **Verified by**：`tests/unit/test_report_export.py` 覆盖格式解析、模板、
  单文件输出、路径白名单、引用锚点校验、系统字体查找与无字体错误路径；
  `tests/unit/test_document_workers.py` 覆盖 author 自检、review 与可选
  LLM 证据审查；`test_document_pipeline_e2e.py` 覆盖显式多格式 author。
- **Revisit when**：用户反馈“必须一次拿到多种格式”成为常态；发布策略允许
  附带字体；或改用新的 PDF/文档生成链（如系统浏览器打印、LibreOffice）。

## 关联

- RFC：[`../rfc/0005-document-image-and-report-pipeline.md`](../rfc/0005-document-image-and-report-pipeline.md)
- 验收：[`../acceptance/acceptance-criteria.yaml`](../acceptance/acceptance-criteria.yaml)
