"""文档解析的稳定数据模型。

工具文本是给人/模型看的；这里的结构化结果给事件、审计、报告引用与缓存使用。
锚点（anchor）是整条链路的证据坐标：`page=3`、`sheet=Sheet1!A1:F20`、
`slide=2`、`paragraph=12`、`heading=3` 等。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class DocumentError(ValueError):
    """文档不可读/不受支持。消息会直接回灌给模型，必须可操作。"""


class UnsupportedFormat(DocumentError):
    """格式不在支持矩阵内。"""


class DocumentTooLarge(DocumentError):
    """超过字节/页数/单元格等限制。"""


class DocumentBlock(BaseModel):
    kind: Literal[
        "heading", "text", "table", "sheet", "slide", "note", "image",
        "form", "attachment", "link", "header", "footer", "section",
    ] = "text"
    anchor: str = ""
    text: str = ""
    rows: list[list[str]] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)


class DocumentModel(BaseModel):
    path: str = ""
    format: str = ""
    mime: str = ""
    size: int = 0
    sha256: str = ""
    title: str = ""
    blocks: list[DocumentBlock] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    pages: int = 0
    sheets: int = 0
    slides: int = 0
    page_chars: list[int] = Field(default_factory=list)
    scanned_pages: list[int] = Field(default_factory=list)
    # PPTX 版式渲染：每页一个 SVG（绝对定位文本/图片/表格）。
    slide_layouts: list[dict[str, Any]] = Field(default_factory=list)
    needs_ocr: bool = False
    truncated: bool = False


class DocumentLimits(BaseModel):
    max_bytes: int = 20 * 1024 * 1024
    max_pages: int = 200
    max_cells: int = 20_000
    max_blocks: int = 2_000
    max_chars: int = 2_000_000
    max_zip_entries: int = 5_000
    max_zip_uncompressed: int = 200 * 1024 * 1024
    max_zip_ratio: int = 200
    max_xml_part_bytes: int = 8 * 1024 * 1024
    # PDF 结构化提取开关；默认只处理文本型 PDF。
    pdf_text_only: bool = True
    pdf_metadata: bool = True
    pdf_outline: bool = True
    pdf_annotations: bool = True
    pdf_forms: bool = True
    pdf_attachments: bool = True
    pdf_tables: bool = True
    max_tables: int = 100
    # PPTX 版式 SVG；图片是否内联由调用方按需开启。
    pptx_layout: bool = True
    pptx_layout_images: bool = False
