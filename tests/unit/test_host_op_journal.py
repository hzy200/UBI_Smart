"""A2L 本地 op_journal：重启后把未完成操作标成 unknown。"""

from __future__ import annotations

import pytest

from ubismart.host_broker.op_journal import (
    OP_FAILED,
    OP_FINISHED,
    OP_STARTED,
    OP_UNKNOWN,
    OpJournal,
)


def test_start_is_idempotent_and_get_roundtrips(tmp_path) -> None:
    journal = OpJournal(tmp_path / "ops.sqlite3")
    assert journal.start("op-1", "read_file", meta={"path": "a.py"}) is True
    assert journal.start("op-1", "read_file") is False
    record = journal.get("op-1")
    assert record is not None
    assert record.status == OP_STARTED
    assert record.kind == "read_file"
    assert '"path": "a.py"' in record.meta


def test_finish_only_from_non_terminal(tmp_path) -> None:
    journal = OpJournal(tmp_path / "ops.sqlite3")
    journal.start("op-1", "read_file")
    assert journal.finish("op-1", OP_FINISHED, result={"ok": True}) is True
    assert journal.finish("op-1", OP_FAILED) is False
    record = journal.get("op-1")
    assert record is not None
    assert record.status == OP_FINISHED


def test_recover_marks_started_as_unknown(tmp_path) -> None:
    path = tmp_path / "ops.sqlite3"
    first = OpJournal(path)
    first.start("op-1", "write_file")
    # 模拟进程重启：新实例把遗留的 started 标成 unknown。
    second = OpJournal(path)
    record = second.get("op-1")
    assert record is not None
    assert record.status == OP_UNKNOWN
    assert [item.op_id for item in second.unknown()] == ["op-1"]
    # unknown 可以在确认后写入终态，但不允许被当成成功重放。
    assert second.finish("op-1", OP_FINISHED, result={"confirmed": True}) is True


def test_rejects_invalid_terminal_status(tmp_path) -> None:
    journal = OpJournal(tmp_path / "ops.sqlite3")
    journal.start("op-1", "read_file")
    with pytest.raises(ValueError):
        journal.finish("op-1", "maybe")


def test_close_is_idempotent_and_releases_the_file(tmp_path) -> None:
    path = tmp_path / "ops.sqlite3"
    journal = OpJournal(path)
    journal.start("op-1", "read_file")
    journal.close()
    journal.close()
    with pytest.raises(RuntimeError):
        journal.get("op-1")
    # 连接已关闭：Windows 上文件句柄不应再阻止删除。
    path.unlink()
