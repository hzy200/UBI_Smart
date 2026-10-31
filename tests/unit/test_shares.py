"""只读分享：脱敏、过期/撤销与匿名页面。"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import Session

from ubismart.config import Settings
from ubismart.control.schema import ControlShareLink
from ubismart.control.shares import ShareStore
from ubismart.control.store import ControlStore
from ubismart.web import app as web_app
from ubismart.web.shares import build_share_payload, render_share_html


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


def test_share_payload_redacts_paths_tools_and_system() -> None:
    payload = build_share_payload(
        [
            {"role": "user", "text": "看看 /tmp/ws/app.py"},
            {
                "role": "工具",
                "kind": "tool",
                "name": "read_file",
                "summary": "读取 /tmp/ws/app.py",
                "preview": "RAW TOOL OUTPUT",
                "state": "完成",
                "ok": True,
            },
            {"role": "assistant", "text": "已修改 /tmp/ws/app.py"},
            {"role": "system", "text": "INTERNAL SYSTEM PROMPT"},
        ],
        title="演示",
        workspace="/tmp/ws",
    )
    assert [item["role"] for item in payload["messages"]] == [
        "user",
        "工具",
        "assistant",
    ]
    text = str(payload)
    assert "INTERNAL SYSTEM PROMPT" not in text
    assert "RAW TOOL OUTPUT" not in text
    assert "/tmp/ws" not in text
    assert "<workspace>" in text


def test_render_share_html_escapes_model_text() -> None:
    html = render_share_html(
        {
            "title": "<script>alert(1)</script>",
            "created_at": "2026-10-29T10:00:00+00:00",
            "expires_at": "2026-11-05T10:00:00+00:00",
            "messages": [
                {"role": "assistant", "text": "<img src=x onerror=1>"}
            ],
        }
    )
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert "<img src=x" not in html


def test_share_store_expiry_and_revoke(tmp_path) -> None:
    store = ControlStore(_settings(tmp_path).control_db_url)
    try:
        shares = ShareStore(store.engine)
        record, token = shares.create(
            "local",
            thread_id="t1",
            created_by="local",
            title="演示",
            payload={"messages": []},
            expires_in_days=7,
        )
        assert token.startswith("shr_")
        assert shares.list("local", "t1")[0]["active"] is True
        resolved = shares.resolve(token)
        assert resolved and resolved["views"] == 1
        assert shares.revoke("local", record["id"]) is True
        assert shares.resolve(token) is None
        assert shares.list("local", "t1")[0]["active"] is False

        _, expired = shares.create(
            "local",
            thread_id="t1",
            created_by="local",
            title="过期",
            payload={"messages": []},
        )
        with Session(store.engine) as session, session.begin():
            session.execute(
                update(ControlShareLink)
                .where(ControlShareLink.token_hash != "")
                .values(expires_at="2000-01-01T00:00:00+00:00")
            )
        assert shares.resolve(expired) is None
    finally:
        store.close()


def test_share_api_creates_redacted_public_page(tmp_path) -> None:
    settings = _settings(tmp_path)
    client = TestClient(web_app.create_app(settings))
    saved = client.post(
        "/api/thread/t1/transcript",
        json={
            "messages": [
                {"role": "user", "text": "看看 /tmp/ws/app.py"},
                {
                    "role": "工具",
                    "kind": "tool",
                    "name": "read_file",
                    "summary": "读取 /tmp/ws/app.py",
                    "preview": "RAW TOOL OUTPUT",
                    "ok": True,
                },
                {
                    "role": "assistant",
                    "text": "已修复 /tmp/ws/app.py",
                },
                {"role": "system", "text": "INTERNAL SYSTEM PROMPT"},
            ]
        },
    )
    assert saved.status_code == 200, saved.text

    created = client.post(
        "/api/sessions/t1/shares", json={"expires_in_days": 7}
    )
    assert created.status_code == 200, created.text
    path = created.json()["path"]
    share_id = created.json()["share"]["id"]
    assert path.startswith("/share/shr_")

    listed = client.get("/api/sessions/t1/shares").json()
    assert listed["enabled"] is True
    assert "shr_" not in str(listed)

    page = client.get(path)
    assert page.status_code == 200
    assert page.headers["x-robots-tag"].startswith("noindex")
    body = page.text
    assert "INTERNAL SYSTEM PROMPT" not in body
    assert "RAW TOOL OUTPUT" not in body
    assert "/tmp/ws" not in body
    assert "已修复" in body

    public = client.get(path.replace("/share/", "/api/share/"))
    assert public.status_code == 200
    assert public.json()["payload"]["messages"]

    revoked = client.delete(f"/api/shares/{share_id}")
    assert revoked.status_code == 200
    assert client.get(path).status_code == 404
