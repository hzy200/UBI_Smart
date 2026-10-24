"""GUI 新增接口：会话配置、模型切换、审批读取、快照/回滚、上传。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from coding_agent import web
from coding_agent.config import Settings
from coding_agent.events import ApprovalRequested, RunFinished, RunStarted
from coding_agent.runtime import HistoryMessage
from coding_agent.sandbox.snapshots import RestoreResult, SnapshotEntry
from coding_agent.web import local_runtime
from coding_agent.web.auth import AuthConfig


class FakeRuntime:
    def __init__(self, *args, **kwargs) -> None:
        self.kwargs = kwargs
        self.closed = False
        self.steered: list[tuple[str, str]] = []

    async def run(self, prompt: str, *, thread_id: str):
        yield RunStarted(thread_id=thread_id)
        yield RunFinished(thread_id=thread_id, answer=prompt)

    async def resume(self, thread_id: str, decisions: dict[str, bool]):
        yield RunFinished(thread_id=thread_id, answer="resumed")

    async def resume_steering(self, thread_id: str):
        yield RunFinished(thread_id=thread_id, answer="resumed")

    def steer(self, thread_id: str, action: str, **kwargs) -> None:
        self.steered.append((thread_id, action))

    def is_paused(self, thread_id: str) -> bool:
        return False

    async def pending_approvals(self, thread_id: str):
        return [
            ApprovalRequested(
                request_id="c1",
                tool="shell_exec",
                command="pip install x",
                level="L2 变更性",
            )
        ]

    async def history(self, thread_id: str, *, limit=None, offset=0):
        return [HistoryMessage("user", "hi")]

    def list_snapshots(self, *, limit: int = 20):
        return [SnapshotEntry("20260101T000000-a1", "a.py")]

    def diff_snapshot(self, *, path=None, snapshot_id=None) -> str:
        return "--- a/a.py\n+++ b/a.py\n"

    def restore(self, *, path=None, snapshot_id=None):
        return RestoreResult(ok=True, path=path or "", snapshot_id=snapshot_id or "")

    def restore_since(self, prefix: str, *, dry_run: bool = False):
        return [RestoreResult(ok=True, path="a.py", snapshot_id="s1")]

    def last_run_started_at(self, thread_id: str | None = None) -> str:
        return "20260101T000000"

    async def checkpoints(self, thread_id: str, *, limit: int = 20):
        return [{"checkpoint_id": "c1", "step": 0, "preview": "hi"}]

    async def fork(self, thread_id: str, *, checkpoint_id: str = "", new_thread_id: str = ""):
        return f"{thread_id}-fork"

    async def rewind(self, thread_id: str, *, checkpoint_id: str = "", steps: int = 1):
        return "c0"

    async def compact_thread(
        self,
        thread_id: str,
        *,
        keep_recent: int = 6,
        new_thread_id: str = "",
    ):
        return f"{thread_id}-compact"

    def memories(self, *, scope=None):
        return ["项目使用 pytest"]

    def remember(self, text: str, *, thread_id="", scope="project"):
        return [text]

    def forget(self, index: int, *, thread_id="", scope="project"):
        return []

    def clear_memory(self, *, thread_id="", scope="project"):
        return []

    def pin_memory(self, index: int, *, pinned: bool, thread_id="", scope="project"):
        return []

    def set_memory_priority(self, index: int, priority: int, *, thread_id="", scope="project"):
        return []

    def store_upload(self, filename: str, data: bytes, *, thread_id="", max_bytes=0):
        return {
            "path": f"/tmp/ws/.agent/uploads/{filename}",
            "relative_path": f".agent/uploads/{filename}",
            "bytes": len(data),
        }

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        audit_dir=str(tmp_path / "audit"),
        checkpoint_path=":memory:",
        deepseek_api_key="sk-test",
        wsl_workspace="/tmp/ws",
    )


@pytest.fixture
def runtime(monkeypatch) -> SimpleNamespace:
    state = SimpleNamespace(kwargs=None, instances=[])

    def factory(*args, **kwargs):
        instance = FakeRuntime(*args, **kwargs)
        state.kwargs = kwargs
        state.instances.append(instance)
        return instance

    monkeypatch.setattr(local_runtime, "AgentRuntime", factory)
    return state


def test_project_endpoint_resolves_workspace_and_reports_desktop_mode(
    settings, runtime, monkeypatch
) -> None:
    seen: list[str] = []

    def fake_probe(target: str) -> dict:
        seen.append(target)
        return {
            "workspace": target,
            "mode": "native",
            "exists": True,
            "writable": True,
            "agent_python": "/opt/agent/python",
            "agent_version": "3.12.4",
            "project": {
                "python": f"{target}/.venv/bin/python",
                "version": "3.12.4",
                "source": ".venv",
                "pytest": True,
            },
            "package_manager": "uv",
            "lockfile": "uv.lock",
            "test_command": f"{target}/.venv/bin/python -m pytest -q",
            "warnings": [],
        }

    monkeypatch.setattr(web.app, "probe_project", fake_probe)
    client = TestClient(
        web.create_app(settings, desktop_mode=True)
    )
    response = client.get("/api/project", params={"workspace": "/tmp/ws"})
    assert response.status_code == 200
    data = response.json()
    assert seen == ["/tmp/ws"]
    assert data["desktop_mode"] is True
    assert data["project"]["source"] == ".venv"
    assert data["package_manager"] == "uv"

    capabilities = client.get("/api/capabilities").json()
    assert capabilities["desktop_mode"] is True


def test_session_binds_workspace_and_access_mode(settings, runtime) -> None:
    client = TestClient(web.create_app(settings))
    created = client.post(
        "/api/sessions",
        json={"workspace": "/tmp/ws", "access_mode": "read_only"},
    )
    assert created.status_code == 200
    data = created.json()
    assert data["workspace"] == "/tmp/ws"
    assert data["access_mode"] == "read_only"
    assert data["capabilities"]["read_only"] is True
    assert data["capabilities"]["server_write_enabled"] is False

    run = client.post(
        "/api/run", json={"prompt": "hi", "thread_id": data["thread_id"]}
    )
    assert run.status_code == 200
    assert runtime.kwargs["access_mode"] == "read_only"
    assert runtime.kwargs["workspace"] == "/tmp/ws"


def test_session_config_survives_app_restart(settings, runtime) -> None:
    created = TestClient(web.create_app(settings)).post(
        "/api/sessions",
        json={"workspace": "/tmp/ws", "access_mode": "read_only"},
    ).json()
    thread = created["thread_id"]

    # 新 app 实例模拟进程重启；配置从租户审计目录里的 JSON 恢复。
    restarted = TestClient(web.create_app(settings))
    capabilities = restarted.get(f"/api/capabilities?thread_id={thread}").json()
    assert capabilities["workspace"] == "/tmp/ws"
    assert capabilities["access_mode"] == "read_only"


def test_legacy_session_uses_audit_workspace(settings, runtime) -> None:
    from coding_agent.audit import AuditLogger, AuditRecord

    AuditLogger(settings.resolved_audit_dir).write(
        AuditRecord(
            ts="2026-01-01T00:00:00+00:00",
            kind="run_start",
            thread_id="legacy",
            workspace="/tmp/legacy-ws",
            detail="旧会话",
        )
    )
    client = TestClient(web.create_app(settings))
    rows = client.get("/api/sessions").json()["sessions"]
    row = next(item for item in rows if item["thread_id"] == "legacy")
    assert row["workspace"] == "/tmp/legacy-ws"

    client.get("/api/snapshots?thread_id=legacy")
    assert runtime.kwargs["workspace"] == "/tmp/legacy-ws"


def test_created_session_is_visible_before_its_first_run(
    settings, runtime
) -> None:
    client = TestClient(web.create_app(settings))
    created = client.post(
        "/api/sessions",
        json={"workspace": "/tmp/ws", "access_mode": "read_only"},
    ).json()
    listed = client.get("/api/sessions").json()["sessions"]
    assert any(item["thread_id"] == created["thread_id"] for item in listed)


def test_unknown_provider_is_rejected(settings, runtime) -> None:
    client = TestClient(web.create_app(settings))
    response = client.post(
        "/api/sessions",
        json={
            "workspace": "/tmp/ws",
            "access_mode": "read_only",
            "provider": "does-not-exist",
        },
    )
    assert response.status_code == 422


def test_persisted_write_mode_cannot_bypass_readonly_server(
    settings, runtime
) -> None:
    writer = TestClient(web.create_app(settings, allow_write=True))
    created = writer.post(
        "/api/sessions",
        json={"workspace": "/tmp/ws", "access_mode": "write_auto"},
    ).json()
    thread = created["thread_id"]

    readonly = TestClient(web.create_app(settings, allow_write=False))
    caps = readonly.get(f"/api/capabilities?thread_id={thread}").json()
    assert caps["read_only"] is True
    assert caps["write_enabled"] is False

    readonly.post("/api/run", json={"prompt": "hi", "thread_id": thread})
    assert runtime.kwargs["access_mode"] == "read_only"


def test_write_mode_requires_server_write_flag(settings, runtime) -> None:
    client = TestClient(web.create_app(settings))
    denied = client.post(
        "/api/sessions",
        json={"workspace": "/tmp/ws", "access_mode": "write_auto"},
    )
    assert denied.status_code == 403

    allowed_client = TestClient(web.create_app(settings, allow_write=True))
    allowed = allowed_client.post(
        "/api/sessions",
        json={"workspace": "/tmp/ws", "access_mode": "write_auto"},
    )
    assert allowed.status_code == 200
    assert allowed.json()["access_mode"] == "write_auto"


def test_model_switch_is_idle_only_and_reaches_runtime(settings, runtime) -> None:
    client = TestClient(web.create_app(settings))
    created = client.post(
        "/api/sessions",
        json={"workspace": "/tmp/ws", "access_mode": "read_only"},
    ).json()
    thread = created["thread_id"]

    switched = client.post(
        f"/api/sessions/{thread}/model",
        json={"provider": "deepseek", "model": "deepseek-reasoner"},
    )
    assert switched.status_code == 200
    client.post("/api/run", json={"prompt": "hi", "thread_id": thread})
    assert runtime.kwargs["provider_name"] == "deepseek"
    assert runtime.kwargs["model_override"] == "deepseek-reasoner"


def test_pending_approvals_and_snapshots(settings, runtime) -> None:
    client = TestClient(web.create_app(settings))
    created = client.post(
        "/api/sessions",
        json={"workspace": "/tmp/ws", "access_mode": "read_only"},
    ).json()
    thread = created["thread_id"]

    approvals = client.get(f"/api/approvals/{thread}").json()
    assert approvals["approvals"][0]["request_id"] == "c1"

    snapshots = client.get(f"/api/snapshots?thread_id={thread}").json()
    assert snapshots["snapshots"][0]["path"] == "a.py"

    diff = client.get(f"/api/diff?thread_id={thread}&path=a.py").json()
    assert "a.py" in diff["diff"]

    restored = client.post(
        "/api/restore",
        json={"thread_id": thread, "path": "a.py", "snapshot_id": "s1"},
    ).json()
    assert restored["ok"] is True

    checkpoints = client.get(f"/api/checkpoints/{thread}").json()
    assert checkpoints["checkpoints"][0]["checkpoint_id"] == "c1"

    forked = client.post(f"/api/fork/{thread}").json()
    assert forked["thread_id"].endswith("-fork")


def test_compact_creates_a_new_session(settings, runtime) -> None:
    client = TestClient(web.create_app(settings))
    created = client.post(
        "/api/sessions",
        json={"workspace": "/tmp/ws", "access_mode": "read_only"},
    ).json()
    thread = created["thread_id"]
    response = client.post(
        f"/api/context/{thread}/compact",
        json={"keep_recent": 4},
    )
    assert response.status_code == 200
    assert response.json()["thread_id"].endswith("-compact")
    # 新会话配置已绑定，可直接继续。
    capabilities = client.get(
        f"/api/capabilities?thread_id={response.json()['thread_id']}"
    ).json()
    assert capabilities["workspace"] == "/tmp/ws"


def test_upload_goes_to_agent_uploads(settings, runtime) -> None:
    client = TestClient(web.create_app(settings))
    created = client.post(
        "/api/sessions",
        json={"workspace": "/tmp/ws", "access_mode": "read_only"},
    ).json()
    thread = created["thread_id"]

    response = client.post(
        f"/api/sessions/{thread}/uploads",
        files=[("files", ("a.txt", b"hello", "text/plain"))],
    )
    assert response.status_code == 200
    uploaded = response.json()["uploads"][0]
    assert uploaded["relative_path"] == ".agent/uploads/a.txt"
    assert uploaded["bytes"] == 5


def test_health_uses_the_same_secret_store_as_runtime(
    settings, monkeypatch
) -> None:
    class FakeStore:
        data = {("coding-agent", "deepseek"): "sk-from-store"}

        def __init__(self, service: str = "coding-agent") -> None:
            self.service = service

        def get(self, name: str) -> str:
            return self.data.get((self.service, name), "")

        def has(self, name: str) -> bool:
            return bool(self.get(name))

        def set(self, name: str, value: str) -> None:
            self.data[(self.service, name)] = value

        def delete(self, name: str) -> None:
            self.data.pop((self.service, name), None)

    monkeypatch.setattr(web.app, "SecretStore", FakeStore)
    settings = settings.model_copy(update={"deepseek_api_key": ""})
    client = TestClient(web.create_app(settings))
    data = client.get("/api/health").json()
    assert data["api_key"] is True
    assert data["reachable"] is True


def test_health_reports_broken_provider_config(settings) -> None:
    settings = settings.model_copy(
        update={"llm_providers": "{not-json"}
    )
    client = TestClient(web.create_app(settings))
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["reachable"] is False
    assert "ProviderConfigError" in response.json()["error"]


def test_provider_connection_probe(settings, runtime, monkeypatch) -> None:
    seen: dict[str, str] = {}

    class FakeResponse:
        status_code = 200

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            seen["trust_env"] = kwargs.get("trust_env")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, headers=None):
            seen["url"] = url
            seen["auth"] = (headers or {}).get("Authorization", "")
            return FakeResponse()

    monkeypatch.setattr(web.app.httpx, "AsyncClient", FakeClient)
    client = TestClient(web.create_app(settings))
    response = client.post(
        "/api/providers/test", json={"name": "deepseek"}
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert seen["url"].endswith("/models")
    assert seen["auth"].startswith("Bearer ")
    assert seen["trust_env"] is False


def test_provider_connection_probe_requires_key(settings, runtime) -> None:
    settings = settings.model_copy(update={"deepseek_api_key": ""})
    client = TestClient(web.create_app(settings))
    response = client.post(
        "/api/providers/test", json={"name": "deepseek"}
    )
    assert response.status_code == 422


def test_multi_user_keys_are_stored_per_user(settings, runtime, monkeypatch) -> None:
    class FakeStore:
        data: dict[tuple[str, str], str] = {}

        def __init__(self, service: str = "coding-agent") -> None:
            self.service = service

        def get(self, name: str) -> str:
            return self.data.get((self.service, name), "")

        def set(self, name: str, value: str) -> None:
            self.data[(self.service, name)] = value

        def delete(self, name: str) -> None:
            self.data.pop((self.service, name), None)

        def has(self, name: str) -> bool:
            return bool(self.get(name))

    FakeStore.data = {}
    monkeypatch.setattr(web.app, "SecretStore", FakeStore)

    settings = settings.model_copy(update={"deepseek_api_key": ""})
    auth = AuthConfig(
        tokens={"tok-a": "alice", "tok-b": "bob"},
        allow_anonymous_local=False,
    )
    client = TestClient(web.create_app(settings, auth, multi_user=True))
    headers = {"Authorization": "Bearer tok-a"}

    before = client.get("/api/providers", headers=headers).json()
    assert before["providers"][0]["has_key"] is False

    saved = client.post(
        "/api/providers/deepseek/key",
        headers=headers,
        json={"api_key": "sk-alice"},
    )
    assert saved.status_code == 200
    after = client.get("/api/providers", headers=headers).json()
    assert after["providers"][0]["has_key"] is True

    # 另一个用户看不到 alice 的 key。
    bob = client.get(
        "/api/providers", headers={"Authorization": "Bearer tok-b"}
    ).json()
    assert bob["providers"][0]["has_key"] is False


def test_upload_total_quota(settings, runtime) -> None:
    settings = settings.model_copy(
        update={"web_max_upload_total_bytes": 6}
    )
    client = TestClient(web.create_app(settings))
    created = client.post(
        "/api/sessions",
        json={"workspace": "/tmp/ws", "access_mode": "read_only"},
    ).json()
    thread = created["thread_id"]

    first = client.post(
        f"/api/sessions/{thread}/uploads",
        files=[("files", ("a.txt", b"12345", "text/plain"))],
    )
    assert first.status_code == 200
    second = client.post(
        f"/api/sessions/{thread}/uploads",
        files=[("files", ("b.txt", b"12345", "text/plain"))],
    )
    assert second.status_code == 413

    # 新建同工作区的会话也不能绕过工作区累计配额。
    other = client.post(
        "/api/sessions",
        json={"workspace": "/tmp/ws", "access_mode": "read_only"},
    ).json()["thread_id"]
    third = client.post(
        f"/api/sessions/{other}/uploads",
        files=[("files", ("c.txt", b"12345", "text/plain"))],
    )
    assert third.status_code == 413


def test_memory_endpoints(settings, runtime) -> None:
    client = TestClient(web.create_app(settings))
    assert client.get("/api/memory").json()["facts"] == ["项目使用 pytest"]
    added = client.post(
        "/api/memory",
        json={"action": "remember", "text": "使用 pytest"},
    )
    assert added.status_code == 200
    assert added.json()["facts"] == ["使用 pytest"]
