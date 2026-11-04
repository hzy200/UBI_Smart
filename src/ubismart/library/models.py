"""资料库的稳定数据模型与错误类型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# 只允许文本型资料；表格按文本块索引。
LIBRARY_SUFFIXES = frozenset(
    {
        ".pdf",
        ".docx",
        ".md",
        ".markdown",
        ".txt",
        ".text",
        ".xlsx",
        ".xlsm",
        ".pptx",
        ".html",
        ".htm",
        ".epub",
        ".csv",
        ".tsv",
    }
)

STATUS_READY = "ready"
STATUS_FAILED = "failed"
STATUS_NEEDS_OCR = "needs_ocr"
STATUS_DELETED = "deleted"

ParserVersion = Literal[1]


class LibraryError(RuntimeError):
    """资料库操作失败；消息可直接回灌给用户/模型。"""


class SupportedFormatError(LibraryError):
    """格式不在允许列表内。"""


class LibraryTooLarge(LibraryError):
    """单文件或单用户总量超过上限。"""


class LibraryQuotaExceeded(LibraryError):
    """条目数超过上限。"""


class LibraryNotFound(LibraryError):
    """条目不存在或已删除。"""


@dataclass(slots=True)
class Chunk:
    chunk_id: str
    text: str
    anchor: str = ""
    heading: str = ""
    kind: str = "text"
    ordinal: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "text": self.text,
            "anchor": self.anchor,
            "heading": self.heading,
            "kind": self.kind,
            "ordinal": self.ordinal,
        }


@dataclass(slots=True)
class LibraryItem:
    item_id: str
    name: str
    suffix: str
    mime: str
    size: int
    sha256: str
    status: str
    chunk_count: int = 0
    title: str = ""
    parser_version: int = 1
    created_at: str = ""
    updated_at: str = ""
    error: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "name": self.name,
            "suffix": self.suffix,
            "mime": self.mime,
            "size": self.size,
            "sha256": self.sha256,
            "status": self.status,
            "chunk_count": self.chunk_count,
            "title": self.title,
            "parser_version": self.parser_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "error": self.error,
            "warnings": list(self.warnings),
        }
