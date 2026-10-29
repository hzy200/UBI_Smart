"""Phase 4：会话管理、文件/Git、恢复中心与 provider 档案。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from coding_agent import web
from coding_agent.audit import AuditLogger, AuditRecord
from coding_agent.config import Settings
from coding_agent.identity.provider_profiles import (
    ProviderProfile,
    ProviderProfileStore,
    merge_provider_items,
    profile_from_payload,
)
from coding_agent.web import local_runtime
from coding_agent.web.routes import workspace as workspace_routes


class FakeRuntime:
    def __init__(self, *args, **kwargs) -> None:
        self.workspace = kwargs.get("workspace") or ""

    async def history(self, thread_id, *, limit=None, offset=0):
        return [
            SimpleNamespace(role="user", text="你好"),
            SimpleNamespace(role="assistant", text="你好，有什么可以帮你？"),
        ]

    async def pending_approvals(self, thread_id):
        return []

    def is_paused(self, thread_id) -> bool:
        return False

    async def aclose(self) -> None:
        pass


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        audit_dir=str(tmp_path / "audit"),
        checkpoint_path=":memory:",
        deepseek_api_key="sk-test",
        wsl_workspace="/tmp/ws",
    )


def test_session_rename_search_export_and_delete(settings, monkeypatch) -> None:
    monkeypatch.setattr(local_runtime, "AgentRuntime", FakeRuntime)
    client = TestClient(web.create_app(settings))
    created = client.post(
        "/api/sessions", json={"workspace": "/tmp/ws"}
    ).json()
    thread = created["thread_id"]

    renamed = client.post(
        f"/api/sessions/{thread}/rename", json={"title": "  修复 登录  "}
    )
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "修复 登录"

    rows = client.get("/api/sessions", params={"q": "登录"}).json()["sessions"]
    assert [row["thread_id"] for row in rows] == [thread]

    meta = client.post(
        f"/api/sessions/{thread}/meta",
        json={"pinned": True, "tags": ["重要", "UI"], "unread": True},
    )
    assert meta.status_code == 200
    assert meta.json()["pinned"] is True
    assert meta.json()["tags"] == ["重要", "UI"]
    assert meta.json()["unread"] is True
    listed = client.get("/api/sessions", params={"q": "登录"}).json()["sessions"]
    assert listed[0]["unread"] is True

    read = client.post(
        f"/api/sessions/{thread}/meta", json={"unread": False}
    )
    assert read.json()["unread"] is False

    archived = client.post(
        f"/api/sessions/{thread}/meta", json={"archived": True}
    )
    assert archived.status_code == 200
    assert archived.json()["archived"] is True
    assert client.get("/api/sessions").json()["sessions"] == []
    archived_rows = client.get(
        "/api/sessions",
        params={"include_archived": "true", "q": "重要"},
    ).json()["sessions"]
    assert [row["thread_id"] for row in archived_rows] == [thread]
    client.post(f"/api/sessions/{thread}/meta", json={"archived": False})

    exported = client.get(
        f"/api/sessions/{thread}/export", params={"format": "markdown"}
    ).json()
    assert exported["title"] == "修复 登录"
    assert "## 用户" in exported["content"]
    assert "你好" in exported["content"]

    assert client.delete(f"/api/sessions/{thread}").status_code == 200
    assert client.get("/api/sessions").json()["sessions"] == []
    assert client.get(f"/api/sessions/{thread}/export").status_code == 410
    assert (
        client.post("/api/sessions", json={"thread_id": thread}).status_code
        == 410
    )


def test_interrupted_session_appears_in_recovery(settings, monkeypatch) -> None:
    monkeypatch.setattr(local_runtime, "AgentRuntime", FakeRuntime)
    AuditLogger(settings.resolved_audit_dir).write(
        AuditRecord(
            ts="2026-01-01T00:00:00+00:00",
            kind="run_start",
            thread_id="crashed",
            workspace="/tmp/ws",
            detail="跑到一半",
        )
    )
    client = TestClient(web.create_app(settings))
    items = client.get("/api/recovery").json()["items"]
    assert items == [
        {
            "thread_id": "crashed",
            "title": "跑到一半",
            "workspace": "/tmp/ws",
            "last_active": "2026-01-01 00:00:00",
            "status": "interrupted",
            "approvals": 0,
        }
    ]


def test_uploads_can_be_listed_and_deleted(
    settings, monkeypatch, tmp_path
) -> None:
    # 单用户解析会做 Windows→/mnt 转换；这里直接用宿主机路径测文件操作。
    monkeypatch.setattr(
        "coding_agent.identity.tenancy.normalize_root", lambda value: str(value)
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    uploads = workspace / ".agent" / "uploads" / "t1"
    uploads.mkdir(parents=True)
    (uploads / "a.txt").write_text("hello", encoding="utf-8")

    client = TestClient(web.create_app(settings))
    created = client.post(
        "/api/sessions",
        json={"thread_id": "t1", "workspace": str(workspace)},
    )
    assert created.status_code == 200

    listed = client.get("/api/sessions/t1/uploads").json()
    assert listed["used_bytes"] == 5
    assert [row["relative_path"] for row in listed["uploads"]] == [
        ".agent/uploads/t1/a.txt"
    ]

    deleted = client.request(
        "DELETE",
        "/api/sessions/t1/uploads",
        json={"path": ".agent/uploads/t1/a.txt"},
    )
    assert deleted.status_code == 200
    assert deleted.json()["bytes"] == 5
    assert not (uploads / "a.txt").exists()
    assert client.get("/api/sessions/t1/uploads").json()["uploads"] == []


def test_capabilities_expose_budget_and_upload_limits(settings) -> None:
    settings = settings.model_copy(
        update={"run_cost_budget_usd": 2.5, "web_max_upload_bytes": 1024}
    )
    client = TestClient(web.create_app(settings))
    data = client.get("/api/capabilities").json()
    assert data["run_cost_budget_usd"] == 2.5
    assert data["max_upload_bytes"] == 1024


def test_files_and_git_endpoints_use_session_workspace(
    settings, monkeypatch
) -> None:
    seen: dict[str, str] = {}

    def fake_list(workspace: str, path: str) -> dict:
        seen["workspace"] = workspace
        seen["path"] = path
        return {"path": path, "entries": [], "truncated": False}

    def fake_git(workspace: str, *, diff: bool = False) -> dict:
        seen["git"] = workspace
        seen["diff"] = str(diff)
        return {"is_repo": True, "changes": [], "commits": []}

    monkeypatch.setattr(workspace_routes, "list_directory", fake_list)
    monkeypatch.setattr(workspace_routes, "git_summary", fake_git)
    client = TestClient(web.create_app(settings))
    thread = client.post(
        "/api/sessions", json={"workspace": "/tmp/ws"}
    ).json()["thread_id"]

    listing = client.get(
        "/api/files", params={"thread_id": thread, "path": "src"}
    )
    assert listing.status_code == 200
    assert seen == {"workspace": "/tmp/ws", "path": "src"}

    git = client.get(
        "/api/git", params={"thread_id": thread, "diff": "true"}
    )
    assert git.status_code == 200
    assert seen["git"] == "/tmp/ws"
    assert seen["diff"] == "True"


def test_search_endpoint_uses_session_workspace(settings, monkeypatch) -> None:
    seen: dict[str, object] = {}

    def fake_search(workspace: str, query: str, *, limit: int = 50) -> dict:
        seen["workspace"] = workspace
        seen["query"] = query
        seen["limit"] = limit
        return {
            "query": query,
            "results": [{"path": "a.py", "line": 3, "kind": "content"}],
            "truncated": False,
        }

    monkeypatch.setattr(workspace_routes, "search_workspace", fake_search)
    client = TestClient(web.create_app(settings))
    thread = client.post(
        "/api/sessions", json={"workspace": "/tmp/ws"}
    ).json()["thread_id"]

    response = client.get(
        "/api/search",
        params={"thread_id": thread, "q": "needle", "limit": 5},
    )
    assert response.status_code == 200
    assert response.json()["results"][0]["path"] == "a.py"
    assert response.json()["content_search"] is True
    assert seen == {"workspace": "/tmp/ws", "query": "needle", "limit": 5}


def test_provider_profile_crud_changes_provider_rows(
    settings, tmp_path
) -> None:
    client = TestClient(web.create_app(settings))
    payload = {
        "name": "local",
        "base_url": "http://127.0.0.1:8000/v1",
        "model": "qwen",
        "input_cost_per_1m": 0.1,
    }
    saved = client.post("/api/provider-profiles", json=payload)
    assert saved.status_code == 200
    assert [row["name"] for row in saved.json()["profiles"]] == ["local"]
    assert (tmp_path / "provider-profiles.json").is_file()

    rows = client.get("/api/providers").json()["providers"]
    assert [row["name"] for row in rows] == ["local"]
    assert rows[0]["base_url"] == "http://127.0.0.1:8000/v1"

    deleted = client.delete("/api/provider-profiles/local")
    assert deleted.status_code == 200
    rows = client.get("/api/providers").json()["providers"]
    assert [row["name"] for row in rows] == ["deepseek"]


def test_multi_user_rejects_provider_profile_writes(settings) -> None:
    from coding_agent.web.auth import AuthConfig

    auth = AuthConfig(
        tokens={"tok-a": "alice"}, allow_anonymous_local=False
    )
    client = TestClient(web.create_app(settings, auth, multi_user=True))
    response = client.post(
        "/api/provider-profiles",
        headers={"Authorization": "Bearer tok-a"},
        json={"name": "x", "base_url": "https://x.test/v1", "model": "m"},
    )
    assert response.status_code == 403


def test_provider_profile_validation_and_merge() -> None:
    with pytest.raises(ValueError):
        profile_from_payload({"name": "x", "base_url": "ftp://x", "model": "m"})
    with pytest.raises(ValueError):
        profile_from_payload({"name": "x", "base_url": "https://x", "model": ""})

    base = json.dumps(
        [{"name": "deepseek", "model": "chat", "api_key": "sk"}]
    )
    profile = ProviderProfile(
        name="deepseek",
        base_url="https://proxy.test/v1",
        model="chat-2",
    )
    merged = merge_provider_items(base, [profile])
    assert merged == [
        {
            "name": "deepseek",
            "model": "chat-2",
            "api_key": "sk",
            "base_url": "https://proxy.test/v1",
            "enabled": True,
            "priority": 0,
            "input_cost_per_1m": 0.0,
            "output_cost_per_1m": 0.0,
            "max_tokens": 0.0,
            "max_cost": 0.0,
            "capabilities": [],
            "task_types": [],
        }
    ]


def test_profile_store_ignores_corrupt_rows(tmp_path) -> None:
    path = tmp_path / "providers.json"
    path.write_text(
        json.dumps(
            [
                {"name": "ok", "base_url": "https://x", "model": "m"},
                {"name": "bad", "base_url": "nope", "model": ""},
            ]
        ),
        encoding="utf-8",
    )
    store = ProviderProfileStore(path)
    assert [profile.name for profile in store.load()] == ["ok"]
    store.upsert(ProviderProfile("second", "https://y", "m2"))
    assert [profile.name for profile in store.load()] == ["ok", "second"]
