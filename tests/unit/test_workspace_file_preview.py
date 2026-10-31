"""raw/document 预览读取与 Range。"""

from __future__ import annotations

from pathlib import Path

import pytest

from ubismart.execution.file_access import LocalFileAccess
from ubismart.workspace.file_preview import (
    PreviewError,
    list_session_uploads,
    raw_response_parts,
    read_document,
    read_raw,
    sniff_mime,
)


def test_read_raw_and_range(tmp_path: Path) -> None:
    path = tmp_path / "a.md"
    path.write_text("# hi\n\ntext", encoding="utf-8")
    access = LocalFileAccess(str(tmp_path))
    raw = read_raw(access, "a.md")
    assert raw.mime == "text/markdown"
    assert raw.filename == "a.md"
    status, headers, body = raw_response_parts(raw, "bytes=0-1")
    assert status == 206
    assert body == b"# "
    assert headers["Content-Range"] == f"bytes 0-1/{raw.size}"
    etag = headers["ETag"]
    status, headers, body = raw_response_parts(raw, "", etag)
    assert status == 304
    assert body == b""


def test_read_document_returns_blocks(tmp_path: Path) -> None:
    path = tmp_path / "a.md"
    path.write_text("# 标题\n\n正文", encoding="utf-8")
    model = read_document(LocalFileAccess(str(tmp_path)), "a.md")
    assert model.format == "md"
    assert model.blocks[0].anchor == "heading=1"


def test_read_document_can_embed_pptx_images(tmp_path: Path) -> None:
    from PIL import Image
    from pptx import Presentation
    from pptx.util import Inches

    icon = tmp_path / "icon.png"
    Image.new("RGB", (8, 8), (0, 255, 0)).save(icon)
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[5])
    slide.shapes.add_picture(str(icon), Inches(1), Inches(1))
    presentation.save(tmp_path / "with-image.pptx")

    model = read_document(
        LocalFileAccess(str(tmp_path)),
        "with-image.pptx",
        include_images=True,
    )
    images = [block for block in model.blocks if block.kind == "image"]
    assert images
    assert images[0].meta["data_url"].startswith("data:image/")


def test_path_traversal_is_rejected(tmp_path: Path) -> None:
    access = LocalFileAccess(str(tmp_path))
    with pytest.raises(PreviewError):
        read_raw(access, "../outside.txt")


def test_secret_file_is_rejected(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("TOKEN=x", encoding="utf-8")
    with pytest.raises(PreviewError) as exc:
        read_raw(LocalFileAccess(str(tmp_path)), ".env")
    assert "读保护" in str(exc.value)


def test_sniff_mime_by_magic(tmp_path: Path) -> None:
    assert sniff_mime("x.bin", b"%PDF-1.7") == "application/pdf"
    assert sniff_mime("x.bin", b"\x89PNG\r\n\x1a\nrest") == "image/png"


def test_list_session_uploads_is_scoped_to_thread(tmp_path: Path) -> None:
    uploads = tmp_path / ".agent" / "uploads" / "t1"
    uploads.mkdir(parents=True)
    (uploads / "a.pdf").write_bytes(b"%PDF-1.7")
    (uploads / "b.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    other = tmp_path / ".agent" / "uploads" / "t2"
    other.mkdir(parents=True)
    (other / "secret.txt").write_text("x", encoding="utf-8")

    rows = list_session_uploads(LocalFileAccess(str(tmp_path)), "t1")
    assert {row["name"] for row in rows} == {"a.pdf", "b.png"}
    assert all(row["path"].startswith(".agent/uploads/t1/") for row in rows)
    assert list_session_uploads(LocalFileAccess(str(tmp_path)), "") == []
