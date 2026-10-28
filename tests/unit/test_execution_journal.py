"""ExecutionJournal：长驻 append 句柄与关闭语义。"""

from __future__ import annotations

import json

import pytest

from coding_agent.execution.contracts import ExecutionResult
from coding_agent.execution.service import ExecutionJournal


def _result(op_id: str) -> ExecutionResult:
    return ExecutionResult(op_id=op_id, seq=1, kind="run", status="ok")


def test_handle_is_reused_and_each_put_is_flushed(tmp_path) -> None:
    path = tmp_path / "receipts.jsonl"
    journal = ExecutionJournal(path)
    journal.put("op-1", "hash-1", _result("op-1"), run_id="run-1")
    journal.put("op-2", "hash-2", _result("op-2"), run_id="run-1")

    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert [row["op_id"] for row in rows] == ["op-1", "op-2"]

    # 同一文件的新实例仍能加载已有回执。
    restarted = ExecutionJournal(path)
    assert restarted.get("op-1") is not None
    restarted.close()
    journal.close()


def test_close_is_idempotent_and_rejects_writes(tmp_path) -> None:
    path = tmp_path / "receipts.jsonl"
    journal = ExecutionJournal(path)
    journal.put("op-1", "hash-1", _result("op-1"))
    journal.close()
    journal.close()

    with pytest.raises(RuntimeError):
        journal.put("op-2", "hash-2", _result("op-2"))
    # 内存索引仍可读；只是不再写盘。
    assert journal.get("op-1") is not None
    # 连接已关闭：Windows 上句柄不应再阻止删除。
    path.unlink()


def test_constructing_does_not_create_the_file(tmp_path) -> None:
    path = tmp_path / "receipts.jsonl"
    journal = ExecutionJournal(path)
    assert not path.exists()
    journal.close()
    assert not path.exists()
