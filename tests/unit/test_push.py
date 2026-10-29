"""Web Push 订阅存储、配置降级与失效订阅清理。"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from coding_agent.config import Settings
from coding_agent.control.store import ControlStore
from coding_agent.web import app as web_app
from coding_agent.web.push import PushService


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


def test_push_subscription_store_roundtrip(tmp_path) -> None:
    store = ControlStore(_settings(tmp_path).control_db_url)
    try:
        first = store.add_push_subscription(
            "local",
            endpoint="https://push.example/1",
            p256dh="key-1",
            auth="auth-1",
            user_agent="ua",
        )
        assert first is not None
        again = store.add_push_subscription(
            "local",
            endpoint="https://push.example/1",
            p256dh="key-2",
            auth="auth-2",
        )
        assert again is not None and again["id"] == first["id"]
        rows = store.list_push_subscriptions("local")
        assert len(rows) == 1
        assert rows[0]["p256dh"] == "key-2"
        assert store.list_push_subscriptions("other") == []
        assert (
            store.remove_push_subscription(
                "local", endpoint="https://push.example/1"
            )
            is True
        )
        assert store.list_push_subscriptions("local") == []
    finally:
        store.close()


def test_push_config_is_disabled_without_keys(tmp_path) -> None:
    client = TestClient(web_app.create_app(_settings(tmp_path)))
    config = client.get("/api/push/config").json()
    assert config == {"enabled": False, "public_key": ""}

    posted = client.post(
        "/api/push/subscriptions",
        json={
            "endpoint": "https://push.example/1",
            "keys": {"p256dh": "k", "auth": "a"},
        },
    )
    assert posted.status_code == 200
    assert posted.json()["enabled"] is False


def test_push_service_sends_and_drops_gone_subscriptions(tmp_path) -> None:
    settings = _settings(tmp_path).model_copy(
        update={
            "webpush_public_key": "public",
            "webpush_private_key": "private",
            "webpush_subject": "mailto:test@example.com",
        }
    )
    store = ControlStore(settings.control_db_url)
    try:
        store.add_push_subscription(
            "local",
            endpoint="https://push.example/1",
            p256dh="key-1",
            auth="auth-1",
        )
        service = PushService(settings, store)
        calls: list[dict] = []

        def fake_webpush(**kwargs):
            calls.append(kwargs)

        class FakeError(Exception):
            def __init__(self):
                self.response = type(
                    "Response", (), {"status_code": 410}
                )()

        service._checked = True
        service._module = (fake_webpush, FakeError)
        assert service.enabled is True
        assert service.send("local", {"title": "运行完成"}) == 1
        assert calls and calls[0]["subscription_info"]["endpoint"].endswith(
            "/1"
        )

        def gone_webpush(**kwargs):
            raise FakeError()

        service._module = (gone_webpush, FakeError)
        assert service.send("local", {"title": "again"}) == 0
        # 410 表示订阅已失效，必须清理。
        assert store.list_push_subscriptions("local") == []
    finally:
        store.close()
