"""资料库异步任务：状态、进度、取消与批量重建。"""

from __future__ import annotations

from tests.unit.test_library_store import MemoryFs

from ubismart.library.store import (
    JOB_CANCELLED,
    JOB_READY,
    STATUS_CANCELLED,
    LibraryStore,
)
from ubismart.memory.embeddings import HashEmbedder


def _store(**kwargs) -> LibraryStore:
    return LibraryStore(MemoryFs("/lib"), "/lib", **kwargs)


def test_ingest_job_moves_from_queued_to_ready() -> None:
    store = _store()
    started = store.start_ingest("a.md", b"# title\n\nneedle")
    assert started["item"]["status"] == "queued"
    assert started["job"]["status"] == "queued"

    job_id = started["job"]["job_id"]
    item_id = started["item"]["item_id"]
    assert store.get_job(job_id)["stage"] == "queued"

    job = store.process_job(job_id)
    assert job["status"] == JOB_READY
    assert job["progress"] == 100
    item = store.get(item_id)
    assert item["status"] == "ready"
    assert item["chunk_count"] >= 1
    assert store.search("needle")


def test_cancel_queued_job_marks_item_cancelled() -> None:
    store = _store()
    started = store.start_ingest("a.md", b"# title\n\nneedle")
    job_id = started["job"]["job_id"]
    item_id = started["item"]["item_id"]

    cancelled = store.cancel_job(job_id)
    assert cancelled["status"] == JOB_CANCELLED
    assert store.get(item_id)["status"] == STATUS_CANCELLED

    # 已经取消的任务不会再被处理成 ready。
    assert store.process_job(job_id)["status"] == JOB_CANCELLED


def test_reindex_all_processes_every_item() -> None:
    store = _store()
    first = store.ingest("a.md", b"# a\n\nalpha")
    second = store.ingest("b.md", b"# b\n\nbeta")
    assert first["status"] == "ready"
    assert second["status"] == "ready"

    started = store.start_reindex_all()
    assert started["count"] == 2
    job = store.process_job(started["job"]["job_id"])
    assert job["status"] == JOB_READY
    assert job["done"] == 2
    assert job["total"] == 2
    assert job["errors"] == {}


def test_orphan_queued_item_is_recovered_as_failed() -> None:
    store = _store()
    started = store.start_ingest("a.md", b"# title\n\nneedle")
    item_id = started["item"]["item_id"]
    # 模拟 jobs.json 丢失/崩溃后只留下 queued 条目。
    store._fs.files.pop("/lib/jobs.json", None)

    assert store.recover_jobs(stale_seconds=3_600) >= 1
    assert store.get(item_id)["status"] == "failed"


def test_vector_job_writes_vectors_outside_item_lock() -> None:
    store = _store(
        search_mode="vector",
        embedder=HashEmbedder(32),
    )
    started = store.start_ingest("a.md", b"# a\n\nalpha alpha")
    job = store.process_job(started["job"]["job_id"])
    assert job["status"] == JOB_READY
    item_id = started["item"]["item_id"]
    assert f"/lib/items/{item_id}/vectors.json" in store._fs.files
    hits = store.search("alpha", mode="vector")
    assert hits and hits[0]["mode"] == "vector"
