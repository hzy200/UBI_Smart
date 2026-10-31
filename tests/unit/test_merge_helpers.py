"""implement 合并的辅助路径：路径归一化、numstat 解析、快照与回滚。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from ubismart.orchestrator import merge
from ubismart.sandbox.fs import SandboxFsError
from ubismart.sandbox.wsl_exec import ExecResult

pytestmark = pytest.mark.regression


def _result(
    exit_code: int = 0,
    *,
    stdout: str = "",
    stderr: str = "",
    timed_out: bool = False,
) -> ExecResult:
    return ExecResult(
        command="git",
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_ms=1,
        timed_out=timed_out,
    )


# ---------------- 路径归一化 ----------------

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("src/a.py", "src/a.py"),
        ("src\\a.py", "src/a.py"),
        ("src/./a.py", "src/a.py"),
        ("", ""),
        (".", ""),
        ("..", ""),
        ("../evil.py", ""),
        ("/etc/passwd", ""),
        (".agent", ""),
        (".agent/x.py", ""),
        ("a/__pycache__/b.pyc", ""),
    ],
)
def test_normalize_patch_path(raw: str, expected: str) -> None:
    assert merge._normalize_patch_path(raw) == expected


def test_merge_paths_merges_and_filters() -> None:
    record = {"task_id": "t1", "files": ["a.py", "../bad", ".agent/x"]}
    paths = merge._merge_paths(record, {"t1": ["a.py", "b.py"]})
    assert paths == ["a.py", "b.py"]


def test_patch_components_group_by_shared_file() -> None:
    records = [
        {"task_id": "t1"},
        {"task_id": "t2"},
        {"task_id": "t3"},
    ]
    groups = merge._patch_components(
        records,
        {"t1": ["a.py"], "t2": ["b.py"], "t3": ["a.py"]},
    )
    assert [[item["task_id"] for item in group] for group in groups] == [
        ["t1", "t3"],
        ["t2"],
    ]


# ---------------- numstat ----------------

def test_patch_paths_parses_numstat(monkeypatch) -> None:
    monkeypatch.setattr(
        merge,
        "_git",
        lambda sandbox, args, root: _result(
            stdout="1\t2\tsrc/a.py\0"
        ),
    )
    assert merge._patch_paths(object(), "p.patch", "/ws") == (
        ["src/a.py"],
        "",
    )


def test_patch_paths_handles_rename_fields(monkeypatch) -> None:
    monkeypatch.setattr(
        merge,
        "_git",
        lambda sandbox, args, root: _result(
            stdout="0\t0\tsrc/new.py\0src/old.py\0"
        ),
    )
    assert merge._patch_paths(object(), "p.patch", "/ws") == (
        ["src/new.py", "src/old.py"],
        "",
    )


def test_patch_paths_rejects_out_of_scope_paths(monkeypatch) -> None:
    monkeypatch.setattr(
        merge,
        "_git",
        lambda sandbox, args, root: _result(
            stdout="1\t1\t../evil.py\0"
        ),
    )
    paths, error = merge._patch_paths(object(), "p.patch", "/ws")
    assert paths == []
    assert "不允许合并" in error


def test_patch_paths_propagates_git_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        merge,
        "_git",
        lambda sandbox, args, root: _result(1, stderr="bad patch"),
    )
    paths, error = merge._patch_paths(object(), "p.patch", "/ws")
    assert paths == []
    assert "bad patch" in error


# ---------------- _git 两条路径 ----------------

def test_git_prefers_execution_service() -> None:
    requests = []

    class Execution:
        def execute(self, request):
            requests.append(request)
            return SimpleNamespace(
                exit_code=None,
                status="ok",
                stdout="out",
                stderr="",
                error="",
                duration_ms=3,
            )

    sandbox = SimpleNamespace(execution=Execution())
    result = merge._git(sandbox, ["status"], root="/ws")
    assert result.ok
    assert result.stdout == "out"
    assert requests[0].kind == "git"
    assert requests[0].payload["cwd"] == "/ws"


def test_git_maps_failed_execution_to_nonzero() -> None:
    class Execution:
        def execute(self, request):
            return SimpleNamespace(
                exit_code=None,
                status="failed",
                stdout="",
                stderr="",
                error="boom",
                duration_ms=1,
            )

    result = merge._git(
        SimpleNamespace(execution=Execution()), ["status"], root="/ws"
    )
    assert result.exit_code == 1
    assert result.stderr == "boom"


def test_git_falls_back_to_host_run(monkeypatch) -> None:
    calls = []

    def fake_host_run(sandbox, command, **kwargs):
        calls.append((command, kwargs))
        return _result(stdout="legacy")

    monkeypatch.setattr(merge, "host_run", fake_host_run)
    result = merge._git(SimpleNamespace(), ["status"], root="/ws")
    assert result.stdout == "legacy"
    assert calls[0][1]["kind"] == "git_apply"


# ---------------- merge_implement_patch ----------------

def _record(**overrides) -> dict:
    data = {
        "task_id": "t1",
        "status": "success",
        "patch_path": "/ws/p.patch",
        "files": ["a.py"],
    }
    data.update(overrides)
    return data


def test_merge_patch_requires_patch_path() -> None:
    merged, error, ids = asyncio.run(
        merge.merge_implement_patch(object(), "/ws", _record(patch_path=""))
    )
    assert merged is False
    assert "为空" in error
    assert ids == []


def test_merge_patch_skips_failed_worker() -> None:
    merged, error, _ = asyncio.run(
        merge.merge_implement_patch(
            object(), "/ws", _record(status="failed")
        )
    )
    assert merged is False
    assert "未成功" in error


def test_merge_patch_check_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        merge, "_git", lambda *args, **kwargs: _result(1, stderr="conflict")
    )
    merged, error, _ = asyncio.run(
        merge.merge_implement_patch(object(), "/ws", _record())
    )
    assert merged is False
    assert "conflict" in error


def test_merge_patch_path_error(monkeypatch) -> None:
    monkeypatch.setattr(
        merge, "_git", lambda *args, **kwargs: _result()
    )
    monkeypatch.setattr(
        merge, "_patch_paths", lambda *args: ([], "坏路径")
    )
    merged, error, _ = asyncio.run(
        merge.merge_implement_patch(object(), "/ws", _record())
    )
    assert merged is False
    assert error == "坏路径"


def test_merge_patch_snapshot_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        merge, "_git", lambda *args, **kwargs: _result()
    )
    monkeypatch.setattr(
        merge, "_patch_paths", lambda *args: (["a.py"], "")
    )
    monkeypatch.setattr(
        merge, "_snapshot_paths", lambda *args: (["s1"], "留底失败")
    )
    merged, error, ids = asyncio.run(
        merge.merge_implement_patch(object(), "/ws", _record())
    )
    assert merged is False
    assert error == "留底失败"
    assert ids == ["s1"]


def test_merge_patch_success(monkeypatch) -> None:
    monkeypatch.setattr(
        merge, "_git", lambda *args, **kwargs: _result()
    )
    monkeypatch.setattr(
        merge, "_patch_paths", lambda *args: (["a.py"], "")
    )
    monkeypatch.setattr(
        merge, "_snapshot_paths", lambda *args: (["s1"], "")
    )
    merged, error, ids = asyncio.run(
        merge.merge_implement_patch(object(), "/ws", _record())
    )
    assert (merged, error, ids) == (True, "", ["s1"])


# ---------------- 3-way 预演 ----------------

def test_validate_three_way_cleans_temp_index(monkeypatch) -> None:
    commands: list[str] = []

    def fake_host_run(sandbox, command, **kwargs):
        commands.append(command)
        return _result()

    monkeypatch.setattr(merge, "host_run", fake_host_run)
    ok, error = asyncio.run(
        merge._validate_three_way(
            object(), "/ws", [_record(patch_path="/ws/a.patch")]
        )
    )
    assert (ok, error) == (True, "")
    assert any("read-tree" in command for command in commands)
    assert any("--3way --cached" in command for command in commands)
    assert any(command.startswith("rm -f") for command in commands)


def test_validate_three_way_read_tree_failure(monkeypatch) -> None:
    commands: list[str] = []

    def fake_host_run(sandbox, command, **kwargs):
        commands.append(command)
        if "read-tree" in command:
            return _result(1, stderr="no HEAD")
        return _result()

    monkeypatch.setattr(merge, "host_run", fake_host_run)
    ok, error = asyncio.run(
        merge._validate_three_way(object(), "/ws", [_record()])
    )
    assert ok is False
    assert "no HEAD" in error
    assert any(command.startswith("rm -f") for command in commands)


def test_validate_three_way_patch_failure(monkeypatch) -> None:
    def fake_host_run(sandbox, command, **kwargs):
        if "read-tree" in command:
            return _result()
        if "apply" in command:
            return _result(1, stderr="conflict")
        return _result()

    monkeypatch.setattr(merge, "host_run", fake_host_run)
    ok, error = asyncio.run(
        merge._validate_three_way(object(), "/ws", [_record()])
    )
    assert ok is False
    assert "conflict" in error


# ---------------- 快照 ----------------

def test_snapshot_paths_empty_is_noop() -> None:
    assert merge._snapshot_paths(object(), "/ws", []) == ([], "")


def test_snapshot_paths_uses_batch_script(monkeypatch) -> None:
    payload = (
        'AGENT_SNAPSHOT_JSON:{"a.py": {"exists": true, '
        '"path": "/ws/a.py", "content": "old"}}'
    )
    monkeypatch.setattr(
        merge, "host_run", lambda *a, **k: _result(stdout=payload)
    )
    captured: list[list[tuple[str, str, bool]]] = []

    class Store:
        def __init__(self, *args):
            pass

        def save_many(self, items):
            captured.append(list(items))
            return ["s1"]

    monkeypatch.setattr(merge, "SnapshotStore", Store)
    monkeypatch.setattr(merge, "SandboxFs", lambda *a: object())

    ids, error = merge._snapshot_paths(object(), "/ws", ["a.py"])
    assert (ids, error) == (["s1"], "")
    assert captured == [[("/ws/a.py", "old", True)]]


def test_snapshot_paths_reports_batch_error(monkeypatch) -> None:
    payload = 'AGENT_SNAPSHOT_JSON:{"a.py": {"error": "路径越界"}}'
    monkeypatch.setattr(
        merge, "host_run", lambda *a, **k: _result(stdout=payload)
    )
    ids, error = merge._snapshot_paths(object(), "/ws", ["a.py"])
    assert ids == []
    assert "a.py" in error and "路径越界" in error


def test_snapshot_paths_falls_back_to_per_file(monkeypatch) -> None:
    monkeypatch.setattr(merge, "host_run", lambda *a, **k: _result())

    class Fs:
        def __init__(self, *args):
            pass

        def probe(self, target):
            return ("/ws/a.py", SimpleNamespace(exists=True, is_file=True))

        def read_text(self, resolved, max_bytes=0):
            return "old"

    class Store:
        def __init__(self, *args):
            pass

        def save(self, resolved, content, *, existed):
            assert existed is True
            return "s1"

    monkeypatch.setattr(merge, "SandboxFs", Fs)
    monkeypatch.setattr(merge, "SnapshotStore", Store)

    ids, error = merge._snapshot_paths(object(), "/ws", ["a.py"])
    assert (ids, error) == (["s1"], "")


def test_snapshot_paths_reports_probe_error(monkeypatch) -> None:
    monkeypatch.setattr(merge, "host_run", lambda *a, **k: _result())

    class Fs:
        def __init__(self, *args):
            pass

        def probe(self, target):
            raise SandboxFsError("denied")

    monkeypatch.setattr(merge, "SandboxFs", Fs)
    monkeypatch.setattr(
        merge, "SnapshotStore", lambda *args: SimpleNamespace()
    )

    ids, error = merge._snapshot_paths(object(), "/ws", ["a.py"])
    assert ids == []
    assert "denied" in error


# ---------------- 回滚 ----------------

def test_rollback_stops_on_reverse_success(monkeypatch) -> None:
    monkeypatch.setattr(
        merge, "host_run", lambda *a, **k: _result()
    )

    def _boom(*args):
        raise AssertionError("反向 apply 成功时不应回退快照")

    monkeypatch.setattr(merge, "SnapshotStore", _boom)
    merge._rollback_merged(
        object(), "/ws", [_record(patch_path="/ws/a.patch")], ["a.py"]
    )


def test_rollback_restores_snapshot_when_reverse_fails(monkeypatch) -> None:
    monkeypatch.setattr(
        merge, "host_run", lambda *a, **k: _result(1, stderr="conflict")
    )
    restored: list[str] = []

    class Store:
        def __init__(self, *args):
            pass

        def latest_for(self, path):
            return SimpleNamespace(path=path)

        def restore(self, entry):
            restored.append(entry.path)

    monkeypatch.setattr(merge, "SandboxFs", lambda *a: object())
    monkeypatch.setattr(merge, "SnapshotStore", Store)

    merge._rollback_merged(
        object(), "/ws", [_record(patch_path="/ws/a.patch")], ["a.py"]
    )
    assert restored == ["a.py"]
