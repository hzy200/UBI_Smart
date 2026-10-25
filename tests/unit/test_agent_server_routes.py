"""A3 状态与工作区路由：成功、限流、越界与错误映射。

直接注册路由并注入假 deps，绕开 token/上下文头；鉴权与装配由
`test_agent_server.py` / `test_server_entrypoint.py` 负责。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from coding_agent.agent_protocol import AgentContext
from coding_agent.agent_server_routes.state import StateDeps, register_state_routes
from coding_agent.agent_server_routes.workspace import (
    WorkspaceDeps,
    register_workspace_routes,
)
from coding_agent.audit import AuditLogger, AuditRecord
from coding_agent.audit.logger import now_iso
from coding_agent.config import Settings
from coding_agent.sandbox.snapshots import RestoreResult, SnapshotEntry

pytestmark = pytest.mark.regression


class _Runtime:
    def __init__(self) -> None:
        self.closed = 0
        self.limits: list[int] = []
        self.calls: list[tuple] = []
        self.fail = ""
        self.invalid_index = False

    def last_run_started_at(self, thread_id: str | None = None) -> str:
        return "2026-01-01T00:00:00+00:00"

    async def checkpoints(self, thread_id: str, *, limit: int = 20):
        self.limits.append(limit)
        if self.fail == "checkpoints":
            raise RuntimeError("checkpoint 不可用")
        return [{"checkpoint_id": "c1"}]

    async def fork(self, thread_id: str, *, checkpoint_id: str = "") -> str:
        if self.fail == "fork":
            raise RuntimeError("fork 失败")
        return "forked"

    async def rewind(
        self, thread_id: str, *, checkpoint_id: str = "", steps: int = 1
    ) -> str:
        self.calls.append(("rewind", steps, checkpoint_id))
        if self.fail == "rewind":
            raise RuntimeError("rewind 失败")
        return "cp-1"

    def memories(self, *, scope: str = "project"):
        return [f"{scope}:fact"]

    def remember(self, text: str, *, thread_id: str = "", scope: str = "project"):
        return [text]

    def forget(self, index: int, *, thread_id: str = "", scope: str = "project"):
        if self.invalid_index:
            raise IndexError("索引越界")
        return ["forgotten"]

    def clear_memory(self, *, thread_id: str = "", scope: str = "project"):
        return []

    def pin_memory(
        self,
        index: int,
        *,
        pinned: bool = True,
        thread_id: str = "",
        scope: str = "project",
    ):
        return [f"pin:{pinned}"]

    def set_memory_priority(
        self,
        index: int,
        priority: int,
        *,
        thread_id: str = "",
        scope: str = "project",
    ):
        return [f"priority:{priority}"]

    def list_snapshots(self, *, limit: int = 20):
        if self.fail == "snapshots":
            raise RuntimeError("快照不可用")
        return [SnapshotEntry("s1", "a.txt")]

    def diff_snapshot(self, *, path=None, snapshot_id=None) -> str:
        return f"diff:{path or snapshot_id}"

    def restore_since(self, prefix: str, *, dry_run: bool = False):
        return [RestoreResult(ok=True, path="a.txt", message=prefix)]

    def restore(self, *, path=None, snapshot_id=None) -> RestoreResult:
        return RestoreResult(ok=True, path=path or "a.txt")

    def store_upload(
        self, filename: str, data: bytes, *, thread_id: str = "", max_bytes: int = 0
    ):
        return {
            "path": f"/ws/.agent/uploads/{thread_id}/{filename}",
            "relative_path": f".agent/uploads/{thread_id}/{filename}",
            "bytes": len(data),
        }

    async def aclose(self) -> None:
        self.closed += 1


def _state_client(tmp_path: Path, runtime: _Runtime) -> TestClient:
    app = FastAPI()
    audit_dir = tmp_path / "audit"
    states: dict = {}

    async def close(runtime) -> None:
        await runtime.aclose()

    register_state_routes(
        app,
        StateDeps(
            context=lambda request: AgentContext(workspace=str(tmp_path)),
            new_runtime=lambda context: runtime,
            close_runtime=close,
            reject_active=lambda context, thread_id: None,
            audit_dir=lambda context: audit_dir,
            states=states,
        ),
    )
    return TestClient(app)


def _workspace_client(
    tmp_path: Path,
    runtime: _Runtime,
    **overrides,
) -> tuple[TestClient, Path]:
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    app = FastAPI()

    async def close(runtime) -> None:
        await runtime.aclose()

    settings = Settings(
        _env_file=None,
        audit_dir=str(tmp_path / "audit"),
        checkpoint_path=":memory:",
        wsl_workspace=str(workspace),
        **overrides,
    )
    register_workspace_routes(
        app,
        WorkspaceDeps(
            context=lambda request: AgentContext(workspace=str(workspace)),
            new_runtime=lambda context: runtime,
            close_runtime=close,
            reject_active=lambda context, thread_id: None,
            settings=settings,
        ),
    )
    return TestClient(app), workspace


# ---------------- state ----------------

def test_run_started_and_checkpoints(tmp_path) -> None:
    runtime = _Runtime()
    client = _state_client(tmp_path, runtime)

    assert client.get("/v1/run_started/t1").json()["prefix"].startswith("2026")
    response = client.get("/v1/checkpoints/t1?limit=0")
    assert response.status_code == 200
    assert response.json()["checkpoints"] == [{"checkpoint_id": "c1"}]
    assert runtime.limits == [1]


def test_checkpoints_error_maps_to_503(tmp_path) -> None:
    runtime = _Runtime()
    runtime.fail = "checkpoints"
    response = _state_client(tmp_path, runtime).get("/v1/checkpoints/t1")
    assert response.status_code == 503


def test_fork_and_rewind(tmp_path) -> None:
    runtime = _Runtime()
    client = _state_client(tmp_path, runtime)

    assert client.post("/v1/fork/t1").json() == {"thread_id": "forked"}
    assert (
        client.post("/v1/rewind/t1?steps=0&checkpoint_id=cp").json()
        == {"checkpoint_id": "cp-1"}
    )
    assert runtime.calls == [("rewind", 1, "cp")]


def test_fork_and_rewind_errors_map_to_409(tmp_path) -> None:
    runtime = _Runtime()
    client = _state_client(tmp_path, runtime)
    runtime.fail = "fork"
    assert client.post("/v1/fork/t1").status_code == 409
    runtime.fail = "rewind"
    assert client.post("/v1/rewind/t1").status_code == 409


def test_memory_actions(tmp_path) -> None:
    runtime = _Runtime()
    client = _state_client(tmp_path, runtime)

    assert client.get("/v1/memory?scope=project").json() == {
        "facts": ["project:fact"]
    }
    cases = [
        ({"action": "remember", "text": "x"}, ["x"]),
        ({"action": "forget", "index": 0}, ["forgotten"]),
        ({"action": "clear"}, []),
        ({"action": "pin", "index": 0, "pinned": False}, ["pin:False"]),
        ({"action": "priority", "index": 0, "priority": 3}, ["priority:3"]),
    ]
    for body, expected in cases:
        response = client.post("/v1/memory", json=body)
        assert response.status_code == 200, body
        assert response.json()["facts"] == expected

    runtime.invalid_index = True
    assert client.post("/v1/memory", json={"action": "forget"}).status_code == 422


def test_audit_sessions_and_recovery(tmp_path) -> None:
    runtime = _Runtime()
    logger = AuditLogger(tmp_path / "audit")
    logger.write(
        AuditRecord(
            ts=now_iso(),
            kind="run_start",
            thread_id="t1",
            detail="hello",
        )
    )
    client = _state_client(tmp_path, runtime)

    records = client.get("/v1/audit?thread_id=t1").json()["records"]
    assert records and records[0]["thread_id"] == "t1"
    assert client.get("/v1/sessions?q=nomatch").json()["sessions"] == []
    assert "items" in client.get("/v1/recovery").json()


# ---------------- workspace ----------------

def test_files_and_file_content(tmp_path) -> None:
    runtime = _Runtime()
    client, workspace = _workspace_client(tmp_path, runtime)
    (workspace / "a.txt").write_text("hi", encoding="utf-8")

    listing = client.get("/v1/files").json()
    assert any(item["name"] == "a.txt" for item in listing["entries"])
    assert client.get("/v1/file?path=a.txt").json()["content"] == "hi"

    assert client.get("/v1/files?path=../x").status_code == 422
    assert client.get("/v1/file?path=missing.txt").status_code == 422


def test_git_and_project_routes(tmp_path, monkeypatch) -> None:
    from coding_agent.agent_server_routes import workspace as routes

    runtime = _Runtime()
    client, _ = _workspace_client(tmp_path, runtime)
    monkeypatch.setattr(
        routes, "git_summary", lambda workspace, diff=False: {"branch": "main"}
    )
    monkeypatch.setattr(
        routes, "probe_project", lambda workspace: {"kind": "python"}
    )

    assert client.get("/v1/git?diff=true").json() == {"branch": "main"}
    assert client.get("/v1/project").json() == {
        "kind": "python",
        "desktop_mode": False,
    }

    def boom(*args, **kwargs):
        raise routes.BrowseError("not a repo")

    monkeypatch.setattr(routes, "git_summary", boom)
    assert client.get("/v1/git").status_code == 503


def test_snapshots_diff_and_restore(tmp_path) -> None:
    runtime = _Runtime()
    client, _ = _workspace_client(tmp_path, runtime)

    assert client.get("/v1/snapshots?thread_id=t1").json()["snapshots"] == [
        {"snapshot_id": "s1", "path": "a.txt"}
    ]
    assert client.get("/v1/diff?thread_id=t1&path=a.txt").json() == {
        "diff": "diff:a.txt"
    }
    assert client.post(
        "/v1/restore", json={"thread_id": "t1", "prefix": "p"}
    ).json()["results"][0]["ok"] is True
    assert client.post(
        "/v1/restore", json={"thread_id": "t1", "path": "a.txt"}
    ).json()["ok"] is True

    runtime.fail = "snapshots"
    assert client.get("/v1/snapshots?thread_id=t1").status_code == 503


def test_uploads_limits_and_listing(tmp_path) -> None:
    runtime = _Runtime()
    client, workspace = _workspace_client(
        tmp_path,
        runtime,
        web_max_upload_files=1,
        web_max_upload_bytes=4,
        web_max_upload_total_bytes=100,
    )

    too_many = client.post(
        "/v1/uploads/t1",
        files=[
            ("files", ("a.txt", b"a", "text/plain")),
            ("files", ("b.txt", b"b", "text/plain")),
        ],
    )
    assert too_many.status_code == 413

    too_large = client.post(
        "/v1/uploads/t1",
        files={"files": ("a.txt", b"12345", "text/plain")},
    )
    assert too_large.status_code == 413

    ok = client.post(
        "/v1/uploads/t1",
        files={"files": ("a.txt", b"123", "text/plain")},
    )
    assert ok.status_code == 200
    assert ok.json()["uploads"][0]["name"] == "a.txt"

    base = workspace / ".agent" / "uploads" / "t1"
    base.mkdir(parents=True, exist_ok=True)
    (base / "old.txt").write_bytes(b"x" * 10)
    listed = client.get("/v1/uploads/t1").json()
    assert listed["used_bytes"] == 10
    assert listed["uploads"][0]["relative_path"].startswith(".agent/uploads/t1/")


def test_uploads_total_quota_and_delete(tmp_path) -> None:
    runtime = _Runtime()
    client, workspace = _workspace_client(
        tmp_path,
        runtime,
        web_max_upload_bytes=100,
        web_max_upload_total_bytes=10,
    )
    base = workspace / ".agent" / "uploads" / "t1"
    base.mkdir(parents=True, exist_ok=True)
    (base / "old.txt").write_bytes(b"x" * 10)

    quota = client.post(
        "/v1/uploads/t1",
        files={"files": ("new.txt", b"y", "text/plain")},
    )
    assert quota.status_code == 413

    target = base / "old.txt"
    assert client.request(
        "DELETE",
        "/v1/uploads/t1",
        json={"path": ".agent/uploads/t1/old.txt"},
    ).json() == {"ok": True, "bytes": 10}
    assert not target.exists()

    missing = client.request(
        "DELETE",
        "/v1/uploads/t1",
        json={"path": ".agent/uploads/t1/old.txt"},
    )
    assert missing.status_code == 404
    traversal = client.request(
        "DELETE",
        "/v1/uploads/t1",
        json={"path": "../outside.txt"},
    )
    assert traversal.status_code == 422


def test_reject_active_is_called_for_restore(tmp_path) -> None:
    runtime = _Runtime()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    app = FastAPI()

    async def close(runtime) -> None:
        await runtime.aclose()

    def reject(context, thread_id):
        raise HTTPException(status_code=409, detail="运行中")

    register_workspace_routes(
        app,
        WorkspaceDeps(
            context=lambda request: AgentContext(workspace=str(workspace)),
            new_runtime=lambda context: runtime,
            close_runtime=close,
            reject_active=reject,
            settings=Settings(_env_file=None, wsl_workspace=str(workspace)),
        ),
    )
    response = TestClient(app).post(
        "/v1/restore", json={"thread_id": "t1"}
    )
    assert response.status_code == 409
    assert runtime.closed == 0
