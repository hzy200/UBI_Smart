"""worker journal：指纹、落盘、复用过滤与计量清零。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ubismart.session.worker_journal import (
    WorkerJournal,
    parse_roles,
    request_fingerprint,
    task_fingerprint,
    workspace_fingerprint,
)


def _result(task_id: str = "a", *, status: str = "success") -> dict[str, Any]:
    return {
        "task_id": task_id,
        "role": "explore",
        "goal": "定位",
        "status": status,
        "summary": "入口在 a.py",
        "findings": ["main 在 a.py:3"],
        "files": ["a.py"],
        "input_tokens": 123,
        "output_tokens": 45,
        "tool_calls": [{"name": "file_read"}],
        "proposed_tasks": [{"id": "x"}],
        "budget_used": {"input_tokens": 123},
    }


def _record(
    journal: WorkerJournal,
    *,
    task_hash: str = "task",
    workspace_hash: str = "ws",
    status: str = "success",
    result: dict[str, Any] | None = None,
) -> None:
    journal.record(
        thread_id="thread-1",
        request_hash=request_fingerprint("做事"),
        task_hash=task_hash,
        workspace_hash=workspace_hash,
        role="explore",
        status=status,
        attempt=1,
        result=result or _result(),
    )


def test_roundtrip_trims_usage_and_tool_traces(tmp_path: Path) -> None:
    journal = WorkerJournal(tmp_path / "workers.jsonl")
    _record(journal)

    entry = journal.lookup(
        thread_id="thread-1",
        request_hash=request_fingerprint("做事"),
        task_hash="task",
        workspace_hash="ws",
    )

    assert entry is not None
    result = entry["result"]
    assert result["summary"] == "入口在 a.py"
    assert result["input_tokens"] == 0
    assert result["output_tokens"] == 0
    assert result["tool_calls"] == []
    assert result["proposed_tasks"] == []


def test_latest_entry_wins(tmp_path: Path) -> None:
    journal = WorkerJournal(tmp_path / "workers.jsonl")
    first = _result()
    first["summary"] = "旧结论"
    second = _result()
    second["summary"] = "新结论"

    _record(journal, result=first)
    _record(journal, result=second)

    entry = journal.lookup(
        thread_id="thread-1",
        request_hash=request_fingerprint("做事"),
        task_hash="task",
        workspace_hash="ws",
    )
    assert entry is not None
    assert entry["result"]["summary"] == "新结论"


def test_status_filter_and_workspace_mismatch(tmp_path: Path) -> None:
    journal = WorkerJournal(tmp_path / "workers.jsonl")
    _record(journal, status="partial")

    assert (
        journal.lookup(
            thread_id="thread-1",
            request_hash=request_fingerprint("做事"),
            task_hash="task",
            workspace_hash="ws",
            allow_statuses={"success"},
        )
        is None
    )
    assert (
        journal.lookup(
            thread_id="thread-1",
            request_hash=request_fingerprint("做事"),
            task_hash="task",
            workspace_hash="ws",
            allow_statuses={"success", "partial"},
        )
        is not None
    )
    assert (
        journal.lookup(
            thread_id="thread-1",
            request_hash=request_fingerprint("做事"),
            task_hash="task",
            workspace_hash="other",
            allow_statuses={"partial"},
        )
        is None
    )


def test_fingerprints_track_inputs(tmp_path: Path) -> None:
    base = {"id": "t1", "role": "explore", "goal": "定位", "inputs": ["a.py"]}
    changed = {**base, "inputs": ["b.py"]}

    assert task_fingerprint(base) != task_fingerprint(changed)
    assert request_fingerprint("甲") != request_fingerprint("乙")
    assert workspace_fingerprint(
        {"git": True, "git_clean": True, "git_head": "a"}
    ) != workspace_fingerprint(
        {"git": True, "git_clean": True, "git_head": "b"}
    )


def test_roles_parse_with_default() -> None:
    assert parse_roles("explore, review", default=("explore",)) == {
        "explore",
        "review",
    }
    assert parse_roles("", default=("explore", "review")) == {
        "explore",
        "review",
    }
