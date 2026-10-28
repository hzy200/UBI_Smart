"""P1：brokered write、分块传输、宿主快照与恢复。"""

from __future__ import annotations

import asyncio
import base64
import hashlib

import pytest

from coding_agent.host_broker.errors import BrokerOpError
from coding_agent.host_broker.ops import HostReadOnlyOps
from coding_agent.host_broker.paths import HostPathPolicy
from coding_agent.host_broker.policy import GrantStore, HostPolicy
from coding_agent.host_broker.protocol import (
    CONFLICT,
    DENIED_ZX,
    GRANT_REQUIRED,
    make_request,
)
from coding_agent.host_broker.snapshots import HostSnapshotStore
from coding_agent.host_broker.toolchain import ToolchainRegistry


def _ops(tmp_path, *, deny=(), write=True):
    root = tmp_path / "project"
    root.mkdir()
    policy = HostPolicy(
        deny_roots=[str(item) for item in deny],
        sensitive_roots=[],
    )
    grants = GrantStore()
    snaps = HostSnapshotStore(tmp_path / "snapshots")
    ops = HostReadOnlyOps(
        policy=policy,
        path_policy=HostPathPolicy(),
        grants=grants,
        toolchains=ToolchainRegistry(),
        workspace_roots={"ws-1": str(root)},
        broker_id="b-1",
        snapshots=snaps,
        allow_write=write,
    )
    return ops, grants, root, snaps


def _frame(kind, grant_id="", payload=None, op_id="op-1"):
    return make_request(
        kind,
        op_id=op_id,
        workspace={"workspace_id": "ws-1", "kind": "host"},
        payload=payload or {},
        grant_id=grant_id,
    )


def _write_frame(kind, grant_id, payload, op_id="op-1"):
    return _frame(kind, grant_id, payload, op_id)


def test_write_requires_grant_then_creates_snapshot(tmp_path) -> None:
    ops, grants, root, snaps = _ops(tmp_path)

    with pytest.raises(BrokerOpError) as exc:
        asyncio.run(
            ops.handle(
                _write_frame(
                    "host.write_text",
                    "",
                    {"path": "a.txt", "text": "new"},
                )
            )
        )
    assert exc.value.code == GRANT_REQUIRED

    grant = grants.issue(scope=[str(root)], verbs=["write"])
    response = asyncio.run(
        ops.handle(
            _write_frame(
                "host.write_text",
                grant.grant_id,
                {"path": "a.txt", "text": "new"},
            )
        )
    )
    assert response.status == "ok"
    assert (root / "a.txt").read_text(encoding="utf-8") == "new"
    snapshot_id = response.data["snapshot_id"]
    assert snapshot_id

    restored = asyncio.run(
        ops.handle(
            _write_frame(
                "host.restore",
                grant.grant_id,
                {"snapshot_id": snapshot_id},
                op_id="op-restore",
            )
        )
    )
    assert restored.data["undo_snapshot_id"]
    assert not (root / "a.txt").exists()


def test_expected_sha_conflict_is_rejected(tmp_path) -> None:
    ops, grants, root, _ = _ops(tmp_path)
    (root / "a.txt").write_text("old", encoding="utf-8")
    grant = grants.issue(scope=[str(root)], verbs=["write"])

    with pytest.raises(BrokerOpError) as exc:
        asyncio.run(
            ops.handle(
                _write_frame(
                    "host.write_text",
                    grant.grant_id,
                    {
                        "path": "a.txt",
                        "text": "new",
                        "expected_sha256": hashlib.sha256(
                            b"different"
                        ).hexdigest(),
                    },
                )
            )
        )
    assert exc.value.code == CONFLICT
    assert (root / "a.txt").read_text(encoding="utf-8") == "old"


def test_chunked_upload_commit_and_read_chunk(tmp_path) -> None:
    ops, grants, root, _ = _ops(tmp_path)
    grant = grants.issue(scope=[str(root)], verbs=["write", "read"])
    payload = (b"0123456789" * 1000) + b"tail"

    begin = asyncio.run(
        ops.handle(
            _write_frame(
                "host.write_begin",
                grant.grant_id,
                {"path": "big.bin"},
                op_id="op-begin",
            )
        )
    )
    upload_id = begin.data["upload_id"]
    offset = 0
    for index in range(0, len(payload), 256 * 1024):
        chunk = payload[index : index + 256 * 1024]
        async def _chunk(chunk=chunk, offset=offset):
            return await ops.handle(
                _write_frame(
                    "host.write_chunk",
                    grant.grant_id,
                    {
                        "upload_id": upload_id,
                        "offset": offset,
                        "data_b64": base64.b64encode(chunk).decode(
                            "ascii"
                        ),
                    },
                    op_id=f"op-chunk-{offset}",
                )
            )

        asyncio.run(_chunk())
        offset += len(chunk)
    committed = asyncio.run(
        ops.handle(
            _write_frame(
                "host.write_commit",
                grant.grant_id,
                {"upload_id": upload_id},
                op_id="op-commit",
            )
        )
    )
    assert (root / "big.bin").read_bytes() == payload
    assert committed.data["bytes"] == len(payload)

    first = asyncio.run(
        ops.handle(
            _write_frame(
                "host.read_chunk",
                grant.grant_id,
                {"path": "big.bin", "offset": 0, "length": 1024},
                op_id="op-read-1",
            )
        )
    )
    assert base64.b64decode(first.data["data_b64"]) == payload[:1024]
    assert first.data["eof"] is False

    last = asyncio.run(
        ops.handle(
            _write_frame(
                "host.read_chunk",
                grant.grant_id,
                {
                    "path": "big.bin",
                    "offset": len(payload) - 4,
                    "length": 1024,
                },
                op_id="op-read-2",
            )
        )
    )
    assert base64.b64decode(last.data["data_b64"]) == payload[-4:]
    assert last.data["eof"] is True


def test_write_many_is_atomic_for_snapshots(tmp_path) -> None:
    ops, grants, root, _ = _ops(tmp_path)
    grant = grants.issue(scope=[str(root)], verbs=["write"])
    uploads = []
    for index, name in enumerate(("a.txt", "b.txt")):
        begin = asyncio.run(
            ops.handle(
                _write_frame(
                    "host.write_begin",
                    grant.grant_id,
                    {"path": name},
                    op_id=f"op-begin-{index}",
                )
            )
        )
        upload_id = begin.data["upload_id"]
        uploads.append(upload_id)
        asyncio.run(
            ops.handle(
                _write_frame(
                    "host.write_chunk",
                    grant.grant_id,
                    {
                        "upload_id": upload_id,
                        "offset": 0,
                        "data_b64": base64.b64encode(
                            f"content-{name}".encode()
                        ).decode("ascii"),
                    },
                    op_id=f"op-chunk-{index}",
                )
            )
        )
    result = asyncio.run(
        ops.handle(
            _write_frame(
                "host.write_many_commit",
                grant.grant_id,
                {"uploads": uploads},
                op_id="op-many",
            )
        )
    )
    assert [item["snapshot_id"] for item in result.data["written"]]
    assert (root / "a.txt").read_text(encoding="utf-8") == "content-a.txt"
    assert (root / "b.txt").read_text(encoding="utf-8") == "content-b.txt"


def test_delete_and_mkdir(tmp_path) -> None:
    ops, grants, root, _ = _ops(tmp_path)
    (root / "a.txt").write_text("old", encoding="utf-8")
    grant = grants.issue(scope=[str(root)], verbs=["write"])

    deleted = asyncio.run(
        ops.handle(
            _write_frame(
                "host.delete",
                grant.grant_id,
                {"path": "a.txt"},
            )
        )
    )
    assert deleted.data["deleted"] is True
    assert deleted.data["snapshot_id"]
    assert not (root / "a.txt").exists()

    made = asyncio.run(
        ops.handle(
            _write_frame(
                "host.mkdir",
                grant.grant_id,
                {"path": "sub/dir"},
                op_id="op-mkdir",
            )
        )
    )
    assert made.data["path"] == "sub/dir"
    assert (root / "sub" / "dir").is_dir()


def test_zx_and_escape_rejected_for_write(tmp_path) -> None:
    ops, grants, root, _ = _ops(tmp_path, deny=[tmp_path / "project"])
    grant = grants.issue(scope=[str(root)], verbs=["write"])
    with pytest.raises(BrokerOpError) as exc:
        asyncio.run(
            ops.handle(
                _write_frame(
                    "host.write_text",
                    grant.grant_id,
                    {"path": "a.txt", "text": "x"},
                )
            )
        )
    assert exc.value.code == DENIED_ZX

    second = tmp_path / "second"
    second.mkdir()
    ops2, grants2, root2, _ = _ops(second)
    grant2 = grants2.issue(scope=[str(root2)], verbs=["write"])
    with pytest.raises(BrokerOpError):
        asyncio.run(
            ops2.handle(
                _write_frame(
                    "host.write_text",
                    grant2.grant_id,
                    {"path": "../outside.txt", "text": "x"},
                )
            )
        )
