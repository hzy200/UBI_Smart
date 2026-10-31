"""SandboxExecutionBackend：把 WSL 沙箱包装成 A4 执行后端。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from ubismart.execution.sandbox_backend import SandboxExecutionBackend
from ubismart.sandbox.wsl_exec import ExecResult


@dataclass
class _Stat:
    path: str
    exists: bool
    is_file: bool
    size: int


@dataclass
class _Restore:
    ok: bool
    message: str


class _FakeSandbox:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.closed = False

    def run(self, command: str, *, cwd: str | None = None, **kwargs: Any) -> Any:
        self.calls.append((command, cwd))
        return ExecResult(
            command=command,
            exit_code=0,
            stdout="out",
            stderr="",
            duration_ms=1,
        )

    def close(self) -> None:
        self.closed = True


class _FakeFs:
    def __init__(self) -> None:
        self.writes: list[tuple[str, bytes]] = []

    def read_text(self, path: str, *, max_bytes: int) -> str:
        return f"read:{path}:{max_bytes}"

    def write_bytes(self, path: str, data: bytes) -> int:
        self.writes.append((path, data))
        return len(data)

    def list_dir(
        self, path: str, *, max_depth: int = 1, limit: int = 200
    ) -> list[tuple[str, str]]:
        return [(path, f"d{max_depth}:{limit}")]

    def lexical(self, path: str) -> str:
        return f"lex:{path}"

    def stat(self, path: str) -> _Stat:
        return _Stat(path=path, exists=True, is_file=True, size=3)


class _FakeSnapshots:
    def __init__(self, found: bool = True) -> None:
        self.found = found
        self.saved: list[tuple[str, str, bool]] = []

    def save(self, path: str, content: str, *, existed: bool = True) -> str:
        self.saved.append((path, content, existed))
        return "snap-1"

    def find(self, snapshot_id: str, path: str | None = None) -> Any:
        if not self.found:
            return None
        return {"id": snapshot_id, "path": path}

    def restore(self, entry: Any) -> _Restore:
        return _Restore(ok=True, message=f"restored {entry['id']}")


def _backend(tmp_path, *, found: bool = True) -> tuple[SandboxExecutionBackend, _FakeSandbox]:
    sandbox = _FakeSandbox()
    backend = SandboxExecutionBackend(sandbox, "/ws")
    backend.fs = _FakeFs()  # type: ignore[assignment]
    backend.snapshots = _FakeSnapshots(found=found)  # type: ignore[assignment]
    return backend, sandbox


def test_capabilities_advertise_the_sandbox_surface(tmp_path) -> None:
    backend, _ = _backend(tmp_path)
    caps = backend.capabilities()
    assert caps["backend"] == "_FakeSandbox"
    assert caps["workspace"] == "/ws"
    assert caps["git"] is True and caps["snapshot"] is True
    assert "patch_apply" in caps["operations"]


def test_run_and_run_raw_forward_all_arguments(tmp_path) -> None:
    backend, sandbox = _backend(tmp_path)
    backend.run("echo hi", cwd="/a", timeout=3, capture_limit=9)
    backend.run_raw("echo raw", cwd="/b", timeout=4, capture_limit=10)
    assert sandbox.calls == [("echo hi", "/a"), ("echo raw", "/b")]


def test_fs_delegates_to_sandbox_fs(tmp_path) -> None:
    backend, _ = _backend(tmp_path)
    assert backend.read_text("a.py", max_bytes=12) == "read:a.py:12"
    assert backend.write_bytes("a.py", b"abc") == 3
    assert backend.list_dir("src", max_depth=2, limit=5) == [("src", "d2:5")]
    stat = backend.stat("a.py")
    assert stat == {"path": "lex:a.py", "exists": True, "is_file": True, "size": 3}


def test_git_quotes_arguments_and_uses_workspace_by_default(tmp_path) -> None:
    backend, sandbox = _backend(tmp_path)
    backend.git(["apply", "/ws/a b.patch"])
    assert sandbox.calls == [
        ("git -C /ws apply '/ws/a b.patch'", "/ws")
    ]
    backend.git(["status"], cwd="/other")
    assert sandbox.calls[-1] == ("git -C /other status", "/other")


def test_apply_patch_builds_check_and_apply_commands(tmp_path) -> None:
    backend, sandbox = _backend(tmp_path)
    backend.apply_patch("/ws/t.patch", root="/ws", check=True)
    backend.apply_patch("/ws/t.patch", root="/ws")
    commands = [command for command, _ in sandbox.calls]
    assert commands[0].endswith("apply --check /ws/t.patch")
    assert commands[1].endswith("apply /ws/t.patch")


def test_snapshot_and_restore_delegate(tmp_path) -> None:
    backend, _ = _backend(tmp_path)
    assert backend.snapshot("a.py", "old", existed=True) == "snap-1"
    restored = backend.restore("snap-1", "a.py")
    assert restored == {"ok": True, "message": "restored snap-1"}


def test_restore_missing_snapshot_raises(tmp_path) -> None:
    backend, _ = _backend(tmp_path, found=False)
    with pytest.raises(ValueError, match="快照不存在"):
        backend.restore("missing")


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ("create", ("git -C /ws worktree add -b feature /ws/wt HEAD", "/ws")),
        ("remove", ("git -C /ws worktree remove --force /ws/wt", "/ws")),
        ("list", ("git -C /ws worktree list --porcelain", "/ws")),
    ],
)
def test_worktree_actions(tmp_path, action, expected) -> None:
    backend, sandbox = _backend(tmp_path)
    backend.worktree(action, path="/ws/wt", branch="feature")
    assert sandbox.calls[-1] == expected


def test_worktree_unknown_action_raises(tmp_path) -> None:
    backend, _ = _backend(tmp_path)
    with pytest.raises(ValueError, match="未知 worktree action"):
        backend.worktree("merge", path="/ws/wt")


def test_close_calls_sandbox_close_when_present(tmp_path) -> None:
    backend, sandbox = _backend(tmp_path)
    backend.close()
    assert sandbox.closed is True


def test_close_tolerates_missing_close(tmp_path) -> None:
    class _NoClose:
        def run(self, command: str, **kwargs: Any) -> Any:
            return None

    backend = SandboxExecutionBackend(_NoClose(), "/ws")  # type: ignore[arg-type]
    backend.close()  # 不抛异常即可
