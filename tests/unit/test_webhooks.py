"""出站 Webhook：URL 校验、secret 保密、签名与投递状态。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from coding_agent.config import Settings
from coding_agent.control import webhooks as webhook_module
from coding_agent.control.store import ControlStore
from coding_agent.control.webhooks import (
    WebhookError,
    WebhookService,
    WebhookStore,
    normalize_events,
    sign_payload,
    validate_webhook_url,
)
from coding_agent.web import app as web_app

GLOBAL_IP_URL = "https://93.184.216.34/hook"


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        deepseek_api_key="sk-test",
        wsl_workspace="/tmp/ws",
        audit_dir=str(tmp_path / "audit"),
        web_data_root=str(tmp_path / "web-data"),
        control_db_url=f"sqlite:///{tmp_path / 'control.db'}",
        control_db_auto_create=True,
    )


def test_validate_webhook_url_rejects_unsafe_targets() -> None:
    assert validate_webhook_url(GLOBAL_IP_URL) == GLOBAL_IP_URL
    for bad in (
        "http://93.184.216.34/hook",
        "https://127.0.0.1/hook",
        "https://10.0.0.1/hook",
        "https://user:pass@93.184.216.34/hook",
        "https://93.184.216.34:8443/hook",
    ):
        with pytest.raises(WebhookError):
            validate_webhook_url(bad)


def test_webhook_events_are_validated() -> None:
    assert normalize_events(["run_finished", "approval_requested"]) == (
        "run_finished",
        "approval_requested",
    )
    with pytest.raises(WebhookError):
        normalize_events(["unknown.event"])


def test_webhook_store_hides_secret_and_signs(tmp_path) -> None:
    store = ControlStore(_settings(tmp_path).control_db_url)
    try:
        webhooks = WebhookStore(store.engine)
        record, secret = webhooks.create(
            "local",
            url=GLOBAL_IP_URL,
            events=["run_finished"],
            name="ci",
        )
        assert secret.startswith("whsec_")
        assert "secret" not in webhooks.list("local")[0]
        delivery = webhooks.list_for_delivery("local", "run_finished")
        assert delivery[0]["secret"] == secret
        assert sign_payload(secret, b"{}").startswith("sha256=")
        assert webhooks.delete("local", record["id"]) is True
        assert webhooks.list("local") == []
    finally:
        store.close()


def test_webhook_service_records_delivery(tmp_path, monkeypatch) -> None:
    store = ControlStore(_settings(tmp_path).control_db_url)
    try:
        webhooks = WebhookStore(store.engine)
        webhooks.create(
            "local", url=GLOBAL_IP_URL, events=["run_finished"]
        )
        captured: list = []

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(request, timeout=5):
            captured.append(request)
            return Response()

        monkeypatch.setattr(webhook_module, "urlopen", fake_urlopen)
        service = WebhookService(webhooks)
        assert service.send("local", "run_finished", {"title": "完成"}) == 1
        assert captured
        request = captured[0]
        assert request.get_header("X-codingagent-event") == "run_finished"
        signature = request.get_header("X-codingagent-signature")
        assert signature and signature.startswith("sha256=")
        rows = webhooks.list("local")
        assert rows[0]["last_status"] == 200
        assert rows[0]["last_sent_at"]
    finally:
        store.close()


def test_webhook_api_create_and_delete(tmp_path) -> None:
    client = TestClient(web_app.create_app(_settings(tmp_path)))
    created = client.post(
        "/api/webhooks",
        json={"name": "ci", "url": GLOBAL_IP_URL, "events": ["run_finished"]},
    )
    assert created.status_code == 200, created.text
    secret = created.json()["secret"]
    assert secret.startswith("whsec_")
    listed = client.get("/api/webhooks").json()
    assert listed["enabled"] is True
    assert secret not in str(listed["webhooks"])
    removed = client.delete(
        f"/api/webhooks/{created.json()['webhook']['id']}"
    )
    assert removed.status_code == 200
    assert client.get("/api/webhooks").json()["webhooks"] == []

    bad = client.post(
        "/api/webhooks",
        json={
            "url": "https://127.0.0.1/hook",
            "events": ["run_finished"],
        },
    )
    assert bad.status_code == 422
