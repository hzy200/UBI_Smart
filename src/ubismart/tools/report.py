"""报告格式解析、md/docx/pdf 导出与确定性校验。

PDF 不附带字体：运行时查找系统 CJK 字体；找不到就明确失败。
"""

from __future__ import annotations

import io
import os
import re
import zipfile
from pathlib import Path
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


def _pdf_bytes(content: str, title: str, font_path: str) -> bytes:
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

    font_name = "ReportCJK"
    try:
        pdfmetrics.registerFont(TTFont(font_name, font_path, subfontIndex=0))
    except TypeError:
        pdfmetrics.registerFont(TTFont(font_name, font_path))
    except Exception as exc:  # noqa: BLE001 - 字体损坏/格式不支持
        raise ReportError(f"注册 PDF 字体失败：{font_path}：{exc}") from exc

    base = getSampleStyleSheet()
    body = ParagraphStyle(
        "CJKBody", parent=base["BodyText"], fontName=font_name, fontSize=10.5,
        leading=16,
    )
    heading = ParagraphStyle(
        "CJKHeading", parent=base["Heading2"], fontName=font_name, fontSize=14,
        leading=20, spaceBefore=8, spaceAfter=4,
    )
    code_style = ParagraphStyle(
        "CJKCode", parent=base["Code"], fontName=font_name, fontSize=8.5,
        leading=12,
    )
    story: list[Any] = []
    if title:
        story.append(Paragraph(_escape(title), base["Title"]))
        story.append(Spacer(1, 4 * mm))

    code: list[str] = []
    table: list[list[str]] = []

    def flush_code() -> None:
        if code:
            story.append(Preformatted("\n".join(code), code_style))
            story.append(Spacer(1, 2 * mm))
            code.clear()

    def flush_table() -> None:
        if not table:
            return
        cells = [
            [Paragraph(_escape(cell), body) for cell in row] for row in table
        ]
        rendered = Table(cells, hAlign="LEFT")
        rendered.setStyle(
            TableStyle(
                [
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]
            )
        )
        story.append(rendered)
        story.append(Spacer(1, 2 * mm))
        table.clear()

    in_code = False
    for line in _lines(content):
        trimmed = line.strip()
        if trimmed.startswith("```"):
            if in_code:
                flush_code()
                in_code = False
            else:
                flush_table()
                in_code = True
            continue
        if in_code:
            code.append(line)
            continue
        if trimmed.startswith("|") and trimmed.endswith("|"):
            cells = [cell.strip() for cell in trimmed.strip("|").split("|")]
            if all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
                continue
            table.append(cells)
            continue
        flush_table()
        if not trimmed:
            story.append(Spacer(1, 1.5 * mm))
            continue
        match = re.match(r"^(#{1,6})\s+(.+)$", trimmed)
        if match:
            story.append(Paragraph(_escape(match.group(2)), heading))
            continue
        if re.match(r"^[-*+]\s+", trimmed):
            story.append(
                Paragraph("• " + _escape(re.sub(r"^[-*+]\s+", "", trimmed)), body)
            )
            continue
        story.append(Paragraph(_escape(trimmed), body))

    flush_code()
    flush_table()
    output = io.BytesIO()
    document = SimpleDocTemplate(
        output, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=18 * mm, bottomMargin=18 * mm, title=title or "report",
    )
    document.build(story)
    return output.getvalue()


def _docx_bytes(content: str, title: str) -> bytes:
    try:
        from docx import Document
        from markdown_it import MarkdownIt
    except ImportError as exc:
        raise ReportError(
            "缺少 DOCX 依赖 python-docx/markdown-it-py。"
            "请安装：pip install 'ubismart[report]'"
        ) from exc

    markdown = MarkdownIt("commonmark").enable("table")
    tokens = markdown.parse(content or "")

    def inline_text(token: Any) -> str:
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

    document = Document()
    if title:
        document.add_heading(title, level=0)

    heading_level = 0
    in_cell = False
    cell: list[str] = []
    row: list[str] = []
    table: list[list[str]] = []
    list_stack: list[bool] = []

    for token in tokens:
        kind = token.type
        if kind == "heading_open":
            heading_level = int(str(token.tag).lstrip("h") or 1)
        elif kind == "heading_close":
            heading_level = 0
        elif kind == "inline":
            text = inline_text(token)
            if not text:
                continue
            if in_cell:
                cell.append(text)
            elif heading_level:
                document.add_heading(text, level=min(heading_level, 4))
            elif list_stack:
                document.add_paragraph(
                    text,
                    style="List Number" if list_stack[-1] else "List Bullet",
                )
            else:
                document.add_paragraph(text)
        elif kind in {"fence", "code_block"}:
            paragraph = document.add_paragraph()
            run = paragraph.add_run(str(token.content).rstrip("\n"))
            run.font.name = "Courier New"
        elif kind == "bullet_list_open":
            list_stack.append(False)
        elif kind == "ordered_list_open":
            list_stack.append(True)
        elif kind in {"bullet_list_close", "ordered_list_close"}:
            if list_stack:
                list_stack.pop()
        elif kind == "table_open":
            table = []
        elif kind == "tr_open":
            row = []
        elif kind in {"th_open", "td_open"}:
            in_cell = True
            cell = []
        elif kind in {"th_close", "td_close"}:
            row.append(" ".join(cell))
            in_cell = False
        elif kind == "tr_close":
            if row:
                table.append(row)
        elif kind == "table_close":
            if table:
                rendered = document.add_table(
                    rows=len(table),
                    cols=max(len(item) for item in table),
                )
                rendered.style = "Table Grid"
                for r_index, values in enumerate(table):
                    for c_index, value in enumerate(values):
                        rendered.cell(r_index, c_index).text = value
                table = []
        elif kind == "hr":
            document.add_paragraph("—")

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
            return headings
        except Exception:  # noqa: BLE001 - 退化到正文匹配
            return []
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
    if root is None:
        if sandbox is None:
            raise ValueError("build_report_tools 需要 sandbox 或 root")
        root = resolve_workspace(settings, sandbox)
    if fs is None and sandbox is not None:
        fs = SandboxFs(sandbox, root)
    if file_access is not None:
        access: FileAccess = file_access
    elif fs is not None:
        access = SandboxFileAccess(fs, root)
    else:
        access = LocalFileAccess(root)
    if snapshots is None and sandbox is not None and fs is not None:
        snapshots = SnapshotStore(
            sandbox,
            fs,
            root,
            manifest_root=settings.resolved_snapshot_manifest_dir,
        )
    scope = [
        str(item).replace("\\", "/").strip("/")
        for item in (write_scope or [])
        if str(item).strip()
    ]

    def _display(target: str) -> str:
        try:
            return access.relative_to_root(target)
        except Exception:  # noqa: BLE001
            return str(target)

    def _write(
        rel: str, data: bytes, *, fmt: str, content: str
    ) -> tuple[str, str]:
        if scope and not any(rel == item or rel.startswith(item + "/") for item in scope):
            raise ReportError(f"输出不在 write_scope 内：{rel}")
        if fs is not None:
            target = fs.lexical(rel)
            try:
                info = fs.stat(target)
                existed = (
                    bool(info.get("is_file"))
                    if isinstance(info, dict)
                    else bool(getattr(info, "is_file", False))
                )
            except Exception:  # noqa: BLE001 - 不存在是常态
                existed = False
        else:
            target_path = Path(root, rel).resolve()
            try:
                target_path.relative_to(Path(root).resolve())
            except ValueError as exc:
                raise ReportError("输出路径越界") from exc
            target = str(target_path)
            existed = target_path.is_file()

        snapshot_id = ""
        if snapshots is not None:
            if fmt == "md":
                original = ""
                if existed:
                    try:
                        original = (
                            fs.read_text(target, max_bytes=8 * 1024 * 1024)
                            if fs is not None
                            else Path(target).read_text(encoding="utf-8")
                        )
                    except Exception:  # noqa: BLE001
                        original = ""
                snapshot_id = snapshots.write_with_snapshot(
                    target, content, original, existed=existed
                )
                return ("overwrite" if existed else "create"), snapshot_id
            if existed:
                raise ReportError(
                    "目标二进制报告已存在；为避免不可回滚的覆盖，"
                    "请换一个输出路径。"
                )
            snapshot_id = snapshots.save(target, "", existed=False)

        if fs is not None:
            fs.write_bytes(target, data)
        else:
            path = Path(target)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return ("overwrite" if existed else "create"), snapshot_id

    def _export(
        content: str,
        out_path: str,
        reason: str,
        format: str = "auto",
        title: str = "",
        template: str = "",
    ) -> str:
        try:
            rel, fmt = _normalize_out(settings, out_path, str(format))
            content = _apply_template(
                access,
                template,
                content,
                title=title,
                fmt=fmt,
            )
        except ReportError as exc:
            return pack(str(exc), ReportArtifact(path=out_path, ok=False, rejected=True))
        try:
            if fmt == "md":
                data = str(content or "").encode("utf-8")
                font = ""
            elif fmt == "docx":
                data = _docx_bytes(content, title)
                font = ""
            else:
                font = find_cjk_font(settings)
                data = _pdf_bytes(content, title, font)
            action, snapshot_id = _write(
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

    def _check(
        path: str,
        reason: str,
        required_sections: list[str] | None = None,
    ) -> str:
        try:
            raw = read_raw(access, path)
        except PreviewError as exc:
            return pack(str(exc), ReportArtifact(path=path, ok=False, rejected=True))
        suffix = Path(path).suffix.lower()
        issues: list[str] = []
        pages = 0
        if suffix == ".docx":
            if not zipfile.is_zipfile(io.BytesIO(raw.data)):
                issues.append("DOCX 不是有效的 ZIP 容器")
        elif suffix == ".pdf":
            if not raw.data.startswith(b"%PDF"):
                issues.append("PDF 缺少 %PDF 文件头")
            try:
                from pypdf import PdfReader

                pages = len(PdfReader(io.BytesIO(raw.data)).pages)
                if pages <= 0:
                    issues.append("PDF 页数为 0")
            except ImportError:
                if not raw.data.rstrip().endswith(b"%%EOF"):
                    issues.append("PDF 缺少 %%EOF 结尾，可能被截断")
            except Exception as exc:  # noqa: BLE001
                issues.append(f"PDF 无法打开：{type(exc).__name__}: {exc}")
        elif suffix == ".md":
            if not raw.data.strip():
                issues.append("Markdown 为空")
        else:
            issues.append("报告必须是 .md/.docx/.pdf")
        if not issues:
            text = _report_text(raw.data, suffix)
            issues.extend(_citation_warnings(access, settings, text))
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
        ok = not issues
        artifact = ReportArtifact(
            path=raw.path, format=suffix.lstrip("."), ok=ok,
            size=raw.size, pages=pages, warnings=issues,
        )
        body = "报告校验通过" if ok else "报告校验失败：" + "；".join(issues)
        return pack(f"{body}\n{raw.path}\n{reason}", artifact)

    def _read_report(path: str, reason: str) -> str:
        try:
            raw = read_raw(access, path)
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

    tools: list[BaseTool] = [
        StructuredTool.from_function(
            func=_check,
            name=REPORT_CHECK,
            description="确定性校验报告文件（存在、格式、DOCX ZIP、PDF 页数/结尾）。只读。",
            args_schema=ReportCheckInput,
        ),
        StructuredTool.from_function(
            func=_read_report,
            name=REPORT_READ,
            description="读取报告正文（md/docx/pdf），供证据审查使用。只读。",
            args_schema=ReportReadInput,
        ),
    ]
    if allow_write:
        tools.append(
            StructuredTool.from_function(
                func=_export,
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
