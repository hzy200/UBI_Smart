"""运行切分与并排对比（审计记录为唯一事实源）。"""

from __future__ import annotations

import pytest

from coding_agent.run_compare import build_runs, compare_runs


def _record(kind: str, ts: str, **fields) -> dict:
    return {"kind": kind, "ts": ts, **fields}


def _records() -> list[dict]:
    return [
        _record("run_start", "2026-10-29T10:00:00+00:00", detail="第一问"),
        _record("tool_call", "2026-10-29T10:00:01+00:00", tool="ls", ok=True),
        _record(
            "file_change",
            "2026-10-29T10:00:02+00:00",
            path="a.py",
            snapshot_id="s1",
            added=2,
            removed=1,
        ),
        _record("tool_call", "2026-10-29T10:00:03+00:00", tool="cat", ok=False),
        _record(
            "run_end",
            "2026-10-29T10:00:04+00:00",
            detail="第一答",
            input_tokens=10,
            output_tokens=5,
            cost_usd=0.1,
        ),
        _record("run_start", "2026-10-29T11:00:00+00:00", detail="第二问"),
        _record(
            "file_change",
            "2026-10-29T11:00:01+00:00",
            path="a.py",
            snapshot_id="s2",
            added=4,
            removed=0,
        ),
        _record(
            "file_change",
            "2026-10-29T11:00:02+00:00",
            path="b.py",
            snapshot_id="s3",
            added=1,
            removed=1,
        ),
        _record(
            "run_end",
            "2026-10-29T11:00:03+00:00",
            detail="第二答",
            input_tokens=20,
            output_tokens=10,
            worker_input_tokens=1,
            worker_output_tokens=2,
            cost_usd=0.2,
        ),
    ]


def test_build_runs_splits_on_run_start_and_end() -> None:
    runs = build_runs(_records())
    assert [run["index"] for run in runs] == [1, 2]
    first, second = runs
    assert first["prompt"] == "第一问"
    assert first["answer"] == "第一答"
    assert first["tool_calls"] == 2
    assert first["failed_tools"] == 1
    assert first["total_tokens"] == 15
    assert first["files"][0]["snapshot_id"] == "s1"
    assert first["files"][0]["added"] == 2
    assert second["total_tokens"] == 33
    assert [item["path"] for item in second["files"]] == ["a.py", "b.py"]


def test_build_runs_marks_unfinished_as_interrupted() -> None:
    runs = build_runs(
        [_record("run_start", "2026-10-29T10:00:00+00:00", detail="x")]
    )
    assert runs[0]["status"] == "interrupted"
    assert runs[0]["ended_at"] == ""


def test_compare_runs_returns_union_of_files_and_usage() -> None:
    data = compare_runs(build_runs(_records()), 1, 2)
    assert data["left"]["index"] == 1
    assert data["right"]["index"] == 2
    paths = [item["path"] for item in data["files"]]
    assert paths == ["a.py", "b.py"]
    assert data["files"][0]["left"]["snapshot_id"] == "s1"
    assert data["files"][0]["right"]["snapshot_id"] == "s2"
    assert data["files"][1]["left"] is None
    assert data["usage"]["right"]["tokens"] == 33


def test_compare_runs_rejects_unknown_index() -> None:
    with pytest.raises(KeyError):
        compare_runs(build_runs(_records()), 1, 9)
