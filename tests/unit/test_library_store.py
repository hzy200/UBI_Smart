"""资料库存储/摄取/检索的单元测试（不依赖 WSL）。"""

from __future__ import annotations

import posixpath
import time
from pathlib import Path
from typing import Any

import pytest

from ubismart.library.models import (
    LibraryError,
    LibraryNotFound,
    LibraryQuotaExceeded,
    LibraryTooLarge,
    SupportedFormatError,
)
from ubismart.library.root import resolve_library_root
from ubismart.library.store import CATALOG_MAX_BYTES, LibraryStore
from ubismart.memory.embeddings import HashEmbedder


class _Stat:
    def __init__(self, path: str, *, exists: bool, is_file: bool, size: int = 0):
        self.exists = exists
        self.is_file = is_file
        self.size = size
        self.real_path = path
        self.mtime = time.time()


class MemoryFs:
    """只实现 LibraryStore 用到的 SandboxFs 子集。"""

    def __init__(self, root: str = "/lib") -> None:
        self.root = posixpath.normpath(root)
        self.files: dict[str, bytes] = {}
        self.dirs: set[str] = {self.root}

    @staticmethod
    def _norm(path: str) -> str:
        return posixpath.normpath(path)

    def _parents(self, path: str) -> None:
        current = posixpath.dirname(path)
        while current and current not in self.dirs:
            self.dirs.add(current)
            current = posixpath.dirname(current)

    def mkdir(self, path: str) -> str:
        path = self._norm(path)
        self.dirs.add(path)
        self._parents(path)
        return path

    def mkdir_exclusive(self, path: str) -> bool:
        path = self._norm(path)
        if path in self.dirs or path in self.files:
            return False
        self.dirs.add(path)
        self._parents(path)
        return True

    def rmdir(self, path: str) -> bool:
        path = self._norm(path)
        if path not in self.dirs:
            return False
        prefix = path.rstrip("/") + "/"
        if any(item.startswith(prefix) for item in self.files):
            raise OSError("目录非空")
        if any(
            item != path and item.startswith(prefix) for item in self.dirs
        ):
            raise OSError("目录非空")
        self.dirs.discard(path)
        return True

    def write_bytes(self, path: str, data: bytes) -> int:
        path = self._norm(path)
        self._parents(path)
        self.files[path] = bytes(data)
        return len(data)

    def write_text(self, path: str, content: str) -> int:
        return self.write_bytes(path, content.encode())

    def read_bytes(
        self, path: str, *, max_bytes: int, use_cache: bool = True
    ) -> bytes:
        del use_cache
        path = self._norm(path)
        if path not in self.files:
            raise FileNotFoundError(path)
        data = self.files[path]
        if len(data) > max_bytes:
            raise ValueError("too large")
        return data

    def read_text(
        self, path: str, *, max_bytes: int, use_cache: bool = True
    ) -> str:
        return self.read_bytes(
            path, max_bytes=max_bytes, use_cache=use_cache
        ).decode()

    def stat(self, path: str) -> _Stat:
        path = self._norm(path)
        if path in self.files:
            return _Stat(path, exists=True, is_file=True, size=len(self.files[path]))
        if path in self.dirs:
            return _Stat(path, exists=True, is_file=False)
        return _Stat(path, exists=False, is_file=False)

    def list_dir(
        self, path: str, *, max_depth: int = 1, limit: int = 200
    ) -> list[tuple[str, str]]:
        path = self._norm(path)
        if path not in self.dirs:
            raise FileNotFoundError(path)
        prefix = path.rstrip("/") + "/"
        rows: list[tuple[str, str]] = []
        for item in sorted(self.files):
            if item.startswith(prefix):
                rel = item[len(prefix) :]
                if rel.count("/") + 1 <= max_depth:
                    rows.append(("f", rel))
        for item in sorted(self.dirs):
            if item.startswith(prefix):
                rel = item[len(prefix) :]
                if rel.count("/") + 1 <= max_depth:
                    rows.append(("d", rel))
        return rows[:limit]

    def remove(self, path: str, *, known_file: bool = False) -> bool:
        del known_file
        path = self._norm(path)
        return self.files.pop(path, None) is not None

    def rename(self, src: str, dst: str) -> None:
        src = self._norm(src)
        dst = self._norm(dst)
        if src not in self.files:
            raise FileNotFoundError(src)
        self._parents(dst)
        self.files[dst] = self.files.pop(src)


def _store(**kwargs: Any) -> tuple[MemoryFs, LibraryStore]:
    fs = MemoryFs()
    return fs, LibraryStore(fs, "/lib", **kwargs)


def test_markdown_ingest_chunks_catalog_and_search() -> None:
    fs, store = _store()
    data = (
        "# 项目规范\n\n接口必须使用 HTTPS。\n\n"
        "## 数据表\n\n名称\t数量\n苹果\t3\n香蕉\t5\n"
    ).encode()
    item = store.ingest("spec.md", data)

    assert item["status"] == "ready"
    assert item["chunk_count"] >= 2
    assert "/lib/catalog.json" in fs.files
    chunks = fs.files[f"/lib/items/{item['item_id']}/chunks.jsonl"].decode()
    assert "anchor" in chunks and "heading" in chunks

    hits = store.search("HTTPS 接口", top_k=5)
    assert hits
    assert hits[0]["item_id"] == item["item_id"]
    assert hits[0]["anchor"]


def test_xlsx_ingest_keeps_sheet_anchor(tmp_path: Path) -> None:
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

    _, store = _store()
    item = store.ingest("a.xlsx", path.read_bytes())
    assert item["status"] == "ready"
    hits = store.search("苹果")
    assert hits
    assert "sheet=数据" in hits[0]["anchor"]


def test_pdf_ingest_keeps_page_anchor(tmp_path: Path) -> None:
    from reportlab.pdfgen import canvas

    path = tmp_path / "a.pdf"
    pdf = canvas.Canvas(str(path))
    pdf.drawString(72, 720, "hello pdf library")
    pdf.showPage()
    pdf.save()

    _, store = _store()
    item = store.ingest("a.pdf", path.read_bytes())
    assert item["status"] == "ready"
    hits = store.search("hello")
    assert hits
    assert "page=1" in hits[0]["anchor"]


def test_duplicate_quota_and_format_limits() -> None:
    _, store = _store(max_items=1, max_total_bytes=1_000)
    data = b"# title\n\nbody"
    first = store.ingest("a.md", data)
    duplicate = store.ingest("copy.md", data)
    assert duplicate["duplicate"] is True
    assert duplicate["item_id"] == first["item_id"]

    with pytest.raises(LibraryQuotaExceeded):
        store.ingest("b.md", b"# other\n\nbody")

    with pytest.raises(SupportedFormatError):
        store.ingest("a.exe", b"binary")

    _, small = _store(max_total_bytes=5)
    with pytest.raises(LibraryTooLarge):
        small.ingest("a.md", b"# too large")


def test_delete_soft_and_purge_reindex() -> None:
    fs, store = _store()
    item = store.ingest("a.md", b"# title\n\nneedle")
    item_id = item["item_id"]
    assert store.search("needle")

    store.delete(item_id)
    assert store.search("needle") == []
    with pytest.raises(LibraryNotFound):
        store.reindex(item_id)

    other = store.ingest("b.md", b"# title\n\nsecond")
    store.delete(other["item_id"], purge=True)
    assert f"/lib/items/{other['item_id']}/source.bin" not in fs.files


def test_purge_after_soft_delete_removes_files_and_metadata() -> None:
    fs, store = _store()
    item = store.ingest("a.md", b"# title\n\nneedle")
    item_id = item["item_id"]
    source = f"/lib/items/{item_id}/source.bin"

    store.delete(item_id)
    assert source in fs.files

    store.delete(item_id, purge=True)
    assert source not in fs.files
    assert store.get(item_id) is None


def test_purge_waits_for_the_active_job_then_removes_files() -> None:
    fs, store = _store()
    started = store.start_ingest("a.md", b"# title\n\nneedle")
    item_id = started["item"]["item_id"]
    job_id = started["job"]["job_id"]
    source = f"/lib/items/{item_id}/source.bin"

    store.delete(item_id, purge=True)
    # 任务仍在跑：只登记清除意图，不能和正在写 chunks 的 worker 竞态。
    assert source in fs.files
    assert store.get(item_id)["purge_requested"] is True

    store.process_job(job_id)
    assert source not in fs.files
    assert store.get(item_id) is None


def test_reindex_failure_preserves_old_index_and_ready_status(
    monkeypatch,
) -> None:
    fs, store = _store()
    item = store.ingest("a.md", b"# title\n\nold needle")
    item_id = item["item_id"]
    chunks_path = f"/lib/items/{item_id}/chunks.jsonl"
    before = fs.files[chunks_path]

    def _broken_parse(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise ValueError("parse failed")

    monkeypatch.setattr(
        "ubismart.tools.documents.extract.parse_document",
        _broken_parse,
    )

    result = store.reindex(item_id)

    assert result["status"] == "ready"
    assert result["job"]["status"] == "failed"
    assert fs.files[chunks_path] == before
    assert store.search("old needle")


def test_reindex_commit_failure_preserves_old_chunks() -> None:
    fs, store = _store()
    item = store.ingest("a.md", b"# title\n\nold needle")
    item_id = item["item_id"]
    chunks_path = f"/lib/items/{item_id}/chunks.jsonl"
    before = fs.files[chunks_path]
    original = fs.rename

    def _flaky_rename(src: str, dst: str) -> None:
        if dst.endswith("chunks.jsonl"):
            raise OSError("disk full")
        original(src, dst)

    fs.rename = _flaky_rename  # type: ignore[method-assign]

    result = store.reindex(item_id)

    assert result["status"] == "ready"
    assert result["job"]["status"] == "failed"
    assert fs.files[chunks_path] == before
    assert store.search("old needle")
    assert not any(".stage-" in path for path in fs.files)


def test_cancelled_job_is_terminal_against_late_updates() -> None:
    _fs, store = _store()
    started = store.start_ingest("a.md", b"# title\n\nneedle")
    item_id = started["item"]["item_id"]
    job_id = started["job"]["job_id"]

    store.cancel_job(job_id)
    late = store._update_job(job_id, status="ready", progress=100)

    assert late["status"] == "cancelled"
    assert store.process_job(job_id)["status"] == "cancelled"
    assert store.get(item_id)["status"] == "cancelled"


def test_reindex_updates_chunks() -> None:
    fs, store = _store()
    item = store.ingest("a.md", b"# title\n\nold needle")
    item_id = item["item_id"]
    # 直接改源文件，模拟源文件被外部替换后重建。
    fs.files[f"/lib/items/{item_id}/source.bin"] = (
        b"# title\n\nnew needle"
    )
    updated = store.reindex(item_id)
    assert updated["status"] == "ready"
    assert store.search("new")
    assert not store.search("old")


def test_user_roots_are_isolated() -> None:
    fs_a = MemoryFs("/lib/a")
    fs_b = MemoryFs("/lib/b")
    store_a = LibraryStore(fs_a, "/lib/a")
    store_b = LibraryStore(fs_b, "/lib/b")
    item = store_a.ingest("a.md", b"# title\n\nsecret needle")
    assert store_a.search("needle")
    assert store_b.search("needle") == []
    assert store_b.get(item["item_id"]) is None


def test_vector_and_hybrid_search_use_sidecar() -> None:
    fs = MemoryFs("/lib")
    embedder = HashEmbedder(64)
    store = LibraryStore(
        fs,
        "/lib",
        search_mode="vector",
        embedder=embedder,
    )
    alpha = store.ingest("alpha.md", b"# alpha\n\nalpha alpha alpha")
    store.ingest("beta.md", b"# beta\n\nbeta beta beta")
    vectors_path = f"/lib/items/{alpha['item_id']}/vectors.json"
    assert vectors_path in fs.files

    vector_hits = store.search("alpha", mode="vector")
    assert vector_hits
    assert vector_hits[0]["item_id"] == alpha["item_id"]
    assert vector_hits[0]["mode"] == "vector"
    assert "vector" not in vector_hits[0]

    hybrid_hits = store.search("alpha", mode="hybrid")
    assert hybrid_hits
    assert hybrid_hits[0]["item_id"] == alpha["item_id"]


def test_vector_mode_falls_back_to_lexical_without_embedder() -> None:
    _, store = _store()
    item = store.ingest("a.md", b"# title\n\nalpha beta")
    hits = store.search("alpha", mode="vector")
    assert hits
    assert hits[0]["item_id"] == item["item_id"]
    assert hits[0]["mode"] == "lexical"


def test_html_pptx_and_epub_ingest(tmp_path: Path) -> None:
    import io
    import zipfile

    from pptx import Presentation
    from pptx.util import Inches

    _, store = _store()

    html = "<h1>HTML 规范</h1><p>HTTPS 必须启用 TLS。</p>".encode()
    html_item = store.ingest("spec.html", html)
    assert html_item["status"] == "ready"
    assert store.search("TLS")

    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[5])
    slide.shapes.title.text = "PPTX 标题"
    textbox = slide.shapes.add_textbox(
        Inches(1), Inches(1), Inches(4), Inches(1)
    )
    textbox.text = "幻灯片里的接口约定"
    pptx_path = tmp_path / "deck.pptx"
    presentation.save(pptx_path)
    pptx_data = pptx_path.read_bytes()
    pptx_path.unlink()
    pptx_item = store.ingest("deck.pptx", pptx_data)
    assert pptx_item["status"] == "ready"
    assert store.search("接口约定")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?>'
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
        archive.writestr(
            "OEBPS/content.opf",
            '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
            '<manifest><item id="c1" href="c1.xhtml" '
            'media-type="application/xhtml+xml"/></manifest>'
            '<spine><itemref idref="c1"/></spine></package>',
        )
        archive.writestr(
            "OEBPS/c1.xhtml",
            "<html><body><h1>EPUB 章节</h1><p>电子书正文。</p></body></html>",
        )
    epub_item = store.ingest("book.epub", buffer.getvalue())
    assert epub_item["status"] == "ready"
    assert store.search("电子书正文")


def test_semantic_chunking_splits_on_sentences() -> None:
    from ubismart.library.chunking import chunk_document
    from ubismart.tools.documents.models import DocumentBlock, DocumentModel

    text = "甲" * 120 + "。" + "乙" * 120 + "。"
    model = DocumentModel(
        blocks=[DocumentBlock(kind="text", anchor="line=1", text=text)]
    )
    chunks, _ = chunk_document(model, max_chars=200, overlap=0)
    assert len(chunks) >= 2
    assert all(chunk.text.endswith("。") for chunk in chunks)


def test_catalog_v1_migrates_to_v2_on_write() -> None:
    import json

    fs = MemoryFs("/lib")
    fs.write_text(
        "/lib/catalog.json",
        json.dumps(
            {
                "version": 1,
                "items": {
                    "lib_abcdefgh": {
                        "item_id": "lib_abcdefgh",
                        "name": "old.md",
                        "status": "ready",
                        "size": 1,
                        "sha256": "x",
                    }
                },
            }
        ),
    )
    store = LibraryStore(fs, "/lib")
    assert store.list_items()[0]["tags"] == []
    store.set_tags("lib_abcdefgh", ["旧资料"])
    saved = json.loads(fs.files["/lib/catalog.json"].decode())
    assert saved["version"] == 2
    assert saved["items"]["lib_abcdefgh"]["tags"] == ["旧资料"]


def test_corrupt_catalog_is_not_overwritten() -> None:
    fs, store = _store()
    store.ingest("a.md", b"# title\n\nkeep me")
    broken = b'{"version": 2, "items": '
    fs.files["/lib/catalog.json"] = broken
    before_files = set(fs.files)

    with pytest.raises(LibraryError):
        store.ingest("b.md", b"# title\n\nsecond")

    # 失败必须停在读取阶段：原 catalog 与磁盘都不能被覆盖。
    assert fs.files["/lib/catalog.json"] == broken
    assert set(fs.files) == before_files


def test_corrupt_jobs_file_does_not_leave_an_orphan_source() -> None:
    fs, store = _store()
    store.ingest("a.md", b"# title\n\nkeep me")
    broken = b'{"version": 1, "jobs": '
    fs.files["/lib/jobs.json"] = broken
    before_files = set(fs.files)

    with pytest.raises(LibraryError):
        store.start_ingest("b.md", b"# title\n\nsecond")

    assert fs.files["/lib/jobs.json"] == broken
    assert set(fs.files) == before_files


def test_oversized_catalog_is_not_replaced() -> None:
    fs, store = _store()
    item = store.ingest("a.md", b"# title\n\nkeep me")
    oversized = b"x" * (CATALOG_MAX_BYTES + 1)
    fs.files["/lib/catalog.json"] = oversized

    with pytest.raises(LibraryError):
        store.delete(item["item_id"])

    assert fs.files["/lib/catalog.json"] == oversized


def test_library_root_rejects_workspace_path() -> None:
    class _Sandbox:
        def home(self) -> str:
            return "/home/u"

    class _Settings:
        library_root = "/home/u/ws/.agent/library"

    with pytest.raises(LibraryError):
        resolve_library_root(_Settings(), _Sandbox(), "/home/u/ws")
