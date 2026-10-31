"""document_read / 二进制解析。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ubismart.config import Settings
from ubismart.tools.artifacts import unpack
from ubismart.tools.documents import build_document_tools
from ubismart.tools.documents.extract import parse_document
from ubismart.tools.documents.models import (
    DocumentError,
    DocumentLimits,
    UnsupportedFormat,
)
from ubismart.tools.documents.tool import cleanup_document_cache


def _settings() -> Settings:
    return Settings(_env_file=None)


def _tool(root: Path):
    return build_document_tools(_settings(), root=str(root))[0]


def _invoke(tool, **kwargs) -> tuple[str, dict]:
    raw = tool.invoke(kwargs)
    text, artifact = unpack(raw)
    assert artifact is not None
    return text, artifact


def test_markdown_returns_heading_anchors(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("# 标题\n\n正文\n\n## 小节\n\n内容", encoding="utf-8")
    text, artifact = _invoke(
        _tool(tmp_path), path="a.md", reason="test", mode="auto"
    )
    assert artifact["ok"] is True
    assert artifact["kind"] == "document"
    assert "[heading=1]" in text
    assert "[heading=2]" in text


def test_docx_paragraphs_and_tables(tmp_path: Path) -> None:
    from docx import Document

    document = Document()
    document.add_heading("标题", level=1)
    document.add_paragraph("正文")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "A"
    table.cell(0, 1).text = "B"
    path = tmp_path / "a.docx"
    document.save(path)

    model = parse_document(str(path), path.read_bytes(), limits=DocumentLimits())
    anchors = [block.anchor for block in model.blocks]
    assert "paragraph=1" in anchors
    assert "table=1" in anchors
    text, artifact = _invoke(
        _tool(tmp_path), path="a.docx", reason="test", mode="auto"
    )
    assert artifact["ok"] is True
    assert "A | B" in text


def test_docx_headers_footers_and_notes(tmp_path: Path) -> None:
    import io
    import zipfile

    from docx import Document

    document = Document()
    document.add_paragraph("body")
    document.sections[0].header.paragraphs[0].text = "页眉内容"
    document.sections[0].footer.paragraphs[0].text = "页脚内容"
    path = tmp_path / "extras.docx"
    document.save(path)

    # 注入 comments.xml / footnotes.xml，模拟真实批注与脚注。
    source = path.read_bytes()
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(source)) as original:
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target:
            for info in original.infolist():
                target.writestr(info, original.read(info))
            target.writestr(
                "word/comments.xml",
                '<?xml version="1.0"?>'
                '<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                '<w:comment w:id="1" w:author="审阅人"><w:p><w:r>'
                '<w:t>这是一条批注</w:t></w:r></w:p></w:comment></w:comments>',
            )
            target.writestr(
                "word/footnotes.xml",
                '<?xml version="1.0"?>'
                '<w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                '<w:footnote w:id="1"><w:p><w:r>'
                '<w:t>这是一条脚注</w:t></w:r></w:p></w:footnote></w:footnotes>',
            )
    path.write_bytes(buffer.getvalue())

    model = parse_document("extras.docx", path.read_bytes(), limits=DocumentLimits())
    kinds = {block.kind for block in model.blocks}
    text = "\n".join(block.text for block in model.blocks)
    assert {"header", "footer", "section"} <= kinds
    assert "页眉内容" in text and "页脚内容" in text
    assert "这是一条批注" in text and "这是一条脚注" in text


def test_xlsx_sheet_anchor(tmp_path: Path) -> None:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "数据"
    sheet["A1"] = "名称"
    sheet["B1"] = "数量"
    sheet["A2"] = "苹果"
    sheet["B2"] = 3
    path = tmp_path / "a.xlsx"
    workbook.save(path)

    text, artifact = _invoke(
        _tool(tmp_path), path="a.xlsx", reason="test", mode="tables"
    )
    assert artifact["ok"] is True
    assert "sheet=数据!" in text
    assert "苹果 | 3" in text


def test_pptx_slide_and_notes(tmp_path: Path) -> None:
    from pptx import Presentation

    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[5])
    slide.shapes.title.text = "第一页"
    slide.notes_slide.notes_text_frame.text = "备注"
    path = tmp_path / "a.pptx"
    presentation.save(path)

    text, artifact = _invoke(
        _tool(tmp_path), path="a.pptx", reason="test", mode="outline"
    )
    assert artifact["ok"] is True
    assert "[slide=1]" in text
    assert "第一页" in text


def test_pdf_page_anchor(tmp_path: Path) -> None:
    from reportlab.pdfgen import canvas

    path = tmp_path / "a.pdf"
    pdf = canvas.Canvas(str(path))
    pdf.drawString(72, 720, "hello pdf")
    pdf.showPage()
    pdf.save()

    text, artifact = _invoke(
        _tool(tmp_path), path="a.pdf", reason="test", mode="auto"
    )
    assert artifact["ok"] is True
    assert "page=1" in text
    assert "hello pdf" in text


def test_docx_and_pptx_embedded_images(tmp_path: Path) -> None:
    from docx import Document
    from docx.shared import Inches
    from PIL import Image
    from pptx import Presentation

    icon = tmp_path / "icon.png"
    Image.new("RGB", (16, 16), (255, 0, 0)).save(icon)

    docx = Document()
    docx.add_paragraph("正文")
    docx.add_picture(str(icon), width=Inches(1))
    docx.save(tmp_path / "with-image.docx")

    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[5])
    slide.shapes.add_picture(str(icon), Inches(1), Inches(1))
    presentation.save(tmp_path / "with-image.pptx")

    tools = {
        tool.name: tool
        for tool in build_document_tools(_settings(), root=str(tmp_path))
    }
    for name in ("with-image.docx", "with-image.pptx"):
        text, artifact = unpack(
            tools["document_read"].invoke(
                {
                    "path": name,
                    "reason": "test",
                    "mode": "auto",
                    "include_images": True,
                }
            )
        )
        assert artifact and artifact["ok"] is True, text
        assert artifact["image_paths"], name
        image_path = artifact["image_paths"][0]
        assert (tmp_path / image_path).is_file()
        assert "image_inspect" in text


def test_pdf_metadata_and_text_layer_quality(tmp_path: Path) -> None:
    from reportlab.pdfgen import canvas

    path = tmp_path / "meta.pdf"
    pdf = canvas.Canvas(str(path))
    pdf.setTitle("测试标题")
    pdf.setAuthor("作者")
    pdf.drawString(72, 720, "hello")
    pdf.showPage()
    pdf.drawString(72, 720, "second page")
    pdf.showPage()
    pdf.save()
    model = parse_document(str(path), path.read_bytes(), limits=DocumentLimits())
    assert model.metadata["title"] == "测试标题"
    assert model.metadata["author"] == "作者"
    assert model.page_chars[0] >= 5
    assert model.page_chars[1] >= 11
    assert model.scanned_pages == []
    assert model.needs_ocr is False


def test_scanned_pdf_is_skipped_by_default_text_only_mode(tmp_path: Path) -> None:
    from reportlab.pdfgen import canvas

    path = tmp_path / "scan.pdf"
    pdf = canvas.Canvas(str(path))
    pdf.showPage()
    pdf.save()
    model = parse_document(str(path), path.read_bytes(), limits=DocumentLimits())
    assert model.scanned_pages == [1]
    assert model.needs_ocr is True
    assert any("仅处理文本型" in warning for warning in model.warnings)


def test_pdf_tables_are_extracted_from_text_layer(tmp_path: Path) -> None:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle

    path = tmp_path / "table.pdf"
    document = SimpleDocTemplate(str(path), pagesize=A4)
    table = Table([["Name", "Qty"], ["Apple", "3"]])
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 1, colors.black),
    ]))
    document.build([table])
    model = parse_document(str(path), path.read_bytes(), limits=DocumentLimits())
    anchors = [block.anchor for block in model.blocks if block.kind == "table"]
    assert "page=1/table=1" in anchors
    assert any(
        "Apple" in cell
        for block in model.blocks
        if block.kind == "table"
        for row in block.rows
        for cell in row
    )


class _FakeCacheFs:
    def __init__(self) -> None:
        self.removed: list[str] = []

    def lexical(self, path: str) -> str:
        return path

    def list_dir(self, path: str, *, max_depth: int = 1, limit: int = 200):
        if path.endswith(".agent/documents"):
            return [("d", "old"), ("d", "new")]
        return [("f", "extract.json")]

    def stat(self, path: str):
        class _Info:
            size = 10
            mtime = 1.0 if "old" in path else 9_999_999_999.0

        return _Info()

    def remove(self, path: str) -> bool:
        self.removed.append(path)
        return True


def test_document_cache_capacity_cleanup_removes_oldest() -> None:
    fs = _FakeCacheFs()
    removed = cleanup_document_cache(fs, max_bytes=10, ttl_days=0)
    assert removed == 1
    assert any("old" in path for path in fs.removed)


def test_document_cache_ttl_cleanup() -> None:
    fs = _FakeCacheFs()
    removed = cleanup_document_cache(fs, max_bytes=0, ttl_days=1)
    assert removed >= 1
    assert any("old" in path for path in fs.removed)


def test_pdf_attachments_are_extracted_and_parsed(tmp_path: Path) -> None:
    import io

    from docx import Document
    from pypdf import PdfWriter

    nested = Document()
    nested.add_paragraph("nested attachment text")
    nested_buffer = io.BytesIO()
    nested.save(nested_buffer)

    path = tmp_path / "with-attachment.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_attachment("note.txt", b"hello attachment")
    writer.add_attachment("nested.docx", nested_buffer.getvalue())
    with path.open("wb") as handle:
        writer.write(handle)

    tools = {
        tool.name: tool
        for tool in build_document_tools(_settings(), root=str(tmp_path))
    }
    text, artifact = unpack(
        tools["document_read"].invoke(
            {
                "path": "with-attachment.pdf",
                "reason": "test",
                "mode": "auto",
                "include_attachments": True,
            }
        )
    )
    assert artifact and artifact["ok"] is True, text
    assert artifact["attachments"] == 2
    assert "hello attachment" in text
    assert "nested attachment text" in text
    assert any(
        block.kind == "attachment"
        for block in parse_document(
            "with-attachment.pdf",
            path.read_bytes(),
            limits=DocumentLimits(),
        ).blocks
    )


def test_pptx_layout_svg_contains_text_and_image(tmp_path: Path) -> None:
    from PIL import Image
    from pptx import Presentation
    from pptx.util import Inches

    icon = tmp_path / "icon.png"
    Image.new("RGB", (8, 8), (0, 0, 255)).save(icon)
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[5])
    slide.shapes.title.text = "版式标题"
    slide.shapes.add_picture(str(icon), Inches(1), Inches(1))
    path = tmp_path / "layout.pptx"
    presentation.save(path)

    model = parse_document(
        "layout.pptx",
        path.read_bytes(),
        limits=DocumentLimits(pptx_layout=True, pptx_layout_images=True),
    )
    assert model.slide_layouts
    svg = model.slide_layouts[0]["svg"]
    assert "版式标题" in svg
    assert "<image" in svg


def test_pdf_page_image_renders_png(tmp_path: Path) -> None:
    from reportlab.pdfgen import canvas

    path = tmp_path / "a.pdf"
    pdf = canvas.Canvas(str(path))
    pdf.drawString(72, 720, "hello")
    pdf.showPage()
    pdf.save()
    tools = {
        tool.name: tool
        for tool in build_document_tools(_settings(), root=str(tmp_path))
    }
    text, artifact = unpack(
        tools["pdf_page_image"].invoke(
            {"path": "a.pdf", "page": 1, "reason": "test"}
        )
    )
    assert artifact and artifact["ok"] is True, text
    image_path = artifact["image_path"]
    assert image_path.startswith(".agent/documents/")
    assert (tmp_path / image_path).is_file()
    assert (tmp_path / image_path).read_bytes().startswith(b"\x89PNG")


def test_legacy_formats_are_rejected_with_actionable_hint(tmp_path: Path) -> None:
    path = tmp_path / "a.doc"
    path.write_bytes(b"\xd0\xcf\x11\xe0legacy")
    with pytest.raises(UnsupportedFormat) as exc:
        parse_document(str(path), path.read_bytes())
    assert "docx/pptx" in str(exc.value)
    text, artifact = _invoke(
        _tool(tmp_path), path="a.doc", reason="test", mode="auto"
    )
    assert artifact["ok"] is False
    assert "docx/pptx" in text


def test_disk_cache_is_written_and_reused(tmp_path: Path) -> None:
    content = "# 缓存测试\n\n唯一内容 cache-42\n"
    (tmp_path / "cache.md").write_text(content, encoding="utf-8")
    tool = _tool(tmp_path)
    first_text, first = _invoke(
        tool, path="cache.md", reason="test", mode="auto"
    )
    assert first["ok"] is True
    cache_files = list(
        (tmp_path / ".agent" / "documents").glob("*/extract.json")
    )
    assert cache_files, "expected .agent/documents/<sha>/extract.json"
    second_text, second = _invoke(
        tool, path="cache.md", reason="test", mode="auto"
    )
    assert second["sha256"] == first["sha256"]
    assert second["cached"] is True
    assert "唯一内容" in first_text and "唯一内容" in second_text


def test_zip_bomb_is_rejected(tmp_path: Path) -> None:
    import zipfile

    path = tmp_path / "bomb.docx"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "0" * (5 * 1024 * 1024))
    with pytest.raises(Exception) as exc:
        parse_document(str(path), path.read_bytes(), limits=DocumentLimits())
    assert "压缩比" in str(exc.value) or "过大" in str(exc.value)


def test_macro_and_ole_files_are_rejected(tmp_path: Path) -> None:
    macro = tmp_path / "macro.docm"
    macro.write_bytes(b"PK\x03\x04macro")
    with pytest.raises(UnsupportedFormat):
        parse_document(str(macro), macro.read_bytes())
    ole = tmp_path / "legacy.xls"
    ole.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1legacy")
    with pytest.raises(DocumentError):
        parse_document(str(ole), ole.read_bytes())


def test_pdf_javascript_is_not_executed(tmp_path: Path) -> None:
    from pypdf import PdfWriter

    path = tmp_path / "js.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_js("app.alert('should not run');")
    with path.open("wb") as handle:
        writer.write(handle)
    model = parse_document(str(path), path.read_bytes(), limits=DocumentLimits())
    assert model.pages == 1
    assert model.scanned_pages == [1]


def test_nested_zip_entry_is_not_extracted(tmp_path: Path) -> None:
    import io
    import zipfile

    from docx import Document

    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("secret.txt", "nested payload")
    base = tmp_path / "base.docx"
    document = Document()
    document.add_paragraph("body")
    document.save(base)
    path = tmp_path / "nested.docx"
    with zipfile.ZipFile(base) as original:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as target:
            for info in original.infolist():
                target.writestr(info, original.read(info))
            target.writestr("word/embedded.zip", inner.getvalue())
    model = parse_document(str(path), path.read_bytes(), limits=DocumentLimits())
    assert "nested payload" not in "\n".join(
        block.text for block in model.blocks
    )


def test_oversized_xml_part_is_rejected(tmp_path: Path) -> None:
    import zipfile

    path = tmp_path / "big-xml.docx"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<a>" + "x" * 5000 + "</a>")
    limits = DocumentLimits(max_xml_part_bytes=1000, max_zip_ratio=10_000)
    with pytest.raises(Exception) as exc:
        parse_document(str(path), path.read_bytes(), limits=limits)
    assert "XML 部分过大" in str(exc.value)


def test_xxe_document_is_rejected(tmp_path: Path) -> None:
    import zipfile

    path = tmp_path / "xxe.docx"
    malicious = (
        '<?xml version="1.0"?>'
        '<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        "<foo>&xxe;</foo>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", malicious)
    with pytest.raises(Exception) as exc:
        parse_document(str(path), path.read_bytes(), limits=DocumentLimits())
    assert "XML" in str(exc.value)


def test_secret_file_is_rejected(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("TOKEN=secret", encoding="utf-8")
    text, artifact = _invoke(
        _tool(tmp_path), path=".env", reason="test", mode="auto"
    )
    assert artifact["rejected"] is True
    assert "读保护" in text


def test_invalid_json_payload_not_confused_with_artifact(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text('{"text": "x"}', encoding="utf-8")
    text, artifact = _invoke(
        _tool(tmp_path), path="a.txt", reason="test", mode="auto"
    )
    assert artifact["ok"] is True
    assert json.loads(json.dumps(artifact))["format"] == "txt"
