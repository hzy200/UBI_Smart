"""M4：A2→A3 context HMAC 签名。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from coding_agent.agent_protocol import (
    AgentContext,
    sign_context,
    verify_context,
)
from coding_agent.config import Settings
from coding_agent.control.agent_client import HttpAgentClient
from coding_agent.servers import agent_server

TOKEN = "tok"
KEY = "hmac-secret"


class _Runtime:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def list_snapshots(self, limit: int = 20):
        return []

    async def aclose(self) -> None:
        return None


def _settings(tmp_path, *, key: str = KEY) -> Settings:
    return Settings(
        _env_file=None,
        audit_dir=str(tmp_path / "audit"),
        checkpoint_path=":memory:",
        deepseek_api_key="sk-test",
        wsl_workspace="/tmp/ws",
        agent_data_root=str(tmp_path / "agent"),
        agent_internal_hmac_key=key,
    )


def _app(tmp_path, *, key: str = KEY) -> TestClient:
    return TestClient(
        agent_server.create_agent_app(
            _settings(tmp_path, key=key),
            token=TOKEN,
            runtime_factory=_Runtime,
        )
    )


def _headers(*, signature: str | None) -> dict[str, str]:
    context = AgentContext(workspace="/tmp/ws")
    header = context.to_header()
    data = {
        "Authorization": f"Bearer {TOKEN}",
        "X-Agent-Context": header,
    }
    if signature is not None:
        data["X-Agent-Signature"] = signature
    return data


def test_sign_and_verify() -> None:
    header = AgentContext(workspace="/tmp/ws").to_header()
    signature = sign_context(header, KEY)
    assert verify_context(header, signature, KEY) is True
    assert verify_context(header, signature, "other") is False
    assert verify_context(header, "", KEY) is False
    assert verify_context(header, "", "") is True


def test_required_mode_is_fail_closed() -> None:
    """要求验签却没配 key：必须拒绝，不能退化成"只靠 Bearer token"。"""
    header = AgentContext(workspace="/tmp/ws").to_header()
    signature = sign_context(header, KEY)
    assert verify_context(header, signature, KEY, required=True) is True
    assert verify_context(header, signature, "other", required=True) is False
    assert verify_context(header, "", "", required=True) is False
    assert verify_context(header, signature, "", required=True) is False


def test_app_refuses_to_start_when_required_key_is_missing(tmp_path) -> None:
    with pytest.raises(ValueError):
        agent_server.create_agent_app(
            _settings(tmp_path, key=""),
            token=TOKEN,
            runtime_factory=_Runtime,
            hmac_required=True,
        )


def test_required_mode_rejects_unsigned_context(tmp_path) -> None:
    client = TestClient(
        agent_server.create_agent_app(
            _settings(tmp_path),
            token=TOKEN,
            runtime_factory=_Runtime,
            hmac_required=True,
        )
    )
    response = client.get(
        "/v1/snapshots",
        params={"thread_id": "t1"},
        headers=_headers(signature=None),
    )
    assert response.status_code == 401


def test_client_adds_signature_header() -> None:
    client = HttpAgentClient(
        "http://127.0.0.1:1", TOKEN, hmac_key=KEY
    )
    context = AgentContext(workspace="/tmp/ws")
    headers = client._headers(context)
    assert headers["X-Agent-Signature"] == sign_context(
        context.to_header(), KEY
    )


def test_client_without_key_has_no_signature() -> None:
    client = HttpAgentClient("http://127.0.0.1:1", TOKEN)
    headers = client._headers(AgentContext(workspace="/tmp/ws"))
    assert "X-Agent-Signature" not in headers


def test_agent_rejects_missing_signature(tmp_path) -> None:
    response = _app(tmp_path).get(
        "/v1/snapshots",
        params={"thread_id": "t1"},
        headers=_headers(signature=None),
    )
    assert response.status_code == 401


def test_agent_rejects_bad_signature(tmp_path) -> None:
    response = _app(tmp_path).get(
        "/v1/snapshots",
        params={"thread_id": "t1"},
        headers=_headers(signature="bad"),
    )
    assert response.status_code == 401


def test_agent_accepts_valid_signature(tmp_path) -> None:
    header = AgentContext(workspace="/tmp/ws").to_header()
    response = _app(tmp_path).get(
        "/v1/snapshots",
        params={"thread_id": "t1"},
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "X-Agent-Context": header,
            "X-Agent-Signature": sign_context(header, KEY),
        },
    )
    assert response.status_code == 200


def test_key_disabled_keeps_backward_compatibility(tmp_path) -> None:
    response = _app(tmp_path, key="").get(
        "/v1/snapshots",
        params={"thread_id": "t1"},
        headers=_headers(signature=None),
    )
    assert response.status_code == 200
