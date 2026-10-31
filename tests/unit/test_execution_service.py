"""Phase C0/C1：ExecutionService 契约、幂等与序号。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ubismart.execution import (
    ExecutionConflict,
    ExecutionRequest,
    ExecutionSandbox,
    ExecutionService,
)
from ubismart.execution.context import tool_execution_context
from ubismart.sandbox.wsl_exec import ExecResult


class FakeBackend:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.files: dict[str, bytes] = {}

    def run_raw(self, command, *, cwd=None, timeout=None, capture_limit=None):
        self.calls.append(("raw", command, cwd, timeout, capture_limit))
        return ExecResult(
            command=command, exit_code=0, stdout="raw\n", stderr="",
            duration_ms=1,
        )

    def capabilities(self):
        return {"backend": "fake", "workspace": "/ws"}

    def run(self, command, *, cwd, timeout, capture_limit):
        self.calls.append(("run", command, cwd, timeout, capture_limit))
        if command == "fail":
            return ExecResult(
                command=command, exit_code=2, stdout="", stderr="bad",
                duration_ms=3,
            )
        if command == "slow":
            return ExecResult(
                command=command, exit_code=124, stdout="", stderr="",
                duration_ms=100, timed_out=True,
            )
        return ExecResult(
            command=command, exit_code=0, stdout="ok\n", stderr="",
            duration_ms=1,
        )

    def read_text(self, path, *, max_bytes):
        self.calls.append(("read", path, max_bytes))
        return self.files[path].decode("utf-8")

    def write_bytes(self, path, data):
        self.calls.append(("write", path, len(data)))
        self.files[path] = data
        return len(data)

    def list_dir(self, path, *, max_depth=1, limit=200):
        self.calls.append(("list", path, max_depth, limit))
        return [("f", "a.py")]

    def stat(self, path):
        self.calls.append(("stat", path))
        return {
            "exists": path in self.files,
            "is_file": path in self.files,
            "size": len(self.files.get(path, b"")),
            "real_path": path,
        }

    def git(self, args, *, cwd=None):
        self.calls.append(("git", tuple(args), cwd))
        return ExecResult(
            command="git", exit_code=0, stdout="ok\n", stderr="",
            duration_ms=1,
        )

    def snapshot(self, path, content, *, existed=True):
        self.calls.append(("snapshot", path, content, existed))
        return "snap-1"

    def restore(self, snapshot_id, path=None):
        self.calls.append(("restore", snapshot_id, path))
        return {"ok": True, "path": path or ""}

    def worktree(self, action, *, path, branch="", base="HEAD"):
        self.calls.append(("worktree", action, path, branch, base))
        return ExecResult(
            command="git worktree", exit_code=0, stdout="",
            stderr="", duration_ms=1,
        )

    def apply_patch(self, patch_path, *, root, check=False):
        self.calls.append(("apply_patch", patch_path, root, check))
        return ExecResult(
            command="git apply", exit_code=0, stdout="",
            stderr="", duration_ms=1,
        )


def _service(tmp_path, backend=None) -> ExecutionService:
    return ExecutionService(
        backend or FakeBackend(),
        str(tmp_path),
        journal_path=tmp_path / "receipts.jsonl",
    )


def test_run_maps_exec_result_and_assigns_sequence(tmp_path) -> None:
    backend = FakeBackend()
    service = _service(tmp_path, backend)
    result = service.execute(
        ExecutionRequest(
            op_id="op1",
            kind="run",
            run_id="r1",
            payload={"command": "echo hi", "cwd": "/ws"},
        )
    )
    assert result.status == "ok"
    assert result.seq == 1
    assert result.stdout == "ok\n"
    assert result.receipt_id == "r1:op1"
    assert backend.calls[0][0] == "run"

    failed = service.execute(
        ExecutionRequest(
            op_id="op2", kind="run", run_id="r1",
            payload={"command": "fail"},
        )
    )
    assert failed.status == "failed"
    assert failed.exit_code == 2
    assert failed.seq == 2

    timeout = service.execute(
        ExecutionRequest(
            op_id="op3", kind="run", run_id="r1",
            payload={"command": "slow"},
        )
    )
    assert timeout.status == "timeout"


def test_same_op_id_is_idempotent(tmp_path) -> None:
    backend = FakeBackend()
    service = _service(tmp_path, backend)
    request = ExecutionRequest(
        op_id="op1", kind="run", run_id="r1",
        payload={"command": "echo hi"},
    )
    first = service.execute(request)
    second = service.execute(request)
    assert first.replayed is False
    assert second.replayed is True
    assert second.seq == first.seq
    assert len([call for call in backend.calls if call[0] == "run"]) == 1


def test_same_op_id_with_different_payload_is_a_conflict(tmp_path) -> None:
    service = _service(tmp_path)
    service.execute(
        ExecutionRequest(
            op_id="op1", kind="run", payload={"command": "echo a"}
        )
    )
    with pytest.raises(ExecutionConflict):
        service.execute(
            ExecutionRequest(
                op_id="op1", kind="run", payload={"command": "echo b"}
            )
        )


def test_journal_survives_service_restart(tmp_path) -> None:
    backend = FakeBackend()
    first = _service(tmp_path, backend)
    first.execute(
        ExecutionRequest(
            op_id="op1", kind="run", run_id="r1",
            payload={"command": "echo hi"},
        )
    )
    restarted = _service(tmp_path, backend)
    replayed = restarted.execute(
        ExecutionRequest(
            op_id="op1", kind="run", run_id="r1",
            payload={"command": "echo hi"},
        )
    )
    assert replayed.replayed is True
    assert replayed.seq == 1

    next_result = restarted.execute(
        ExecutionRequest(
            op_id="op2", kind="run", run_id="r1",
            payload={"command": "echo next"},
        )
    )
    assert next_result.seq == 2


def test_file_operations_are_mapped(tmp_path) -> None:
    backend = FakeBackend()
    service = _service(tmp_path, backend)
    written = service.execute(
        ExecutionRequest(
            op_id="w1", kind="write_file",
            payload={"path": "a.py", "content": "print(1)\n"},
        )
    )
    assert written.status == "ok"
    assert written.data["bytes"] == 9

    read = service.execute(
        ExecutionRequest(
            op_id="r1", kind="read_file", payload={"path": "a.py"}
        )
    )
    assert read.data["content"] == "print(1)\n"

    listed = service.execute(
        ExecutionRequest(
            op_id="l1", kind="list_dir", payload={"path": ""}
        )
    )
    assert listed.data["entries"] == [{"kind": "f", "path": "a.py"}]

    stat = service.execute(
        ExecutionRequest(
            op_id="s1", kind="stat", payload={"path": "a.py"}
        )
    )
    assert stat.data["exists"] is True


def test_execution_sandbox_proxy_routes_run_and_delegates(tmp_path) -> None:
    backend = FakeBackend()
    service = _service(tmp_path, backend)

    class RealSandbox:
        def __init__(self) -> None:
            self.settings = SimpleNamespace(name="real")
            self.marker = "real"

    real = RealSandbox()
    proxy = ExecutionSandbox(real, service)
    result = proxy.run("echo hi", cwd="/ws", timeout=5, capture_limit=10)
    assert result.stdout == "raw\n"
    assert backend.calls[-1] == ("raw", "echo hi", "/ws", 5, 10)
    assert proxy.marker == "real"
    proxy.settings = SimpleNamespace(name="updated")
    assert real.settings.name == "updated"
    assert proxy.execution is service


def test_git_snapshot_restore_and_worktree_dispatch(tmp_path) -> None:
    backend = FakeBackend()
    backend.files["a.py"] = b"print(1)\n"
    service = _service(tmp_path, backend)

    git = service.execute(
        ExecutionRequest(
            op_id="g1", kind="git", payload={"args": ["status"]}
        )
    )
    assert git.status == "ok"

    snapshot = service.execute(
        ExecutionRequest(
            op_id="s1", kind="snapshot", payload={"path": "a.py"}
        )
    )
    assert snapshot.status == "ok"
    assert snapshot.snapshot_ids == ["snap-1"]

    restore = service.execute(
        ExecutionRequest(
            op_id="r1",
            kind="restore",
            payload={"snapshot_id": "snap-1", "path": "a.py"},
        )
    )
    assert restore.status == "ok"
    assert restore.data["ok"] is True

    worktree = service.execute(
        ExecutionRequest(
            op_id="w1",
            kind="worktree_create",
            payload={"path": "/tmp/wt", "branch": "agent/wt"},
        )
    )
    assert worktree.status == "ok"

    patch = service.execute(
        ExecutionRequest(
            op_id="p1",
            kind="patch_apply",
            payload={"patch_path": "/ws/t.patch", "root": "/ws"},
        )
    )
    assert patch.status == "ok"
    assert patch.data["patch"] == "/ws/t.patch"


def test_tool_context_produces_stable_op_ids_and_replays(tmp_path) -> None:
    backend = FakeBackend()
    service = _service(tmp_path, backend)

    class RealSandbox:
        settings = SimpleNamespace(name="real")

    proxy = ExecutionSandbox(RealSandbox(), service)
    with tool_execution_context("run-1", "call-1"):
        first = proxy.run("echo hi")
    assert len([call for call in backend.calls if call[0] == "run"]) == 1

    with tool_execution_context("run-1", "call-1"):
        replay = proxy.run("echo hi")
    assert replay.stdout == first.stdout
    assert len([call for call in backend.calls if call[0] == "run"]) == 1


def test_merge_git_path_uses_execution_service(tmp_path) -> None:
    from ubismart.orchestrator.merge import _git

    backend = FakeBackend()
    service = _service(tmp_path, backend)

    class Sandbox:
        execution = service

        def run(self, *args, **kwargs):
            raise AssertionError("不应回退到 sandbox.run")

    result = _git(Sandbox(), ["status", "--short"], root="/ws")
    assert result.ok
    assert any(call[0] == "git" for call in backend.calls)


def test_unknown_and_cancelled_receipts_are_terminal(tmp_path) -> None:
    service = _service(tmp_path)
    request = ExecutionRequest(
        op_id="u1", kind="run", run_id="r1",
        payload={"command": "echo hi"},
    )
    unknown = service.record_unknown(request, error="process killed")
    assert unknown.status == "unknown"
    assert unknown.replayed is False

    replay = service.record_unknown(request, error="process killed")
    assert replay.replayed is True
    assert replay.seq == unknown.seq

    cancelled = service.record_cancelled(
        ExecutionRequest(
            op_id="c1", kind="run", run_id="r1",
            payload={"command": "echo hi"},
        ),
        error="user cancelled",
    )
    assert cancelled.status == "cancelled"


def test_unsupported_kind_is_a_receipt_not_an_exception(tmp_path) -> None:
    service = _service(tmp_path)
    result = service.execute(
        ExecutionRequest(op_id="x", kind="unknown_kind", payload={})
    )
    assert result.status == "unsupported"
    assert "unknown_kind" in result.error


class _HostSandbox:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def run(self, command, *, cwd=None, **_kwargs):
        self.calls.append((command, cwd))
        return "ok"


class _HostBackend(FakeBackend):
    def __init__(self) -> None:
        super().__init__()
        self.sandbox = _HostSandbox()


def test_host_run_uses_whitelist(tmp_path) -> None:
    from ubismart.execution.host_ops import HostExecutionDenied

    backend = _HostBackend()
    service = _service(tmp_path, backend)
    assert (
        service.host_run(
            "mkdir -p /ws/stage",
            kind="workspace_import_prepare",
            cwd="/ws",
        )
        == "ok"
    )
    with pytest.raises(HostExecutionDenied):
        service.host_run("rm -rf /", kind="workspace_import", cwd="/ws")


def test_host_run_requires_backend_sandbox(tmp_path) -> None:
    from ubismart.execution.contracts import ExecutionError

    service = _service(tmp_path)
    with pytest.raises(ExecutionError):
        service.host_run(
            "mkdir -p /ws/stage",
            kind="workspace_import_prepare",
            cwd="/ws",
        )
