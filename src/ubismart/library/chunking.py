"""把 `DocumentModel` 切成带锚点的稳定 chunk。

切块必须保留来源坐标：PDF 的页、DOCX 的段落/标题、XLSX 的 sheet 与行范围。
模型看到的是片段，不是整篇；`anchor` 是后续引用与回溯的唯一坐标。
"""

from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING, Any

from ubismart.library.models import Chunk

if TYPE_CHECKING:
    from ubismart.tools.documents.models import DocumentBlock, DocumentModel

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def _heading_level(block: DocumentBlock) -> int:
    raw = block.meta.get("level") if isinstance(block.meta, dict) else None
    try:
        level = int(raw or 0)
    except (TypeError, ValueError):
        level = 0
    if level > 0:
        return min(level, 6)
    match = _HEADING_RE.match(str(block.text or ""))
    return len(match.group(1)) if match else 0


_SENTENCE_RE = re.compile(r"(?<=[。！？!?；;])")


def _hard_split(text: str, limit: int, overlap: int) -> list[str]:
    """最后兜底：按字符窗口切，块间保留少量重叠。"""
    parts: list[str] = []
    start = 0
    length = len(text)
    while start < length:
        end = min(length, start + limit)
        if end < length:
            floor = start + int(limit * 0.6)
            cut = text.rfind("\n", floor, end)
            if cut > start:
                end = cut
        part = text[start:end].strip()
        if part:
            parts.append(part)
        if end >= length:
            break
        start = max(end - overlap, start + 1)
    return parts


def _split_long_paragraph(text: str, limit: int, overlap: int) -> list[str]:
    """长段落优先按句号/question mark 切开；单句过长再硬切。"""
    sentences = [part for part in _SENTENCE_RE.split(text) if part.strip()]
    if len(sentences) <= 1:
        return _hard_split(text, limit, overlap)
    parts: list[str] = []
    current = ""
    for sentence in sentences:
        candidate = f"{current}{sentence}"
        if current and len(candidate) > limit:
            parts.append(current.strip())
            current = sentence
        else:
            current = candidate
        if len(current) > limit:
            parts.extend(_hard_split(current, limit, overlap))
            current = ""
    if current.strip():
        parts.append(current.strip())
    return parts


def _split_text(text: str, limit: int, overlap: int) -> list[str]:
    """按段落/句子切分，最后才按字符兜底。"""
    raw = str(text or "").strip()
    if not raw:
        return []
    limit = max(200, int(limit))
    overlap = max(0, min(int(overlap), limit // 3))
    if len(raw) <= limit:
        return [raw]

    paragraphs = [
        part.strip() for part in re.split(r"\n\s*\n", raw) if part.strip()
    ]
    parts: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(paragraph) > limit:
            if current:
                parts.append(current.strip())
                current = ""
            parts.extend(_split_long_paragraph(paragraph, limit, overlap))
            continue
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) > limit:
            if current:
                parts.append(current.strip())
            current = paragraph
        else:
            current = candidate
    if current.strip():
        parts.append(current.strip())
    return parts or _hard_split(raw, limit, overlap)


def _cell(value: Any) -> str:
    return str(value if value is not None else "").replace("\t", " ").strip()


def _table_text(header: list[str], rows: list[list[str]]) -> str:
    lines: list[str] = []
    if header:
        lines.append("\t".join(header))
    lines.extend("\t".join(row) for row in rows)
    return "\n".join(line for line in lines if line.strip())


def _table_chunks(
    block: DocumentBlock,
    *,
    max_chars: int,
) -> list[tuple[str, str]]:
    """返回 `(text, anchor_suffix)`；表头在每块重复。"""
    rows = [
        [_cell(value) for value in row]
        for row in (block.rows or [])
        if any(str(value).strip() for value in row)
    ]
    if not rows:
        text = str(block.text or "").strip()
        return [(text, "")] if text else []

    header = rows[0]
    body = rows[1:]
    if not body:
        return [(_table_text(header, []), "")]

    chunks: list[tuple[str, str]] = []
    start = 0
    while start < len(body):
        end = start
        while end < len(body):
            candidate = _table_text(header, body[start : end + 1])
            if end > start and len(candidate) > max_chars:
                break
            end += 1
        group = _table_text(header, body[start:end])
        if group:
            suffix = f";rows={start + 1}-{end}"
            chunks.append((group, suffix))
        start = max(end, start + 1)
    return chunks


def _prefix_heading(text: str, heading: str) -> str:
    if not heading:
        return text
    first = text.lstrip().splitlines()[0] if text.strip() else ""
    if first.strip() == heading:
        return text
    return f"{heading}\n{text}"


def chunk_document(
    model: DocumentModel,
    *,
    item_id: str = "",
    max_chars: int = 4_000,
    overlap: int = 200,
    max_chunks: int = 20_000,
) -> tuple[list[Chunk], list[dict[str, Any]]]:
    """把解析结果切成 chunk，并返回标题大纲。"""
    chunks: list[Chunk] = []
    outline: list[dict[str, Any]] = []
    heading_stack: list[tuple[int, str]] = []
    buffer: list[str] = []
    buffer_anchor = ""
    ordinal = 0

    def current_heading() -> str:
        return " > ".join(text for _, text in heading_stack if text)

    def add(text: str, anchor: str, kind: str = "text") -> None:
        nonlocal ordinal
        body = _prefix_heading(str(text or "").strip(), current_heading())
        if not body:
            return
        parts = _split_text(body, max_chars, overlap)
        for index, part in enumerate(parts, 1):
            if len(chunks) >= max_chunks:
                return
            part_anchor = anchor
            if len(parts) > 1:
                part_anchor = f"{anchor};part={index}" if anchor else f"part={index}"
            digest = hashlib.sha256(
                f"{item_id}:{part_anchor}:{ordinal}:{part[:64]}".encode()
            ).hexdigest()[:16]
            chunks.append(
                Chunk(
                    chunk_id=digest,
                    text=part,
                    anchor=part_anchor,
                    heading=current_heading(),
                    kind=kind,
                    ordinal=ordinal,
                )
            )
            ordinal += 1

    def flush() -> None:
        nonlocal buffer, buffer_anchor
        if not buffer:
            return
        add("\n\n".join(buffer), buffer_anchor)
        buffer = []
        buffer_anchor = ""

    for block in model.blocks or []:
        kind = str(block.kind or "text")
        if kind == "heading":
            flush()
            text = str(block.text or "").strip()
            if not text:
                continue
            level = _heading_level(block) or 1
            heading_stack = heading_stack[: max(0, level - 1)]
            heading_stack.append((level, text))
            outline.append(
                {
                    "text": text,
                    "anchor": block.anchor,
                    "level": level,
                }
            )
            continue

        if kind == "slide":
            flush()
            text = str(block.text or "").strip()
            if text:
                add(text, block.anchor)
            continue

        if kind == "section":
            flush()
            text = str(block.text or "").strip()
            if text:
                heading_stack = [(1, text)]
                outline.append(
                    {"text": text, "anchor": block.anchor, "level": 1}
                )
            continue

        if kind in {"table", "sheet"} or (block.rows and kind != "text"):
            flush()
            for text, suffix in _table_chunks(
                block, max_chars=max_chars
            ):
                anchor = f"{block.anchor}{suffix}" if block.anchor else suffix
                if kind == "sheet" and block.text:
                    text = f"{block.text}\n{text}"
                add(text, anchor, kind="table")
            continue

        text = str(block.text or "").strip()
        if not text:
            continue
        if buffer and len("\n\n".join(buffer)) + len(text) + 2 > max_chars:
            flush()
        if not buffer:
            buffer_anchor = block.anchor
        buffer.append(text)
        if len(text) > max_chars:
            flush()

    flush()
    return chunks, outline
