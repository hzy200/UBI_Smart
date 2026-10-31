"""桌面授权桥：A2L 内部请求 → 前端卡片 → 决定回传。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ubismart.web.auth import AuthUser
from ubismart.web.consent_bridge import register_consent_bridge
from ubismart.web.dependencies import current_user

TOKEN = "internal-consent-token"


def _client() -> TestClient:
    app = FastAPI()
    register_consent_bridge(app, token=TOKEN)
    app.dependency_overrides[current_user] = lambda: AuthUser(name="local")
    return TestClient(app)


def _headers() -> dict[str, str]:
    return {"X-Consent-Token": TOKEN}


def test_pending_request_roundtrip() -> None:
    client = _client()
    created = client.post(
        "/internal/consent",
        json={
            "scope": [r"D:\proj"],
            "verbs": ["read"],
            "ttl_s": 1800,
            "reason": "读取项目文件",
            "sensitive": False,
        },
        headers=_headers(),
    )
    assert created.status_code == 200
    consent_id = created.json()["id"]

    pending = client.get("/api/consent/pending").json()["pending"]
    assert [item["id"] for item in pending] == [consent_id]
    assert pending[0]["scope"] == [r"D:\proj"]

    state = client.get(
        f"/internal/consent/{consent_id}", headers=_headers()
    ).json()
    assert state["state"] == "pending"

    decided = client.post(
        f"/api/consent/{consent_id}", json={"allow": True}
    )
    assert decided.status_code == 200
    assert client.get("/api/consent/pending").json()["pending"] == []
    state = client.get(
        f"/internal/consent/{consent_id}", headers=_headers()
    ).json()
    assert state["state"] == "allowed"


def test_internal_routes_require_token() -> None:
    client = _client()
    response = client.post(
        "/internal/consent",
        json={"scope": [], "verbs": []},
        headers={"X-Consent-Token": "wrong"},
    )
    assert response.status_code == 403


def test_decide_unknown_request_is_404() -> None:
    client = _client()
    assert (
        client.post("/api/consent/missing", json={"allow": True}).status_code
        == 404
    )
