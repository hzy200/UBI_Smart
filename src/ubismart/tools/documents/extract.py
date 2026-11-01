"""二进制/文本文档解析。

依赖全部按需导入：未安装 `documents` extra 时，相关格式返回明确的环境错误，
不影响其他工具。解析只读字节，不执行宏、不启动外部进程。
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import re
import zipfile
from pathlib import Path
from typing import Any

from ubismart.tools.documents.models import (
    DocumentBlock,
    DocumentError,
    DocumentLimits,
    DocumentModel,
    DocumentTooLarge,
    UnsupportedFormat,
)

TEXT_SUFFIXES = {
    ".md", ".markdown", ".txt", ".text", ".log", ".rst", ".tex",
    ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".conf", ".xml", ".html", ".htm", ".py", ".js", ".ts", ".tsx",
    ".jsx", ".css", ".scss", ".sql", ".sh", ".ps1", ".java", ".go",
    ".rs", ".c", ".h", ".cpp", ".hpp", ".kt",
}
CSV_SUFFIXES = {".csv", ".tsv"}
ZIP_SUFFIXES = {".docx", ".xlsx", ".xlsm", ".pptx"}
MIME = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    ".xls": "application/vnd.ms-excel",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".txt": "text/plain",
}
LEGACY_HINT = "请先另存为 docx/pptx 后再读取；本工具不启动 LibreOffice 等外部转换进程。"


def _suffix(path: str) -> str:
    return Path(str(path or "")).suffix.lower()


def _missing(package: str, suffix: str) -> DocumentError:
    return DocumentError(
        f"缺少 {suffix} 解析依赖 {package}。"
        "请安装可选依赖：pip install 'ubismart[documents]'。"
    )


def _zip_size_guard(
    infos: list[zipfile.ZipInfo], limits: DocumentLimits, suffix: str
) -> None:
    if len(infos) > limits.max_zip_entries:
        raise DocumentTooLarge(
            f"{suffix} 压缩包条目过多：{len(infos)} > {limits.max_zip_entries}"
        )
    total = sum(info.file_size for info in infos)
    compressed = sum(info.compress_size for info in infos)
    if total > limits.max_zip_uncompressed:
        raise DocumentTooLarge(
            f"{suffix} 解压后过大：{total} 字节 > {limits.max_zip_uncompressed}"
        )
    if compressed and total / max(compressed, 1) > limits.max_zip_ratio:
        raise DocumentTooLarge(
            f"{suffix} 压缩比异常（{total}/{compressed}），疑似 ZIP 炸弹"
        )


def _validate_xml_members(
    archive: zipfile.ZipFile,
    infos: list[zipfile.ZipInfo],
    limits: DocumentLimits,
    suffix: str,
) -> None:
    # OOXML 的 XML 部分是 XXE/实体扩展的入口；有 defusedxml 时
    # 逐个解析（总预算 5MB，避免把大文档变成解析负担）。
    try:
        from defusedxml import ElementTree as DefusedET
    except ImportError:
        return
    budget = max(limits.max_xml_part_bytes * 2, 8 * 1024 * 1024)
    for info in infos:
        name = info.filename.lower()
        if not name.endswith((".xml", ".rels")):
            continue
        if info.file_size > limits.max_xml_part_bytes:
            raise DocumentTooLarge(
                f"{suffix} 的 XML 部分过大：{info.filename} "
                f"({info.file_size} 字节)"
            )
        if budget <= 0:
            raise DocumentTooLarge(f"{suffix} 的 XML 总量超过校验预算")
        payload = archive.read(info)
        budget -= len(payload)
        try:
            DefusedET.fromstring(payload)
        except Exception as exc:  # noqa: BLE001
            raise DocumentError(
                f"{suffix} 的 XML 内容不安全或损坏（{info.filename}）："
                f"{type(exc).__name__}: {exc}"
            ) from exc


def _check_zip(data: bytes, limits: DocumentLimits, suffix: str) -> None:
    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise DocumentError(f"{suffix} 文件不是有效的 ZIP/OOXML 容器")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            _zip_size_guard(infos, limits, suffix)
            _validate_xml_members(archive, infos, limits, suffix)
    except zipfile.BadZipFile as exc:
        raise DocumentError(f"{suffix} 压缩包损坏：{exc}") from exc


def _truncate(
    blocks: list[DocumentBlock], limits: DocumentLimits
) -> tuple[list[DocumentBlock], bool]:
    kept: list[DocumentBlock] = []
    chars = 0
    truncated = False
    for block in blocks:
        size = len(block.text) + sum(len(cell) for row in block.rows for cell in row)
        if len(kept) >= limits.max_blocks or chars + size > limits.max_chars:
            truncated = True
            break
        kept.append(block)
        chars += size
    return kept, truncated


def _text_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _text_blocks(text: str) -> list[DocumentBlock]:
    if "\x00" in text:
        raise DocumentError("文件包含 NUL 字节，不是可读文本")
    blocks: list[DocumentBlock] = []
    heading_re = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
    buffer: list[str] = []
    heading_no = 0
    line_no = 0

    def flush() -> None:
        body = "\n".join(buffer).strip("\n")
        if body.strip():
            blocks.append(
                DocumentBlock(
                    kind="text",
                    anchor=f"line={max(1, line_no - len(buffer) + 1)}",
                    text=body,
                )
            )

    for line in text.splitlines():
        line_no += 1
        match = heading_re.match(line)
        if match:
            flush()
            buffer = []
            heading_no += 1
            blocks.append(
                DocumentBlock(
                    kind="heading",
                    anchor=f"heading={heading_no}",
                    text=match.group(2).strip(),
                )
            )
            continue
        buffer.append(line)
    flush()
    if not blocks:
        blocks.append(DocumentBlock(kind="text", anchor="line=1", text=""))
    return blocks


def _csv_blocks(text: str, delimiter: str) -> list[DocumentBlock]:
    rows: list[list[str]] = []
    try:
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        for index, row in enumerate(reader):
            if index >= 2_000:
                break
            rows.append([_text_value(cell) for cell in row[:200]])
    except csv.Error as exc:
        raise DocumentError(f"CSV 解析失败：{exc}") from exc
    return [
        DocumentBlock(
            kind="table",
            anchor="rows=1",
            text=f"CSV/TSV 共 {len(rows)} 行（最多显示 2000 行 × 200 列）",
            rows=rows,
        )
    ]


def _pdf_outline(reader: Any) -> list[DocumentBlock]:
    blocks: list[DocumentBlock] = []

    def walk(items: Any, depth: int = 0) -> None:
        for item in items or []:
            if isinstance(item, list):
                walk(item, depth + 1)
                continue
            try:
                title = str(getattr(item, "title", "") or "").strip()
            except Exception:  # noqa: BLE001
                title = ""
            if not title:
                continue
            page = 0
            try:
                page = int(reader.get_destination_page_number(item)) + 1
            except Exception:  # noqa: BLE001 - 目标页可能不存在
                page = 0
            blocks.append(
                DocumentBlock(
                    kind="heading",
                    anchor=f"outline={len(blocks) + 1}",
                    text=title,
                    meta={"page": page, "depth": depth},
                )
            )

    try:
        walk(reader.outline)
    except Exception:  # noqa: BLE001 - 书签损坏不影响正文
        return []
    return blocks


def _pdf_annotations(reader: Any, page_index: int) -> list[DocumentBlock]:
    blocks: list[DocumentBlock] = []
    try:
        annotations = reader.pages[page_index].get("/Annots") or []
    except Exception:  # noqa: BLE001
        return blocks
    for index, raw in enumerate(annotations, 1):
        try:
            annotation = raw.get_object()
            content = str(annotation.get("/Contents") or "").strip()
            subtype = str(annotation.get("/Subtype") or "")
            action = annotation.get("/A")
            uri = ""
            if action:
                uri = str(action.get_object().get("/URI") or "").strip()
            if uri:
                blocks.append(
                    DocumentBlock(
                        kind="link",
                        anchor=f"page={page_index + 1}/link={index}",
                        text=uri,
                    )
                )
            elif content:
                blocks.append(
                    DocumentBlock(
                        kind="note",
                        anchor=f"page={page_index + 1}/annotation={index}",
                        text=f"{subtype}：{content}".strip("："),
                    )
                )
        except Exception:  # noqa: BLE001 - 单条注释损坏跳过
            continue
    return blocks


def _pdf_tables(
    data: bytes, limits: DocumentLimits
) -> dict[int, list[list[list[str]]]]:
    """按页提取表格；pdfplumber 缺失时返回空。"""
    try:
        import pdfplumber
    except ImportError:
        return {}
    tables: dict[int, list[list[list[str]]]] = {}
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for index, page in enumerate(pdf.pages[: limits.max_pages]):
                try:
                    raw_tables = page.extract_tables() or []
                except Exception:  # noqa: BLE001 - 单页表格失败跳过
                    continue
                for raw in raw_tables:
                    rows = [
                        [str(cell or "").strip() for cell in row[:200]]
                        for row in (raw or [])[:2_000]
                    ]
                    if not rows:
                        continue
                    tables.setdefault(index + 1, []).append(rows)
                    if sum(len(items) for items in tables.values()) >= limits.max_tables:
                        return tables
    except Exception:  # noqa: BLE001 - pdfplumber 失败不影响文本
        return tables
    return tables


def _open_pdf(data: bytes) -> tuple[Any, int]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise _missing("pypdf", ".pdf") from exc
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                unlocked = reader.decrypt("")
            except Exception:  # noqa: BLE001 - 加密 PDF 统一拒绝
                unlocked = 0
            if not unlocked:
                raise DocumentError("PDF 已加密，无法读取；请先解除密码保护")
        total = len(reader.pages)
    except DocumentError:
        raise
    except Exception as exc:  # noqa: BLE001 - pypdf 的异常类型不稳定
        raise DocumentError(f"PDF 打开失败：{type(exc).__name__}: {exc}") from exc
    return reader, total


def _pdf_metadata(reader: Any, limits: DocumentLimits) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    if not limits.pdf_metadata:
        return metadata
    try:
        raw = reader.metadata or {}
        for key, name in (
            ("/Title", "title"),
            ("/Author", "author"),
            ("/Subject", "subject"),
            ("/Keywords", "keywords"),
            ("/Creator", "creator"),
            ("/Producer", "producer"),
            ("/CreationDate", "created"),
            ("/ModDate", "modified"),
        ):
            value = str(raw.get(key) or "").strip()
            if value:
                metadata[name] = value
    except Exception:  # noqa: BLE001 - 元数据损坏不影响正文
        metadata = {}
    return metadata


def _pdf_pages(
    reader: Any, total: int, limits: DocumentLimits
) -> tuple[list[DocumentBlock], list[int], list[int]]:
    blocks: list[DocumentBlock] = []
    page_chars: list[int] = []
    scanned_pages: list[int] = []
    for index in range(min(total, limits.max_pages)):
        text = ""
        try:
            text = reader.pages[index].extract_text() or ""
        except Exception as exc:  # noqa: BLE001 - 单页失败不拖垮整份
            blocks.append(
                DocumentBlock(
                    kind="text",
                    anchor=f"page={index + 1}",
                    text=f"（本页提取失败：{type(exc).__name__}: {exc}）",
                )
            )
            page_chars.append(0)
            continue
        page_chars.append(len(text))
        if not text.strip():
            scanned_pages.append(index + 1)
            continue
        if limits.pdf_annotations:
            blocks.extend(_pdf_annotations(reader, index))
        blocks.append(
            DocumentBlock(kind="text", anchor=f"page={index + 1}", text=text.strip())
        )
    return blocks, page_chars, scanned_pages


def _pdf_table_blocks(
    data: bytes, limits: DocumentLimits, total: int
) -> list[DocumentBlock]:
    blocks: list[DocumentBlock] = []
    if not limits.pdf_tables or not total:
        return blocks
    for page, page_tables in _pdf_tables(data, limits).items():
        for number, rows in enumerate(page_tables, 1):
            blocks.append(
                DocumentBlock(
                    kind="table",
                    anchor=f"page={page}/table={number}",
                    text=f"第 {page} 页表格 {number}",
                    rows=rows,
                )
            )
    return blocks


def _pdf_form_blocks(reader: Any, limits: DocumentLimits) -> list[DocumentBlock]:
    blocks: list[DocumentBlock] = []
    if not limits.pdf_forms:
        return blocks
    try:
        fields = reader.get_fields() or {}
        for name, field in list(fields.items())[:100]:
            value = str((field or {}).get("/V") or "").strip()
            blocks.append(
                DocumentBlock(
                    kind="form",
                    anchor=f"form={name}",
                    text=f"{name} = {value}" if value else str(name),
                )
            )
    except Exception:  # noqa: BLE001 - 表单损坏跳过
        pass
    return blocks


def _pdf_attachment_blocks(
    reader: Any, limits: DocumentLimits
) -> list[DocumentBlock]:
    blocks: list[DocumentBlock] = []
    if not limits.pdf_attachments:
        return blocks
    try:
        for name, payloads in list((reader.attachments or {}).items())[:50]:
            size = sum(len(item) for item in payloads or [])
            blocks.append(
                DocumentBlock(
                    kind="attachment",
                    anchor=f"attachment={name}",
                    text=f"{name}（{size} 字节）",
                )
            )
    except Exception:  # noqa: BLE001 - 附件损坏跳过
        pass
    return blocks


def _pdf_warnings(scanned_pages: list[int], limits: DocumentLimits) -> list[str]:
    if not scanned_pages:
        return []
    if limits.pdf_text_only:
        return [
            f"检测到 {len(scanned_pages)} 页无文本层（第 "
            f"{', '.join(str(item) for item in scanned_pages[:20])} 页）；"
            "当前仅处理文本型 PDF，这些页已跳过。"
        ]
    return [f"检测到 {len(scanned_pages)} 页无文本层，需要 OCR/视觉链路。"]


def _pdf(data: bytes, limits: DocumentLimits) -> DocumentModel:
    reader, total = _open_pdf(data)
    metadata = _pdf_metadata(reader, limits)

    blocks: list[DocumentBlock] = []
    if limits.pdf_outline:
        blocks.extend(_pdf_outline(reader))
    page_blocks, page_chars, scanned_pages = _pdf_pages(reader, total, limits)
    blocks.extend(page_blocks)
    blocks.extend(_pdf_table_blocks(data, limits, total))
    blocks.extend(_pdf_form_blocks(reader, limits))
    blocks.extend(_pdf_attachment_blocks(reader, limits))

    warnings = _pdf_warnings(scanned_pages, limits)
    title = str(metadata.get("title") or "")
    return DocumentModel(
        format="pdf",
        mime=MIME[".pdf"],
        title=title,
        metadata=metadata,
        blocks=blocks,
        pages=total,
        page_chars=page_chars,
        scanned_pages=scanned_pages,
        needs_ocr=bool(scanned_pages),
        truncated=total > limits.max_pages,
        warnings=warnings,
    )


def _docx_imports() -> tuple[Any, Any, Any, Any]:
    try:
        import docx
        from docx.oxml.ns import qn
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except ImportError as exc:
        raise _missing("python-docx", ".docx") from exc
    return docx, qn, Table, Paragraph


def _docx_table_block(
    table: Any, table_no: int, limits: DocumentLimits
) -> DocumentBlock:
    rows: list[list[str]] = []
    cells = 0
    for row in table.rows:
        values: list[str] = []
        for cell in row.cells:
            if cells >= limits.max_cells:
                break
            values.append(_text_value(cell.text).strip())
            cells += 1
        if values:
            rows.append(values)
        if cells >= limits.max_cells:
            break
    return DocumentBlock(
        kind="table",
        anchor=f"table={table_no}",
        text=f"表格 {table_no}",
        rows=rows,
    )


def _docx_body_blocks(
    document: Any, qn: Any, Table: Any, Paragraph: Any, limits: DocumentLimits
) -> list[DocumentBlock]:
    blocks: list[DocumentBlock] = []
    para_no = 0
    table_no = 0
    try:
        body = document.element.body
        for child in body.iterchildren():
            if child.tag == qn("w:p"):
                paragraph = Paragraph(child, document)
                text = str(paragraph.text or "").strip()
                if text:
                    para_no += 1
                    blocks.append(
                        DocumentBlock(
                            kind="text",
                            anchor=f"paragraph={para_no}",
                            text=text,
                        )
                    )
            elif child.tag == qn("w:tbl"):
                table_no += 1
                blocks.append(
                    _docx_table_block(Table(child, document), table_no, limits)
                )
    except Exception as exc:  # noqa: BLE001
        raise DocumentError(f"DOCX 解析失败：{type(exc).__name__}: {exc}") from exc
    return blocks


def _docx_section_layout(section: Any, section_index: int) -> DocumentBlock:
    width = float(section.page_width.inches or 0)
    height = float(section.page_height.inches or 0)
    orientation = "横向" if width > height else "纵向"
    return DocumentBlock(
        kind="section",
        anchor=f"section={section_index}",
        text=(
            f"页面 {width:.1f}×{height:.1f} in（{orientation}）；"
            f"页边距 上{section.top_margin.inches:.1f} "
            f"下{section.bottom_margin.inches:.1f} "
            f"左{section.left_margin.inches:.1f} "
            f"右{section.right_margin.inches:.1f} in"
        ),
    )


def _docx_container_blocks(
    part: Any, label: str, section_index: int
) -> list[DocumentBlock]:
    try:
        texts = [
            str(paragraph.text or "").strip()
            for paragraph in part.paragraphs
            if str(paragraph.text or "").strip()
        ]
    except Exception:  # noqa: BLE001
        return []
    if not texts:
        return []
    return [
        DocumentBlock(
            kind=label,
            anchor=f"{label}={section_index}",
            text="\n".join(texts),
        )
    ]


def _docx_section_blocks(document: Any) -> list[DocumentBlock]:
    blocks: list[DocumentBlock] = []
    try:
        for section_index, section in enumerate(document.sections, 1):
            try:
                blocks.append(_docx_section_layout(section, section_index))
            except Exception:  # noqa: BLE001
                continue
            for label, part in (
                ("header", section.header),
                ("footer", section.footer),
            ):
                blocks.extend(
                    _docx_container_blocks(part, label, section_index)
                )
    except Exception:  # noqa: BLE001 - 页面/页眉损坏不影响正文
        pass
    return blocks


def _docx_note_member(
    archive: zipfile.ZipFile,
    member: str,
    tag: str,
    label: str,
    defused_et: Any,
) -> list[DocumentBlock]:
    payload = archive.read(member)
    root = defused_et.fromstring(payload)
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    blocks: list[DocumentBlock] = []
    for index, node in enumerate(root.iter(f"{namespace}{tag}"), 1):
        if node.get(f"{namespace}type") in {
            "separator",
            "continuationSeparator",
        }:
            continue
        text = "".join(
            str(item.text or "") for item in node.iter(f"{namespace}t")
        ).strip()
        if not text:
            continue
        author = str(node.get(f"{namespace}author") or "").strip()
        blocks.append(
            DocumentBlock(
                kind="note",
                anchor=f"{label}={index}",
                text=f"[{author}] {text}" if author else text,
            )
        )
    return blocks


def _docx_note_blocks(data: bytes) -> list[DocumentBlock]:
    try:
        from defusedxml import ElementTree as DefusedET
    except ImportError:
        return []
    blocks: list[DocumentBlock] = []
    members = (
        ("word/footnotes.xml", "footnote", "footnote"),
        ("word/comments.xml", "comment", "comment"),
    )
    for member, tag, label in members:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                blocks.extend(
                    _docx_note_member(archive, member, tag, label, DefusedET)
                )
        except KeyError:
            continue
        except Exception:  # noqa: BLE001 - 附件 XML 损坏不影响正文
            continue
    return blocks


def _docx(data: bytes, limits: DocumentLimits) -> DocumentModel:
    docx, qn, Table, Paragraph = _docx_imports()
    _check_zip(data, limits, ".docx")
    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        raise DocumentError(f"DOCX 打开失败：{type(exc).__name__}: {exc}") from exc

    blocks = _docx_body_blocks(document, qn, Table, Paragraph, limits)
    blocks.extend(_docx_section_blocks(document))
    blocks.extend(_docx_note_blocks(data))

    try:
        title = str(document.core_properties.title or "").strip()
    except Exception:  # noqa: BLE001
        title = ""
    return DocumentModel(format="docx", mime=MIME[".docx"], title=title, blocks=blocks)


def _xlsx(data: bytes, suffix: str, limits: DocumentLimits) -> DocumentModel:
    try:
        import openpyxl
    except ImportError as exc:
        raise _missing("openpyxl", suffix) from exc

    _check_zip(data, limits, suffix)
    try:
        workbook = openpyxl.load_workbook(
            io.BytesIO(data), read_only=True, data_only=True
        )
    except Exception as exc:  # noqa: BLE001
        raise DocumentError(f"{suffix} 打开失败：{type(exc).__name__}: {exc}") from exc

    blocks: list[DocumentBlock] = []
    cells = 0
    sheet_count = len(workbook.sheetnames)
    try:
        for sheet in workbook.worksheets:
            rows: list[list[str]] = []
            dimensions = "A1"
            try:
                dimensions = str(sheet.calculate_dimension() or "A1")
            except Exception:  # noqa: BLE001
                dimensions = "A1"
            for row in sheet.iter_rows(values_only=True):
                values = [_text_value(value) for value in row]
                if not any(values):
                    continue
                cells += len(values)
                if cells > limits.max_cells:
                    break
                rows.append(values)
            blocks.append(
                DocumentBlock(
                    kind="sheet",
                    anchor=f"sheet={sheet.title}!{dimensions}",
                    text=f"工作表 {sheet.title}",
                    rows=rows,
                    meta={"sheet": sheet.title},
                )
            )
            if cells > limits.max_cells:
                break
    finally:
        try:
            workbook.close()
        except Exception:  # noqa: BLE001
            pass
    truncated = len(blocks) < sheet_count or cells > limits.max_cells
    return DocumentModel(
        format=suffix.lstrip("."),
        mime=MIME.get(suffix, "application/vnd.ms-excel"),
        blocks=blocks,
        sheets=len(blocks),
        truncated=truncated,
    )


def _xls(data: bytes, limits: DocumentLimits) -> DocumentModel:
    try:
        import xlrd
    except ImportError as exc:
        raise _missing("xlrd", ".xls") from exc
    try:
        book = xlrd.open_workbook(file_contents=data)
    except Exception as exc:  # noqa: BLE001
        raise DocumentError(f"XLS 打开失败：{type(exc).__name__}: {exc}") from exc

    blocks: list[DocumentBlock] = []
    cells = 0
    for sheet in book.sheets():
        rows: list[list[str]] = []
        for row_index in range(min(sheet.nrows, 2_000)):
            values = [
                _text_value(sheet.cell_value(row_index, col))
                for col in range(min(sheet.ncols, 200))
            ]
            if not any(values):
                continue
            cells += len(values)
            if cells > limits.max_cells:
                break
            rows.append(values)
        blocks.append(
            DocumentBlock(
                kind="sheet",
                anchor=f"sheet={sheet.name}!A1",
                text=f"工作表 {sheet.name}",
                rows=rows,
                meta={"sheet": sheet.name},
            )
        )
        if cells > limits.max_cells:
            break
    return DocumentModel(
        format="xls",
        mime=MIME[".xls"],
        blocks=blocks,
        sheets=len(blocks),
        truncated=cells > limits.max_cells or len(blocks) < book.nsheets,
    )


def _xml(text: Any) -> str:
    return (
        str(text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _slide_fill_svg(
    shape: Any, left: int, top: int, box_w: int, box_h: int
) -> str:
    try:
        fill = shape.fill
        if fill.type is None:
            return ""
        rgb = fill.fore_color.rgb
        if not rgb:
            return ""
        return (
            f'<rect x="{left}" y="{top}" width="{box_w}" '
            f'height="{box_h}" fill="#{rgb}"/>'
        )
    except Exception:  # noqa: BLE001
        return ""


def _slide_paragraph_svg(
    paragraph: Any, cursor_y: float, left: int, box_w: int
) -> tuple[str, float]:
    runs = [run for run in paragraph.runs if str(run.text or "")]
    text = "".join(str(run.text) for run in runs) or str(paragraph.text or "")
    if not text.strip():
        return "", 0.0
    first = runs[0] if runs else None
    size_pt = 18.0
    bold = False
    color = "111111"
    if first is not None:
        try:
            if first.font.size:
                size_pt = max(6.0, float(first.font.size.pt))
            bold = bool(first.font.bold)
            rgb = first.font.color.rgb if first.font.color else None
            if rgb:
                color = str(rgb)
        except Exception:  # noqa: BLE001
            pass
    size = size_pt * 12_700
    align = str(getattr(paragraph, "alignment", "") or "")
    anchor = "start"
    x_text = left + 12_700
    if "CENTER" in align:
        anchor = "middle"
        x_text = left + box_w / 2
    elif "RIGHT" in align:
        anchor = "end"
        x_text = left + box_w - 12_700
    svg = (
        f'<text x="{x_text:.1f}" y="{cursor_y + size:.1f}" '
        f'font-size="{size:.1f}" fill="#{color}" '
        f'font-family="Arial, sans-serif" '
        f'font-weight="{"700" if bold else "400"}" '
        f'text-anchor="{anchor}">{_xml(text)}</text>'
    )
    return svg, size * 1.2


def _slide_text_svg(
    shape: Any, left: int, top: int, box_w: int
) -> list[str]:
    if not getattr(shape, "has_text_frame", False):
        return []
    parts: list[str] = []
    cursor_y = top + 12_700
    for paragraph in list(shape.text_frame.paragraphs)[:100]:
        svg, advance = _slide_paragraph_svg(paragraph, cursor_y, left, box_w)
        if not svg:
            continue
        parts.append(svg)
        cursor_y += advance
    return parts


def _slide_table_svg(
    shape: Any, left: int, top: int, box_w: int, box_h: int
) -> list[str]:
    if not getattr(shape, "has_table", False):
        return []
    rows = list(shape.table.rows)
    if not rows:
        return []
    parts: list[str] = []
    columns = len(rows[0].cells)
    col_w = box_w / max(1, columns)
    row_h = box_h / max(1, len(rows))
    for r_index, row in enumerate(rows):
        for c_index, cell in enumerate(row.cells):
            cx = left + c_index * col_w
            cy = top + r_index * row_h
            parts.append(
                f'<rect x="{cx:.1f}" y="{cy:.1f}" '
                f'width="{col_w:.1f}" height="{row_h:.1f}" '
                'fill="none" stroke="#999999" stroke-width="1270"/>'
            )
            parts.append(
                f'<text x="{cx + 12700:.1f}" '
                f'y="{cy + row_h * 0.6:.1f}" '
                'font-size="152400" fill="#111111" '
                'font-family="Arial, sans-serif">'
                f"{_xml(cell.text)}</text>"
            )
    return parts


def _slide_image_svg(
    shape: Any, left: int, top: int, box_w: int, box_h: int
) -> str:
    try:
        blob = shape.image.blob
        if len(blob) > 2 * 1024 * 1024:
            return ""
        ext = str(shape.image.ext or "png")
        mime = f"image/{'jpeg' if ext in {'jpg', 'jpeg'} else ext}"
        encoded = base64.b64encode(blob).decode("ascii")
        return (
            f'<image x="{left}" y="{top}" width="{box_w}" '
            f'height="{box_h}" preserveAspectRatio="xMidYMid meet" '
            f'href="data:{mime};base64,{encoded}"/>'
        )
    except Exception:  # noqa: BLE001
        return ""


def _slide_svg(
    slide: Any,
    width: int,
    height: int,
    *,
    include_images: bool,
    max_images: int = 10,
) -> str:
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        'preserveAspectRatio="xMidYMid meet">',
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff"/>',
    ]
    image_count = 0
    for shape in slide.shapes:
        try:
            left = int(shape.left or 0)
            top = int(shape.top or 0)
            box_w = int(shape.width or 0)
            box_h = int(shape.height or 0)
        except Exception:  # noqa: BLE001
            continue
        rotation = float(getattr(shape, "rotation", 0) or 0)
        transform = (
            f' transform="rotate({rotation:.2f} {left + box_w / 2:.1f} '
            f'{top + box_h / 2:.1f})"'
            if rotation
            else ""
        )
        parts.append(f"<g{transform}>")
        fill = _slide_fill_svg(shape, left, top, box_w, box_h)
        if fill:
            parts.append(fill)
        parts.extend(_slide_text_svg(shape, left, top, box_w))
        parts.extend(_slide_table_svg(shape, left, top, box_w, box_h))
        if (
            include_images
            and image_count < max_images
            and hasattr(shape, "image")
        ):
            image = _slide_image_svg(shape, left, top, box_w, box_h)
            if image:
                parts.append(image)
                image_count += 1
        parts.append("</g>")
    parts.append("</svg>")
    return "".join(parts)


def _pptx_shape_content(
    slide: Any,
) -> tuple[list[str], list[list[list[str]]], int]:
    lines: list[str] = []
    tables: list[list[list[str]]] = []
    images = 0
    for shape in slide.shapes:
        if getattr(shape, "has_text_frame", False):
            text = str(shape.text_frame.text or "").strip()
            if text:
                lines.append(text)
        if getattr(shape, "has_table", False):
            rows: list[list[str]] = []
            for row in shape.table.rows:
                rows.append([_text_value(cell.text).strip() for cell in row.cells])
            tables.append(rows)
        if getattr(shape, "shape_type", None) == 13:  # PICTURE
            images += 1
    return lines, tables, images


def _pptx_notes(slide: Any) -> str:
    try:
        return str(slide.notes_slide.notes_text_frame.text or "").strip()
    except Exception:  # noqa: BLE001 - 备注不存在很常见
        return ""


def _pptx_slide_blocks(slide: Any, index: int) -> list[DocumentBlock]:
    blocks: list[DocumentBlock] = []
    lines, tables, images = _pptx_shape_content(slide)
    blocks.append(
        DocumentBlock(
            kind="slide",
            anchor=f"slide={index}",
            text="\n".join(lines) if lines else "（本页没有文本内容）",
            meta={"images": images},
        )
    )
    for table_index, rows in enumerate(tables, 1):
        blocks.append(
            DocumentBlock(
                kind="table",
                anchor=f"slide={index}/table={table_index}",
                text=f"幻灯片 {index} 的表格 {table_index}",
                rows=rows,
            )
        )
    notes = _pptx_notes(slide)
    if notes:
        blocks.append(
            DocumentBlock(
                kind="note",
                anchor=f"slide={index}/notes",
                text=notes,
            )
        )
    return blocks


def _pptx(data: bytes, limits: DocumentLimits) -> DocumentModel:
    try:
        from pptx import Presentation
    except ImportError as exc:
        raise _missing("python-pptx", ".pptx") from exc

    _check_zip(data, limits, ".pptx")
    try:
        presentation = Presentation(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        raise DocumentError(f"PPTX 打开失败：{type(exc).__name__}: {exc}") from exc

    blocks: list[DocumentBlock] = []
    slide_layouts: list[dict[str, Any]] = []
    slide_w = int(presentation.slide_width or 12_192_000)
    slide_h = int(presentation.slide_height or 6_858_000)
    total = len(presentation.slides)
    for index, slide in enumerate(presentation.slides, 1):
        if index > limits.max_pages:
            break
        if limits.pptx_layout:
            slide_layouts.append(
                {
                    "page": index,
                    "width": slide_w,
                    "height": slide_h,
                    "svg": _slide_svg(
                        slide,
                        slide_w,
                        slide_h,
                        include_images=limits.pptx_layout_images,
                    ),
                }
            )
        blocks.extend(_pptx_slide_blocks(slide, index))
    try:
        title = str(presentation.core_properties.title or "").strip()
    except Exception:  # noqa: BLE001
        title = ""
    return DocumentModel(
        format="pptx",
        mime=MIME[".pptx"],
        title=title,
        blocks=blocks,
        slide_layouts=slide_layouts,
        slides=total,
        truncated=total > limits.max_pages,
    )


def extract_pdf_attachments(data: bytes) -> list[tuple[str, bytes]]:
    """提取 PDF 内嵌附件。"""
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise _missing("pypdf", ".pdf") from exc
    try:
        reader = PdfReader(io.BytesIO(data))
        attachments = reader.attachments or {}
    except Exception as exc:  # noqa: BLE001
        raise DocumentError(
            f"PDF 附件读取失败：{type(exc).__name__}: {exc}"
        ) from exc
    rows: list[tuple[str, bytes]] = []
    for name, payloads in attachments.items():
        for index, payload in enumerate(payloads or []):
            suffix = f"-{index}" if len(payloads) > 1 else ""
            rows.append((f"{name}{suffix}", bytes(payload)))
    return rows


def _docx_embedded_images(
    data: bytes, max_images: int, max_image_bytes: int
) -> list[tuple[str, bytes]]:
    import docx

    images: list[tuple[str, bytes]] = []
    document = docx.Document(io.BytesIO(data))
    for index, rel in enumerate(document.part.rels.values(), 1):
        if "image" not in str(rel.reltype):
            continue
        blob = rel.target_part.blob
        if len(blob) > max_image_bytes:
            continue
        name = Path(str(rel.target_part.partname)).name or f"image-{index}.png"
        images.append((name, blob))
        if len(images) >= max_images:
            break
    return images


def _pptx_embedded_images(
    data: bytes, max_images: int, max_image_bytes: int
) -> list[tuple[str, bytes]]:
    from pptx import Presentation

    images: list[tuple[str, bytes]] = []
    presentation = Presentation(io.BytesIO(data))
    for slide in presentation.slides:
        for shape in slide.shapes:
            if not hasattr(shape, "image"):
                continue
            blob = shape.image.blob
            if len(blob) > max_image_bytes:
                continue
            ext = str(getattr(shape.image, "ext", "png") or "png")
            images.append((f"slide-image-{len(images) + 1}.{ext}", blob))
            if len(images) >= max_images:
                break
        if len(images) >= max_images:
            break
    return images


def extract_embedded_images(
    path: str,
    data: bytes,
    *,
    max_images: int = 20,
    max_image_bytes: int = 10 * 1024 * 1024,
) -> list[tuple[str, bytes]]:
    """提取 DOCX/PPTX 内嵌图片；其他格式返回空列表。"""
    suffix = _suffix(path)
    try:
        if suffix == ".docx":
            return _docx_embedded_images(data, max_images, max_image_bytes)
        if suffix == ".pptx":
            return _pptx_embedded_images(data, max_images, max_image_bytes)
    except Exception as exc:  # noqa: BLE001 - 图片提取失败不影响正文
        raise DocumentError(
            f"提取内嵌图片失败：{type(exc).__name__}: {exc}"
        ) from exc
    return []


def _parse_by_suffix(
    suffix: str, data: bytes, limits: DocumentLimits
) -> DocumentModel:
    if suffix in {".doc", ".ppt"}:
        raise UnsupportedFormat(f"暂不支持直接读取 {suffix}。{LEGACY_HINT}")
    if suffix == ".pdf":
        return _pdf(data, limits)
    if suffix == ".docx":
        return _docx(data, limits)
    if suffix in {".xlsx", ".xlsm"}:
        return _xlsx(data, suffix, limits)
    if suffix == ".xls":
        return _xls(data, limits)
    if suffix == ".pptx":
        return _pptx(data, limits)
    if suffix in CSV_SUFFIXES:
        text = data.decode("utf-8", errors="replace")
        blocks = _csv_blocks(text, "\t" if suffix == ".tsv" else ",")
        return DocumentModel(
            format=suffix.lstrip("."), mime=MIME[suffix], blocks=blocks
        )
    if suffix in TEXT_SUFFIXES:
        text = data.decode("utf-8", errors="replace")
        return DocumentModel(
            format=suffix.lstrip("."),
            mime=MIME.get(suffix, "text/plain"),
            blocks=_text_blocks(text),
        )
    raise UnsupportedFormat(
        f"暂不支持 {suffix or '无扩展名'} 的文档解析。"
        "支持 pdf、docx、xlsx、xls、pptx、md、csv 与常见文本格式。"
    )


def _finalize_model(
    model: DocumentModel,
    path: str,
    data: bytes,
    suffix: str,
    limits: DocumentLimits,
) -> None:
    blocks, truncated = _truncate(model.blocks, limits)
    model.blocks = blocks
    model.truncated = model.truncated or truncated
    model.path = str(path or "")
    model.size = len(data)
    model.sha256 = hashlib.sha256(data).hexdigest()
    model.mime = model.mime or MIME.get(suffix, "application/octet-stream")
    if not model.format:
        model.format = suffix.lstrip(".") or "text"
    if truncated:
        model.warnings.append(
            f"内容超过上限，已截断为 {len(blocks)} 个块；请用 offset/max_chars 分段读取。"
        )
    if model.needs_ocr and not any(
        "无文本层" in warning for warning in model.warnings
    ):
        model.warnings.append(
            "PDF 存在无文本层的页面，可能是扫描件；需要 OCR 或多模态图像理解。"
        )


def parse_document(
    path: str,
    data: bytes,
    *,
    limits: DocumentLimits | None = None,
) -> DocumentModel:
    """按扩展名解析文档；不支持的格式抛 `UnsupportedFormat`。"""
    limits = limits or DocumentLimits()
    if len(data) > limits.max_bytes:
        raise DocumentTooLarge(
            f"文件过大：{len(data)} 字节 > {limits.max_bytes} 字节"
        )
    suffix = _suffix(path)
    model = _parse_by_suffix(suffix, data, limits)
    _finalize_model(model, path, data, suffix, limits)
    return model
