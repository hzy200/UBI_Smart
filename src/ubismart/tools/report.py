"""报告格式解析、md/docx/pdf 导出与确定性校验。

PDF 不附带字体：运行时查找系统 CJK 字体；找不到就明确失败。
"""

from __future__ import annotations

import io
import os
import re
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from ubismart.config import Settings
from ubismart.execution.file_access import (
    FileAccess,
    LocalFileAccess,
    SandboxFileAccess,
)
from ubismart.sandbox.fs import SandboxFs
from ubismart.sandbox.snapshots import SnapshotStore
from ubismart.sandbox.wsl_exec import resolve_workspace
from ubismart.tools.artifacts import ReportArtifact, pack
from ubismart.workspace.file_preview import (
    PreviewError,
    document_limits,
    read_document,
    read_raw,
)

REPORT_EXPORT = "report_export"
REPORT_CHECK = "report_check"
REPORT_READ = "report_read"
_ALLOWED = {".md", ".docx", ".pdf"}


class ReportError(ValueError):
    pass


class ReportExportInput(BaseModel):
    content: str = Field(description="报告正文（Markdown）")
    out_path: str = Field(description="工作区内输出路径，必须以 .md/.docx/.pdf 结尾")
    reason: str = Field(description="导出报告的意图，一句话说明")
    format: Literal["auto", "md", "docx", "pdf"] = Field(
        default="auto", description="auto 按 out_path 后缀判断"
    )
    title: str = Field(default="", description="报告标题")
    template: str = Field(
        default="",
        description="可选模板：工作区内模板文件路径，或含 {{content}} 的内联模板",
    )


class ReportCheckInput(BaseModel):
    path: str = Field(description="工作区内报告路径")
    reason: str = Field(description="校验报告的意图，一句话说明")
    required_sections: list[str] = Field(
        default_factory=list,
        description="可选：报告必须包含的章节标题；缺失则校验失败",
    )


class ReportReadInput(BaseModel):
    path: str = Field(description="工作区内报告路径")
    reason: str = Field(description="读取报告的意图，一句话说明")


def resolve_output_formats(request: str, context: str = "") -> list[str]:
    """从用户请求解析显式格式；没有显式格式时按上下文返回单个默认值。"""
    text = f"{request or ''}\n{context or ''}"
    lowered = text.lower()
    formats: list[str] = []
    if re.search(r"(^|[^a-z])pdf([^a-z]|$)", lowered):
        formats.append("pdf")
    if re.search(r"(^|[^a-z])(word|docx)([^a-z]|$)", lowered):
        formats.append("docx")
    if re.search(r"(^|[^a-z])(markdown|md)([^a-z]|$)", lowered):
        formats.append("md")
    if formats:
        return list(dict.fromkeys(formats))
    if any(hint in text for hint in ("正式交付", "汇报", "排版", "交付给")):
        return ["docx"]
    return ["md"]


def resolve_output_format(request: str, context: str = "") -> str:
    return resolve_output_formats(request, context)[0]


# 用户显式点名时才纳入“必需章节”，避免给所有报告强加固定结构。
_SECTION_HINTS = (
    "摘要", "背景", "方法", "结论", "风险", "建议",
    "局限性", "参考来源", "对比", "方案",
)


def resolve_required_sections(request: str) -> list[str]:
    """从请求里提取用户显式要求的章节名。"""
    text = str(request or "")
    return [name for name in _SECTION_HINTS if name in text]


def _font_candidates(settings: Settings) -> list[str]:
    values = [
        str(getattr(settings, "report_font_path", "") or ""),
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\simsun.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/System/Library/Fonts/PingFang.ttc",
    ]
    return [value for value in values if value and os.path.isfile(value)]


def find_cjk_font(settings: Settings) -> str:
    candidates = _font_candidates(settings)
    if not candidates:
        raise ReportError(
            "未找到可用的系统 CJK 字体，无法生成 PDF。"
            "请设置 AGENT_REPORT_FONT_PATH 指向一个 .ttf/.ttc 字体文件，"
            "或改为输出 md/docx。"
        )
    return candidates[0]


def _escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _lines(content: str) -> list[str]:
    return str(content or "").replace("\r\n", "\n").split("\n")


def _load_reportlab() -> Any:
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import (
            Paragraph,
            Preformatted,
            SimpleDocTemplate,
            Spacer,
            Table,
            TableStyle,
        )
    except ImportError as exc:
        raise ReportError(
            "缺少 PDF 依赖 ReportLab。请安装：pip install 'ubismart[report]'"
        ) from exc
    return SimpleNamespace(
        colors=colors,
        A4=A4,
        ParagraphStyle=ParagraphStyle,
        getSampleStyleSheet=getSampleStyleSheet,
        mm=mm,
        pdfmetrics=pdfmetrics,
        TTFont=TTFont,
        Paragraph=Paragraph,
        Preformatted=Preformatted,
        SimpleDocTemplate=SimpleDocTemplate,
        Spacer=Spacer,
        Table=Table,
        TableStyle=TableStyle,
    )


def _register_pdf_font(font_path: str) -> str:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    font_name = "ReportCJK"
    try:
        pdfmetrics.registerFont(TTFont(font_name, font_path, subfontIndex=0))
    except TypeError:
        pdfmetrics.registerFont(TTFont(font_name, font_path))
    except Exception as exc:  # noqa: BLE001 - 字体损坏/格式不支持
        raise ReportError(f"注册 PDF 字体失败：{font_path}：{exc}") from exc
    return font_name


def _pdf_styles(
    rl: Any, font_name: str
) -> tuple[Any, Any, Any, Any]:
    base = rl.getSampleStyleSheet()
    body = rl.ParagraphStyle(
        "CJKBody", parent=base["BodyText"], fontName=font_name, fontSize=10.5,
        leading=16,
    )
    heading = rl.ParagraphStyle(
        "CJKHeading", parent=base["Heading2"], fontName=font_name, fontSize=14,
        leading=20, spaceBefore=8, spaceAfter=4,
    )
    code_style = rl.ParagraphStyle(
        "CJKCode", parent=base["Code"], fontName=font_name, fontSize=8.5,
        leading=12,
    )
    return base, body, heading, code_style


class _PdfContent:
    """把 Markdown 行流增量转换为 ReportLab story 元素。"""

    def __init__(
        self, rl: Any, base: Any, body: Any, heading: Any, code_style: Any
    ) -> None:
        self.rl = rl
        self.base = base
        self.body = body
        self.heading = heading
        self.code_style = code_style
        self.story: list[Any] = []
        self.code: list[str] = []
        self.table: list[list[str]] = []
        self.in_code = False

    def add_title(self, title: str) -> None:
        if not title:
            return
        self.story.append(self.rl.Paragraph(_escape(title), self.base["Title"]))
        self.story.append(self.rl.Spacer(1, 4 * self.rl.mm))

    def flush_code(self) -> None:
        if not self.code:
            return
        self.story.append(
            self.rl.Preformatted("\n".join(self.code), self.code_style)
        )
        self.story.append(self.rl.Spacer(1, 2 * self.rl.mm))
        self.code.clear()

    def flush_table(self) -> None:
        if not self.table:
            return
        cells = [
            [self.rl.Paragraph(_escape(cell), self.body) for cell in row]
            for row in self.table
        ]
        rendered = self.rl.Table(cells, hAlign="LEFT")
        rendered.setStyle(
            self.rl.TableStyle(
                [
                    ("GRID", (0, 0), (-1, -1), 0.4, self.rl.colors.grey),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]
            )
        )
        self.story.append(rendered)
        self.story.append(self.rl.Spacer(1, 2 * self.rl.mm))
        self.table.clear()

    def toggle_code_fence(self) -> None:
        if self.in_code:
            self.flush_code()
            self.in_code = False
            return
        self.flush_table()
        self.in_code = True

    def add_table_row(self, trimmed: str) -> None:
        cells = [cell.strip() for cell in trimmed.strip("|").split("|")]
        if all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
            return
        self.table.append(cells)

    def add_text(self, trimmed: str) -> None:
        match = re.match(r"^(#{1,6})\s+(.+)$", trimmed)
        if match:
            self.story.append(
                self.rl.Paragraph(_escape(match.group(2)), self.heading)
            )
            return
        if re.match(r"^[-*+]\s+", trimmed):
            self.story.append(
                self.rl.Paragraph(
                    "• " + _escape(re.sub(r"^[-*+]\s+", "", trimmed)),
                    self.body,
                )
            )
            return
        self.story.append(self.rl.Paragraph(_escape(trimmed), self.body))

    def feed_line(self, line: str) -> None:
        trimmed = line.strip()
        if trimmed.startswith("```"):
            self.toggle_code_fence()
            return
        if self.in_code:
            self.code.append(line)
            return
        if trimmed.startswith("|") and trimmed.endswith("|"):
            self.add_table_row(trimmed)
            return
        self.flush_table()
        if not trimmed:
            self.story.append(self.rl.Spacer(1, 1.5 * self.rl.mm))
            return
        self.add_text(trimmed)


def _pdf_bytes(content: str, title: str, font_path: str) -> bytes:
    rl = _load_reportlab()
    font_name = _register_pdf_font(font_path)
    base, body, heading, code_style = _pdf_styles(rl, font_name)
    builder = _PdfContent(rl, base, body, heading, code_style)
    builder.add_title(title)
    for line in _lines(content):
        builder.feed_line(line)
    builder.flush_code()
    builder.flush_table()
    output = io.BytesIO()
    document = rl.SimpleDocTemplate(
        output, pagesize=rl.A4, leftMargin=18 * rl.mm, rightMargin=18 * rl.mm,
        topMargin=18 * rl.mm, bottomMargin=18 * rl.mm, title=title or "report",
    )
    document.build(builder.story)
    return output.getvalue()


def _load_docx() -> tuple[Any, Any]:
    try:
        from docx import Document
        from markdown_it import MarkdownIt
    except ImportError as exc:
        raise ReportError(
            "缺少 DOCX 依赖 python-docx/markdown-it-py。"
            "请安装：pip install 'ubismart[report]'"
        ) from exc
    return Document, MarkdownIt


def _inline_text(token: Any) -> str:
    parts: list[str] = []
    for child in token.children or []:
        kind = child.type
        if kind in {"text", "code_inline"}:
            parts.append(str(child.content))
        elif kind in {"softbreak", "hardbreak"}:
            parts.append("\n")
        elif kind == "image":
            parts.append(str(child.attrGet("alt") or child.content or ""))
        elif kind == "html_inline":
            parts.append(re.sub(r"<[^>]+>", "", str(child.content)))
    return "".join(parts).strip()


class _DocxWriter:
    """按 token 类型增量写入 python-docx 文档。"""

    def __init__(self, document: Any) -> None:
        self.document = document
        self.heading_level = 0
        self.in_cell = False
        self.cell: list[str] = []
        self.row: list[str] = []
        self.table: list[list[str]] = []
        self.list_stack: list[bool] = []

    def feed(self, token: Any) -> None:
        handler = _DOCX_HANDLERS.get(token.type)
        if handler is not None:
            handler(self, token)

    def heading_open(self, token: Any) -> None:
        self.heading_level = int(str(token.tag).lstrip("h") or 1)

    def heading_close(self, token: Any) -> None:
        self.heading_level = 0

    def inline(self, token: Any) -> None:
        text = _inline_text(token)
        if not text:
            return
        if self.in_cell:
            self.cell.append(text)
            return
        if self.heading_level:
            self.document.add_heading(text, level=min(self.heading_level, 4))
            return
        if self.list_stack:
            self.document.add_paragraph(
                text,
                style="List Number" if self.list_stack[-1] else "List Bullet",
            )
            return
        self.document.add_paragraph(text)

    def code_block(self, token: Any) -> None:
        paragraph = self.document.add_paragraph()
        run = paragraph.add_run(str(token.content).rstrip("\n"))
        run.font.name = "Courier New"

    def bullet_list_open(self, token: Any) -> None:
        self.list_stack.append(False)

    def ordered_list_open(self, token: Any) -> None:
        self.list_stack.append(True)

    def list_close(self, token: Any) -> None:
        if self.list_stack:
            self.list_stack.pop()

    def table_open(self, token: Any) -> None:
        self.table = []

    def row_open(self, token: Any) -> None:
        self.row = []

    def cell_open(self, token: Any) -> None:
        self.in_cell = True
        self.cell = []

    def cell_close(self, token: Any) -> None:
        self.row.append(" ".join(self.cell))
        self.in_cell = False

    def row_close(self, token: Any) -> None:
        if self.row:
            self.table.append(self.row)

    def table_close(self, token: Any) -> None:
        if not self.table:
            return
        rendered = self.document.add_table(
            rows=len(self.table),
            cols=max(len(item) for item in self.table),
        )
        rendered.style = "Table Grid"
        for r_index, values in enumerate(self.table):
            for c_index, value in enumerate(values):
                rendered.cell(r_index, c_index).text = value
        self.table = []

    def horizontal_rule(self, token: Any) -> None:
        self.document.add_paragraph("—")


_DOCX_HANDLERS = {
    "heading_open": _DocxWriter.heading_open,
    "heading_close": _DocxWriter.heading_close,
    "inline": _DocxWriter.inline,
    "fence": _DocxWriter.code_block,
    "code_block": _DocxWriter.code_block,
    "bullet_list_open": _DocxWriter.bullet_list_open,
    "ordered_list_open": _DocxWriter.ordered_list_open,
    "bullet_list_close": _DocxWriter.list_close,
    "ordered_list_close": _DocxWriter.list_close,
    "table_open": _DocxWriter.table_open,
    "tr_open": _DocxWriter.row_open,
    "th_open": _DocxWriter.cell_open,
    "td_open": _DocxWriter.cell_open,
    "th_close": _DocxWriter.cell_close,
    "td_close": _DocxWriter.cell_close,
    "tr_close": _DocxWriter.row_close,
    "table_close": _DocxWriter.table_close,
    "hr": _DocxWriter.horizontal_rule,
}


def _docx_bytes(content: str, title: str) -> bytes:
    Document, MarkdownIt = _load_docx()
    markdown = MarkdownIt("commonmark").enable("table")
    tokens = markdown.parse(content or "")
    document = Document()
    if title:
        document.add_heading(title, level=0)
    writer = _DocxWriter(document)
    for token in tokens:
        writer.feed(token)
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def _apply_template(
    access: FileAccess,
    template: str,
    content: str,
    *,
    title: str,
    fmt: str,
) -> str:
    text = str(template or "").strip()
    if not text:
        return content
    if "\n" not in text and "{{" not in text:
        try:
            raw = read_raw(access, text, max_bytes=1024 * 1024)
            template_text = raw.data.decode("utf-8", errors="replace")
        except PreviewError as exc:
            raise ReportError(f"读取报告模板失败：{exc}") from exc
    else:
        template_text = text
    if "{{content}}" not in template_text:
        raise ReportError("报告模板必须包含 {{content}} 占位符")
    return (
        template_text.replace("{{title}}", title or "")
        .replace("{{format}}", fmt)
        .replace("{{content}}", content)
    )


_CITATION_RE = re.compile(
    r"([A-Za-z0-9_./-]+\.(?:pdf|docx|xlsx|xls|pptx|md|markdown|txt|csv))"
    r"#([A-Za-z0-9_=!./\-\u4e00-\u9fff]+)"
)


def _report_text(data: bytes, suffix: str) -> str:
    if suffix == ".docx":
        try:
            from docx import Document

            document = Document(io.BytesIO(data))
            parts = [paragraph.text for paragraph in document.paragraphs]
            for table in document.tables:
                for row in table.rows:
                    parts.append(" | ".join(cell.text for cell in row.cells))
            return "\n".join(parts)
        except Exception:  # noqa: BLE001 - 退回 UTF-8
            pass
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(data))
            return "\n".join(
                (page.extract_text() or "") for page in reader.pages
            )
        except Exception:  # noqa: BLE001
            pass
    return data.decode("utf-8", errors="replace")


def _normalize_section(text: str) -> str:
    return "".join(str(text or "").split()).casefold().lstrip("#")


def _report_headings(data: bytes, suffix: str) -> list[str]:
    """提取报告章节标题；无法可靠识别时返回空列表（退回正文匹配）。"""
    if suffix == ".md":
        headings: list[str] = []
        for line in data.decode("utf-8", errors="replace").splitlines():
            match = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", line)
            if match:
                headings.append(match.group(1))
        return headings
    if suffix == ".docx":
        try:
            from docx import Document

            document = Document(io.BytesIO(data))
            headings = []
            for paragraph in document.paragraphs:
                style = str(getattr(paragraph.style, "name", "") or "")
                lowered = style.casefold()
                if (
                    lowered.startswith(("heading", "title"))
                    or "标题" in style
                ):
                    if paragraph.text.strip():
                        headings.append(paragraph.text)
        except Exception:  # noqa: BLE001 - 退化到正文匹配
            return []
        else:
            return headings
    return []


def _section_warnings(
    required: list[str], text: str, headings: list[str]
) -> list[str]:
    missing: list[str] = []
    heading_norms = [_normalize_section(item) for item in headings]
    text_norm = _normalize_section(text)
    for name in required:
        needle = _normalize_section(name)
        if not needle:
            continue
        if heading_norms:
            found = any(needle in heading for heading in heading_norms)
        else:
            found = needle in text_norm
        if not found:
            missing.append(str(name))
    if not missing:
        return []
    return ["缺少必需章节：" + "、".join(missing)]


def _citation_warnings(
    access: FileAccess,
    settings: Settings,
    text: str,
) -> list[str]:
    citations: dict[str, set[str]] = {}
    for match in _CITATION_RE.finditer(text):
        source = match.group(1).lstrip("./")
        anchor = match.group(2)
        citations.setdefault(source, set()).add(anchor)
    warnings: list[str] = []
    for source, anchors in list(citations.items())[:50]:
        try:
            model = read_document(
                access,
                source,
                limits=document_limits(settings),
            )
            valid = {block.anchor for block in model.blocks if block.anchor}
        except Exception as exc:  # noqa: BLE001 - 引用坏了要报出来
            warnings.append(f"引用来源不可读：{source}（{exc}）")
            continue
        for anchor in sorted(anchors):
            if anchor not in valid:
                warnings.append(f"引用锚点不存在：{source}#{anchor}")
    return warnings


def _pdf_page_count(data: bytes) -> int:
    try:
        from pypdf import PdfReader

        return len(PdfReader(io.BytesIO(data)).pages)
    except Exception:  # noqa: BLE001 - 页数只是诊断信息
        return 0


def _normalize_out(settings: Settings, out_path: str, fmt: str) -> tuple[str, str]:
    rel = str(out_path or "").replace("\\", "/").strip()
    while rel.startswith("./"):
        rel = rel[2:]
    rel = rel.lstrip("/")
    if not rel or ".." in rel.split("/"):
        raise ReportError("输出路径必须是工作区内的相对路径")
    suffix = Path(rel).suffix.lower()
    if fmt != "auto" and suffix != f".{fmt}":
        raise ReportError(f"输出后缀 {suffix or '（无）'} 与格式 {fmt} 不一致")
    if fmt == "auto":
        fmt = suffix.lstrip(".") or "md"
        if suffix not in _ALLOWED:
            raise ReportError("输出必须是 .md/.docx/.pdf")
    if suffix not in _ALLOWED:
        raise ReportError("输出必须是 .md/.docx/.pdf")
    report_dir = str(getattr(settings, "report_output_dir", ".agent/artifacts/reports"))
    report_dir = report_dir.replace("\\", "/").strip("/")
    if not getattr(settings, "report_allow_any_path", False):
        if report_dir and not (rel == report_dir or rel.startswith(report_dir + "/")):
            raise ReportError(
                f"报告默认只能写到 {report_dir}/；如需自定义路径，"
                "请开启 AGENT_REPORT_ALLOW_ANY_PATH 或由用户显式确认。"
            )
    return rel, fmt


def _resolve_root(
    settings: Settings, sandbox: Any | None, root: str | None
) -> str:
    if root is not None:
        return root
    if sandbox is None:
        raise ValueError("build_report_tools 需要 sandbox 或 root")
    return resolve_workspace(settings, sandbox)


def _resolve_fs(sandbox: Any | None, root: str, fs: Any | None) -> Any | None:
    if fs is None and sandbox is not None:
        return SandboxFs(sandbox, root)
    return fs


def _resolve_access(
    root: str, fs: Any | None, file_access: FileAccess | None
) -> FileAccess:
    if file_access is not None:
        return file_access
    if fs is not None:
        return SandboxFileAccess(fs, root)
    return LocalFileAccess(root)


def _resolve_snapshots(
    settings: Settings,
    sandbox: Any | None,
    fs: Any | None,
    root: str,
    snapshots: Any | None,
) -> Any | None:
    if snapshots is None and sandbox is not None and fs is not None:
        return SnapshotStore(
            sandbox,
            fs,
            root,
            manifest_root=settings.resolved_snapshot_manifest_dir,
        )
    return snapshots


def _normalize_scope(write_scope: list[str] | None) -> list[str]:
    return [
        str(item).replace("\\", "/").strip("/")
        for item in (write_scope or [])
        if str(item).strip()
    ]


def _is_file_entry(info: Any) -> bool:
    if isinstance(info, dict):
        return bool(info.get("is_file"))
    return bool(getattr(info, "is_file", False))


def _render_report(
    settings: Settings, content: str, title: str, fmt: str
) -> tuple[bytes, str]:
    if fmt == "md":
        return str(content or "").encode("utf-8"), ""
    if fmt == "docx":
        return _docx_bytes(content, title), ""
    font = find_cjk_font(settings)
    return _pdf_bytes(content, title, font), font


def _validate_pdf(data: bytes) -> tuple[list[str], int]:
    issues: list[str] = []
    pages = 0
    if not data.startswith(b"%PDF"):
        issues.append("PDF 缺少 %PDF 文件头")
    try:
        from pypdf import PdfReader

        pages = len(PdfReader(io.BytesIO(data)).pages)
        if pages <= 0:
            issues.append("PDF 页数为 0")
    except ImportError:
        if not data.rstrip().endswith(b"%%EOF"):
            issues.append("PDF 缺少 %%EOF 结尾，可能被截断")
    except Exception as exc:  # noqa: BLE001
        issues.append(f"PDF 无法打开：{type(exc).__name__}: {exc}")
    return issues, pages


def _validate_report_bytes(data: bytes, suffix: str) -> tuple[list[str], int]:
    issues: list[str] = []
    pages = 0
    if suffix == ".docx":
        if not zipfile.is_zipfile(io.BytesIO(data)):
            issues.append("DOCX 不是有效的 ZIP 容器")
    elif suffix == ".pdf":
        pdf_issues, pages = _validate_pdf(data)
        issues.extend(pdf_issues)
    elif suffix == ".md":
        if not data.strip():
            issues.append("Markdown 为空")
    else:
        issues.append("报告必须是 .md/.docx/.pdf")
    return issues, pages


class _ReportContext:
    """封装一次 build_report_tools 调用所需的读写上下文。"""

    def __init__(
        self,
        settings: Settings,
        access: FileAccess,
        fs: Any | None,
        root: str,
        snapshots: Any | None,
        scope: list[str],
    ) -> None:
        self.settings = settings
        self.access = access
        self.fs = fs
        self.root = root
        self.snapshots = snapshots
        self.scope = scope

    def display(self, target: str) -> str:
        try:
            return self.access.relative_to_root(target)
        except Exception:  # noqa: BLE001
            return str(target)

    def check_scope(self, rel: str) -> None:
        if self.scope and not any(
            rel == item or rel.startswith(item + "/") for item in self.scope
        ):
            raise ReportError(f"输出不在 write_scope 内：{rel}")

    def resolve_target(self, rel: str) -> tuple[str, bool]:
        if self.fs is not None:
            target = self.fs.lexical(rel)
            try:
                existed = _is_file_entry(self.fs.stat(target))
            except Exception:  # noqa: BLE001 - 不存在是常态
                existed = False
            return target, existed
        target_path = Path(self.root, rel).resolve()
        try:
            target_path.relative_to(Path(self.root).resolve())
        except ValueError as exc:
            raise ReportError("输出路径越界") from exc
        return str(target_path), target_path.is_file()

    def snapshot_markdown(self, target: str, content: str, existed: bool) -> str:
        original = ""
        if existed:
            try:
                original = (
                    self.fs.read_text(target, max_bytes=8 * 1024 * 1024)
                    if self.fs is not None
                    else Path(target).read_text(encoding="utf-8")
                )
            except Exception:  # noqa: BLE001
                original = ""
        return self.snapshots.write_with_snapshot(
            target, content, original, existed=existed
        )

    def persist(self, target: str, data: bytes) -> None:
        if self.fs is not None:
            self.fs.write_bytes(target, data)
            return
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def write(
        self, rel: str, data: bytes, *, fmt: str, content: str
    ) -> tuple[str, str]:
        self.check_scope(rel)
        target, existed = self.resolve_target(rel)
        action = "overwrite" if existed else "create"
        if self.snapshots is not None:
            if fmt == "md":
                return action, self.snapshot_markdown(target, content, existed)
            if existed:
                raise ReportError(
                    "目标二进制报告已存在；为避免不可回滚的覆盖，"
                    "请换一个输出路径。"
                )
            snapshot_id = self.snapshots.save(target, "", existed=False)
        else:
            snapshot_id = ""
        self.persist(target, data)
        return action, snapshot_id

    def export(
        self,
        content: str,
        out_path: str,
        reason: str,
        format: str = "auto",
        title: str = "",
        template: str = "",
    ) -> str:
        try:
            rel, fmt = _normalize_out(self.settings, out_path, str(format))
            content = _apply_template(
                self.access,
                template,
                content,
                title=title,
                fmt=fmt,
            )
        except ReportError as exc:
            return pack(str(exc), ReportArtifact(path=out_path, ok=False, rejected=True))
        try:
            data, font = _render_report(self.settings, content, title, fmt)
            action, snapshot_id = self.write(
                rel, data, fmt=fmt, content=content
            )
        except ReportError as exc:
            return pack(str(exc), ReportArtifact(path=out_path, format=fmt, ok=False))
        except Exception as exc:  # noqa: BLE001 - 写盘/生成错误统一可读化
            return pack(
                f"报告导出失败：{type(exc).__name__}: {exc}",
                ReportArtifact(path=out_path, format=fmt, ok=False),
            )
        artifact = ReportArtifact(
            path=rel,
            action=action,
            format=fmt,
            ok=True,
            size=len(data),
            bytes_written=len(data),
            added=len(data),
            pages=_pdf_page_count(data) if fmt == "pdf" else 0,
            font=font,
            snapshot_id=snapshot_id or None,
        )
        return pack(
            f"已生成 {fmt.upper()} 报告：{rel}（{len(data)} 字节）\n{reason}",
            artifact,
        )

    def content_issues(
        self,
        raw: Any,
        suffix: str,
        required_sections: list[str] | None,
    ) -> list[str]:
        text = _report_text(raw.data, suffix)
        issues = _citation_warnings(self.access, self.settings, text)
        required = [
            str(item).strip()
            for item in (required_sections or [])
            if str(item).strip()
        ]
        if required:
            issues.extend(
                _section_warnings(
                    required,
                    text,
                    _report_headings(raw.data, suffix),
                )
            )
        return issues

    def check(
        self,
        path: str,
        reason: str,
        required_sections: list[str] | None = None,
    ) -> str:
        try:
            raw = read_raw(self.access, path)
        except PreviewError as exc:
            return pack(str(exc), ReportArtifact(path=path, ok=False, rejected=True))
        suffix = Path(path).suffix.lower()
        issues, pages = _validate_report_bytes(raw.data, suffix)
        if not issues:
            issues.extend(self.content_issues(raw, suffix, required_sections))
        ok = not issues
        artifact = ReportArtifact(
            path=raw.path, format=suffix.lstrip("."), ok=ok,
            size=raw.size, pages=pages, warnings=issues,
        )
        body = "报告校验通过" if ok else "报告校验失败：" + "；".join(issues)
        return pack(f"{body}\n{raw.path}\n{reason}", artifact)

    def read_report(self, path: str, reason: str) -> str:
        try:
            raw = read_raw(self.access, path)
        except PreviewError as exc:
            return pack(str(exc), ReportArtifact(path=path, ok=False, rejected=True))
        text = _report_text(raw.data, Path(path).suffix.lower())
        artifact = ReportArtifact(
            path=raw.path,
            action="read",
            format=Path(path).suffix.lstrip("."),
            ok=True,
            size=raw.size,
        )
        return pack(f"报告 {raw.path}：\n{reason}\n\n{text}", artifact)


def _build_tools(context: _ReportContext, allow_write: bool) -> list[BaseTool]:
    tools: list[BaseTool] = [
        StructuredTool.from_function(
            func=context.check,
            name=REPORT_CHECK,
            description="确定性校验报告文件（存在、格式、DOCX ZIP、PDF 页数/结尾）。只读。",
            args_schema=ReportCheckInput,
        ),
        StructuredTool.from_function(
            func=context.read_report,
            name=REPORT_READ,
            description="读取报告正文（md/docx/pdf），供证据审查使用。只读。",
            args_schema=ReportReadInput,
        ),
    ]
    if allow_write:
        tools.append(
            StructuredTool.from_function(
                func=context.export,
                name=REPORT_EXPORT,
                description=(
                    "把 Markdown 报告导出为 md/docx/pdf。"
                    "默认只允许写到 AGENT_REPORT_OUTPUT_DIR；"
                    "PDF 使用系统 CJK 字体，不附带字体文件，找不到会明确失败。"
                ),
                args_schema=ReportExportInput,
            )
        )
    return tools


def build_report_tools(
    settings: Settings,
    sandbox: Any | None = None,
    *,
    allow_write: bool = False,
    write_scope: list[str] | None = None,
    fs: Any | None = None,
    file_access: FileAccess | None = None,
    root: str | None = None,
    snapshots: Any | None = None,
) -> list[BaseTool]:
    resolved_root = _resolve_root(settings, sandbox, root)
    resolved_fs = _resolve_fs(sandbox, resolved_root, fs)
    access = _resolve_access(resolved_root, resolved_fs, file_access)
    resolved_snapshots = _resolve_snapshots(
        settings, sandbox, resolved_fs, resolved_root, snapshots
    )
    context = _ReportContext(
        settings,
        access,
        resolved_fs,
        resolved_root,
        resolved_snapshots,
        _normalize_scope(write_scope),
    )
    return _build_tools(context, allow_write)
