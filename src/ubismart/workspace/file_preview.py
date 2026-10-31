"""文件预览的二进制/结构化读取。

与 `browse.read_text_file` 的分工：
- `read_text_file` 继续服务文本文件与 2MB 内图片预览；
- 本模块服务 PDF/Office viewer 与 `document_read`，返回原始字节或 DocumentModel。

所有入口都要求调用方传入 `FileAccess`，这样 web(A2)、agent_server(A3) 与
host(A2L) 共用同一套路径守卫和秘密文件保护。
"""

from __future__ import annotations

import base64
import io
import mimetypes
import posixpath
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ubismart.execution.file_access import FileAccess
from ubismart.security.read_policy import protected_read_reason

RAW_MAX_BYTES = 20 * 1024 * 1024
_IMAGE_MIME = {
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
    b"GIF87a": "image/gif",
    b"GIF89a": "image/gif",
    b"BM": "image/bmp",
}
_OFFICE_MIME = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
INLINE_MIME_ALLOWLIST = frozenset(
    {
        "application/pdf",
        *_OFFICE_MIME.values(),
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/bmp",
        "text/plain",
        "text/markdown",
        "text/csv",
        "text/tab-separated-values",
        "application/json",
    }
)


class PreviewError(ValueError):
    """预览不可读/不可内联。消息会转成 HTTP 422/415。"""


def document_limits(settings: Any) -> Any:
    """从配置构造解析上限；web 层不直接依赖 tools 包。"""
    from ubismart.tools.documents import DocumentLimits

    return DocumentLimits(
        max_bytes=int(
            getattr(settings, "document_max_bytes", 20 * 1024 * 1024)
        ),
        max_pages=int(getattr(settings, "document_max_pages", 200)),
        max_cells=int(getattr(settings, "document_max_cells", 20_000)),
        max_chars=int(getattr(settings, "document_max_chars", 2_000_000)),
    )


@dataclass(frozen=True)
class RawFile:
    data: bytes
    mime: str
    size: int
    filename: str
    path: str


def _display(access: FileAccess, target: str) -> str:
    try:
        return access.relative_to_root(target)
    except Exception:  # noqa: BLE001 - 展示失败不影响读取
        return str(target)


def _protected(access: FileAccess, target: str, info: Any) -> str | None:
    display = _display(access, target)
    reason = protected_read_reason(display)
    if reason:
        return reason
    real_path = (
        str(info.get("real_path") or "")
        if isinstance(info, dict)
        else str(getattr(info, "real_path", "") or "")
    )
    if real_path:
        return protected_read_reason(real_path)
    return None


def list_session_uploads(
    access: FileAccess,
    thread_id: str,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """列出本会话上传文件（不扫描其他会话）。只返回元数据。"""
    safe = re.sub(r"[^A-Za-z0-9_-]", "", str(thread_id or ""))[:64]
    if not safe:
        return []
    base = f".agent/uploads/{safe}"
    try:
        entries = access.list_dir(base, max_depth=1, limit=limit)
    except Exception:  # noqa: BLE001 - 没有上传目录是常态
        return []
    rows: list[dict[str, Any]] = []
    for kind, rel in entries:
        if kind != "f":
            continue
        full = (
            rel
            if rel == base or rel.startswith(base + "/")
            else f"{base}/{rel}"
        )
        try:
            info = access.stat(full)
            size = (
                int(info.get("size") or 0)
                if isinstance(info, dict)
                else int(getattr(info, "size", 0) or 0)
            )
        except Exception:  # noqa: BLE001
            size = 0
        rows.append(
            {
                "path": full,
                "name": posixpath.basename(full),
                "size": size,
                "suffix": Path(full).suffix.lower(),
            }
        )
        if len(rows) >= limit:
            break
    return rows


def sniff_mime(path: str, data: bytes) -> str:
    """优先魔数，其次扩展名；无法识别时 octet-stream。"""
    if data.startswith(b"%PDF"):
        return "application/pdf"
    for magic, mime in _IMAGE_MIME.items():
        if data.startswith(magic):
            return mime
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    suffix = Path(str(path or "")).suffix.lower()
    if suffix in _OFFICE_MIME and zipfile.is_zipfile(io.BytesIO(data)):
        return _OFFICE_MIME[suffix]
    guessed = mimetypes.guess_type(str(path or ""))[0]
    if guessed:
        return guessed
    if b"\x00" in data[:8192]:
        return "application/octet-stream"
    return "text/plain"


def read_raw(
    access: FileAccess,
    path: str,
    *,
    max_bytes: int = RAW_MAX_BYTES,
    allow_mime: frozenset[str] | None = None,
) -> RawFile:
    try:
        target = access.lexical(path)
    except Exception as exc:  # noqa: BLE001
        raise PreviewError(f"路径被拒绝：{exc}") from exc
    try:
        info = access.stat(target)
    except Exception as exc:  # noqa: BLE001
        raise PreviewError(f"无法解析文件：{exc}") from exc
    reason = _protected(access, target, info)
    if reason:
        raise PreviewError(reason)
    is_file = (
        bool(info.get("is_file"))
        if isinstance(info, dict)
        else bool(getattr(info, "is_file", False))
    )
    if not is_file:
        raise PreviewError("不是普通文件")
    try:
        data = access.read_bytes(target, max_bytes=max_bytes)
    except Exception as exc:  # noqa: BLE001
        raise PreviewError(f"读取失败：{type(exc).__name__}: {exc}") from exc
    mime = sniff_mime(path, data)
    if allow_mime is not None and mime not in allow_mime:
        raise PreviewError(f"不支持的预览类型：{mime}")
    return RawFile(
        data=data,
        mime=mime,
        size=len(data),
        filename=posixpath.basename(str(path or "")),
        path=_display(access, target),
    )


def read_document(
    access: FileAccess,
    path: str,
    *,
    limits: Any | None = None,
    include_images: bool = False,
) -> Any:
    from ubismart.tools.documents import DocumentLimits, parse_document

    limits = limits or DocumentLimits()
    limits.pptx_layout_images = bool(include_images)
    try:
        target = access.lexical(path)
    except Exception as exc:  # noqa: BLE001
        raise PreviewError(f"路径被拒绝：{exc}") from exc
    try:
        info = access.stat(target)
    except Exception as exc:  # noqa: BLE001
        raise PreviewError(f"无法解析文件：{exc}") from exc
    reason = _protected(access, target, info)
    if reason:
        raise PreviewError(reason)
    is_file = (
        bool(info.get("is_file"))
        if isinstance(info, dict)
        else bool(getattr(info, "is_file", False))
    )
    if not is_file:
        raise PreviewError("不是普通文件")
    display = _display(access, target)
    try:
        data = access.read_bytes(target, max_bytes=limits.max_bytes)
        model = parse_document(display, data, limits=limits)
        if include_images and Path(display).suffix.lower() in {".pptx", ".docx"}:
            from ubismart.tools.documents import DocumentBlock
            from ubismart.tools.documents.extract import (
                extract_embedded_images,
            )

            for index, (name, blob) in enumerate(
                extract_embedded_images(display, data)[:5], 1
            ):
                if len(blob) > 2 * 1024 * 1024:
                    continue
                mime = sniff_mime(name, blob)
                if not mime.startswith("image/"):
                    continue
                encoded = base64.b64encode(blob).decode("ascii")
                model.blocks.append(
                    DocumentBlock(
                        kind="image",
                        anchor=f"image={index}",
                        text=f"图片 {index}",
                        meta={
                            "name": name,
                            "data_url": f"data:{mime};base64,{encoded}",
                        },
                    )
                )
        return model
    except PreviewError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PreviewError(f"{type(exc).__name__}: {exc}") from exc


def raw_response_parts(
    raw: RawFile,
    range_header: str = "",
    if_none_match: str = "",
) -> tuple[int, dict[str, str], bytes]:
    """构造 raw 端点响应：支持单段 Range 与 ETag 条件请求。"""
    import hashlib

    etag = '"' + hashlib.sha256(raw.data).hexdigest()[:24] + '"'
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": f'inline; filename="{raw.filename}"',
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "ETag": etag,
    }
    if if_none_match and if_none_match.strip() == etag:
        headers.pop("Content-Disposition", None)
        return 304, headers, b""
    parsed = parse_range(range_header, raw.size)
    if parsed is None:
        headers["Content-Length"] = str(raw.size)
        return 200, headers, raw.data
    start, end = parsed
    body = raw.data[start : end + 1]
    headers["Content-Length"] = str(len(body))
    headers["Content-Range"] = f"bytes {start}-{end}/{raw.size}"
    return 206, headers, body


def parse_range(header: str, total: int) -> tuple[int, int] | None:
    """解析单段 `Range: bytes=start-end`；不合法返回 None（按整段返回）。"""
    text = str(header or "").strip()
    if not text.startswith("bytes="):
        return None
    part = text[len("bytes=") :].split(",", 1)[0].strip()
    if "-" not in part or total <= 0:
        return None
    left, _, right = part.partition("-")
    try:
        if left:
            start = int(left)
            end = int(right) if right else total - 1
        else:
            length = int(right)
            if length <= 0:
                return None
            start = max(0, total - length)
            end = total - 1
    except ValueError:
        return None
    if start < 0 or start >= total or end < start:
        return None
    return start, min(end, total - 1)
