"""A4 host 适配器：HostFs / HostFileAccess / HostSnapshotStore / Backend。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from coding_agent.execution.contracts import ExecutionError
from coding_agent.execution.host_backend import (
    HostExecutionBackend,
)
from coding_agent.execution.host_file_access import HostFileAccess
from coding_agent.execution.host_fs import HostFs
from coding_agent.execution.host_snapshots import HostSnapshotStore
from coding_agent.host_broker.errors import BrokerOpError
from coding_agent.host_broker.ops import HostReadOnlyOps
from coding_agent.host_broker.paths import HostPathPolicy
from coding_agent.host_broker.policy import GrantStore, HostPolicy
from coding_agent.host_broker.protocol import make_request
from coding_agent.host_broker.snapshots import HostSnapshotStore as BrokerSnapshots
from coding_agent.host_broker.toolchain import ToolchainRegistry


class Caller:
    def __init__(self, ops: HostReadOnlyOps, grant_id: str) -> None:
        self.ops = ops
        self.grant_id = grant_id

    def call(
        self,
        kind: str,
        *,
        workspace: dict[str, Any],
        payload: dict[str, Any],
        grant_id: str = "",
    ) -> dict[str, Any]:
        frame = make_request(
            kind,
            op_id=f"test-{kind}-{id(payload)}",
            workspace=workspace,
            payload=payload,
            grant_id=grant_id or self.grant_id,
        )
        try:
            response = asyncio.run(self.ops.handle(frame))
        except BrokerOpError as exc:
            raise RuntimeError(f"{exc.code}: {exc.message}") from exc
        return dict(response.data)


def _setup(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    grants = GrantStore()
    grant = grants.issue(
        scope=[str(root)], verbs=["read", "write", "exec", "install"]
    )
    ops = HostReadOnlyOps(
        policy=HostPolicy(deny_roots=[], sensitive_roots=[]),
        path_policy=HostPathPolicy(),
        grants=grants,
        toolchains=ToolchainRegistry(),
        workspace_roots={"ws-1": str(root)},
        snapshots=BrokerSnapshots(tmp_path / "snapshots"),
        allow_write=True,
    )
    caller = Caller(ops, grant.grant_id)
    access = HostFileAccess(
        caller, workspace_id="ws-1", root_handle="h-1"
    )
    fs = HostFs(caller, workspace_id="ws-1", root_handle="h-1")
    snapshots = HostSnapshotStore(
        caller, workspace_id="ws-1", root_handle="h-1"
    )
    return root, caller, access, fs, snapshots


def test_host_fs_write_and_snapshot_restore(tmp_path) -> None:
    root, _, access, fs, snapshots = _setup(tmp_path)
    target = fs.lexical("src/a.py")
    relative, info, original = fs.read_for_update(target, max_bytes=1000)
    assert info.exists is False and original is None

    snapshot_id = snapshots.write_with_snapshot(
        relative, "print('x')\n", "", existed=False
    )
    assert (root / "src" / "a.py").read_text(encoding="utf-8") == "print('x')\n"
    assert access.read_text("src/a.py", max_bytes=1000) == "print('x')\n"

    entry = snapshots.latest_for("src/a.py")
    assert entry is not None and entry.snapshot_id == snapshot_id
    assert snapshots.was_absent(entry) is True
    result = snapshots.restore(entry)
    assert result.ok and result.undo_snapshot_id
    assert not (root / "src" / "a.py").exists()


def test_host_list_dir_and_find(tmp_path) -> None:
    root, _, access, _, _ = _setup(tmp_path)
    (root / "a.txt").write_text("a", encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "b.py").write_text("b", encoding="utf-8")

    assert ("f", "a.txt") in access.list_dir(".", limit=10)
    assert ("d", "sub") in access.list_dir(".", limit=10)
    assert access.find_files("*.py", limit=10) == ["sub/b.py"]


def test_host_chunked_read_over_frame_limit(tmp_path) -> None:
    root, _, access, _, _ = _setup(tmp_path)
    payload = "x" * (600 * 1024)
    (root / "big.txt").write_text(payload, encoding="utf-8")
    assert access.read_text("big.txt", max_bytes=700 * 1024) == payload


def test_host_snapshot_write_many(tmp_path) -> None:
    root, _, _, fs, snapshots = _setup(tmp_path)
    entries = [
        (fs.lexical("a.txt"), "A", "", False),
        (fs.lexical("b.txt"), "B", "", False),
    ]
    ids = snapshots.write_many_with_snapshots(entries)
    assert set(ids) == {"a.txt", "b.txt"}
    assert (root / "a.txt").read_text(encoding="utf-8") == "A"
    assert (root / "b.txt").read_text(encoding="utf-8") == "B"


def test_host_backend_write_snapshot_and_exec_denied(tmp_path) -> None:
    _, caller, _, _, _ = _setup(tmp_path)
    backend = HostExecutionBackend(
        caller, workspace_id="ws-1", root_handle="h-1"
    )
    backend.write_bytes("a.txt", b"hello")
    data = backend.stat("a.txt")
    assert data["exists"] is True and data["size"] == 5
    snapshot_id = backend.snapshot("a.txt", "hello")
    backend.write_bytes("a.txt", b"new")
    backend.restore(snapshot_id)
    assert backend.read_text("a.txt", max_bytes=10) == "hello"

    with pytest.raises(ExecutionError):
        backend.run("echo hi", cwd=".")
    install = backend.install_request(
        manager="pip", command="pip install requests"
    )
    assert install["manual"] is True and install["executed"] is False
