"""审计异步写：有界队列、专用写线程、失败显式报出。"""

from __future__ import annotations

import threading

import pytest

from ubismart.audit import AuditError, AuditLogger, AuditRecord
from ubismart.audit.logger import now_iso, read_records


def _record(**overrides):
    base = {"ts": now_iso(), "kind": "tool_call", "thread_id": "t1"}
    return AuditRecord(**{**base, **overrides})


def test_async_writer_persists_after_flush(tmp_path) -> None:
    logger = AuditLogger(tmp_path, async_write=True, queue_size=10)
    logger.write(_record(tool="shell_exec"))
    logger.flush()

    assert [record.tool for record in read_records(logger.path)] == [
        "shell_exec"
    ]


def test_async_writer_surfaces_background_failure(tmp_path, monkeypatch) -> None:
    logger = AuditLogger(tmp_path, async_write=True, queue_size=10)

    def fail(_record):
        raise OSError("disk full")

    monkeypatch.setattr(logger, "_write_sync", fail)
    logger.write(_record(tool="shell_exec"))
    with pytest.raises(AuditError):
        logger.flush()
    with pytest.raises(AuditError):
        logger.write(_record(tool="file_edit"))


def test_queue_full_is_an_explicit_failure(tmp_path, monkeypatch) -> None:
    logger = AuditLogger(tmp_path, async_write=True, queue_size=1)
    gate = threading.Event()

    def blocked(_record):
        gate.wait(2)

    monkeypatch.setattr(logger, "_write_sync", blocked)
    logger.write(_record(tool="shell_exec"))
    # 确保 writer 已经取走第一条并卡住，第二条才会把容量 1 的队列填满。
    for _ in range(100):
        if logger._write_queue.qsize() == 0:
            break
        threading.Event().wait(0.01)
    logger.write(_record(tool="file_edit"))
    with pytest.raises(AuditError):
        logger.write(_record(tool="shell_exec"))
    gate.set()
