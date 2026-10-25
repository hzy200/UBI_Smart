"""系统冒烟：只回答“主链路还能不能起来”。

这里刻意不碰 WSL、MySQL、LLM 与桌面 GUI：冒烟失败应先于单点测试排查。
深层行为由 `tests/unit` / `tests/integration` 负责，冒烟不重复断言细节。
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from coding_agent import agent_server, web
from coding_agent.audit import AuditLogger, AuditRecord
from coding_agent.audit.logger import now_iso
from coding_agent.config import Settings
from coding_agent.workspace.browse import list_directory, read_text_file

pytestmark = pytest.mark.smoke

CORE_MODULES = (
    "coding_agent",
    "coding_agent.config",
    "coding_agent.events",
    "coding_agent.agent_protocol",
    "coding_agent.event_transport",
    "coding_agent.runtime",
    "coding_agent.graph.build",
    "coding_agent.orchestrator.graph",
    "coding_agent.execution.service",
    "coding_agent.execution.host_ops",
    "coding_agent.sandbox.policy",
    "coding_agent.sandbox.pathguard",
    "coding_agent.tools.registry",
    "coding_agent.control.store",
    "coding_agent.control.protocol",
    "coding_agent.workspace.browse",
    "coding_agent.audit.logger",
    "coding_agent.web.app",
    "coding_agent.agent_server",
)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        audit_dir=str(tmp_path / "audit"),
        checkpoint_path=":memory:",
        deepseek_api_key="sk-smoke-not-real",
        wsl_workspace=str(tmp_path / "ws"),
        web_data_root=str(tmp_path / "web-data"),
        control_db_url=f"sqlite:///{tmp_path / 'control.db'}",
    )


@pytest.mark.parametrize("name", CORE_MODULES)
def test_core_modules_import(name: str) -> None:
    """发布包缺文件/循环导入会让系统起不来，冒烟先挡这一层。"""
    assert importlib.import_module(name) is not None


def test_a2_web_health_and_session(tmp_path: Path) -> None:
    client = TestClient(web.create_app(_settings(tmp_path)))

    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    created = client.post("/api/sessions", json={"thread_id": "smoke-1"})
    assert created.status_code == 200

    sessions = client.get("/api/sessions")
    assert sessions.status_code == 200
    assert any(
        row.get("thread_id") == "smoke-1"
        for row in sessions.json().get("sessions", [])
    )


def test_a2_workspace_registry_smoke(tmp_path: Path) -> None:
    settings = _settings(tmp_path).model_copy(
        update={
            "workspace_registry": True,
            "workspace_root": "/tmp/smoke-ws",
            "workspace_mount_roots": "/mnt/d",
        }
    )
    client = TestClient(web.create_app(settings))
    created = client.post(
        "/api/workspaces",
        json={
            "display_name": "Smoke",
            "source": "local_import",
            "root_path": "/tmp/smoke-ws/smoke",
        },
    )
    assert created.status_code == 200, created.text
    workspace_id = created.json()["workspace_id"]
    assert created.json()["status"] == "active"

    session = client.post(
        "/api/sessions",
        json={"thread_id": "smoke-ws", "workspace_id": workspace_id},
    )
    assert session.status_code == 200, session.text
    assert session.json()["workspace_id"] == workspace_id

    revisions = client.get(
        f"/api/workspaces/{workspace_id}/revisions"
    )
    assert revisions.status_code == 200
    assert revisions.json()["revisions"] == []


def test_a3_requires_internal_token(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("AGENT_INTERNAL_TOKEN", raising=False)
    with pytest.raises(ValueError, match="AGENT_INTERNAL_TOKEN"):
        agent_server.create_agent_app(_settings(tmp_path), token="")


def test_a3_health_and_meta_auth(tmp_path: Path) -> None:
    client = TestClient(
        agent_server.create_agent_app(_settings(tmp_path), token="smoke-token")
    )

    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json()["service"] == "agent-server"

    assert client.get("/v1/meta").status_code == 401
    meta = client.get(
        "/v1/meta", headers={"Authorization": "Bearer smoke-token"}
    )
    assert meta.status_code == 200
    assert meta.json()["capabilities"]


def test_audit_write_and_redact_smoke(tmp_path: Path) -> None:
    secret = "sk-smoke-SECRET-0123456789"
    logger = AuditLogger(tmp_path)
    logger.write(
        AuditRecord(
            ts=now_iso(),
            kind="tool_call",
            thread_id="smoke-1",
            tool="shell_exec",
            decision="auto",
            ok=True,
            detail=f"failed with {secret}",
        )
    )

    raw = logger.path.read_text(encoding="utf-8")
    assert secret not in raw
    assert "REDACTED" in raw


def test_workspace_browse_smoke(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "hello.txt").write_text("hi", encoding="utf-8")

    listing = list_directory(str(workspace))
    assert any(
        entry.get("name") == "hello.txt" for entry in listing["entries"]
    )
    content = read_text_file(str(workspace), "hello.txt")
    assert content["content"] == "hi"
