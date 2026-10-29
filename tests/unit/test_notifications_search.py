"""站内通知与跨会话搜索索引。"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from coding_agent.config import Settings
from coding_agent.control.store import ControlStore
from coding_agent.web import app as web_app
from coding_agent.web.routes.notifications import notification_payload


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


def test_notification_payload_maps_domain_events() -> None:
    assert notification_payload(
        {"type": "run_finished", "answer": "完成"}
    ) == ("success", "运行完成", "完成", "")
    assert notification_payload(
        {"type": "run_failed", "message": "模型不可用"}
    ) == ("error", "运行失败", "模型不可用", "")
    approval = notification_payload(
        {
            "type": "approval_requested",
            "tool": "shell_exec",
            "command": "ls",
            "request_id": "r1",
        }
    )
    assert approval == ("warning", "需要审批", "shell_exec: ls", "approval:r1")
    assert notification_payload({"type": "assistant_token"}) is None


def test_notification_store_dedupes_and_tracks_read(tmp_path) -> None:
    store = ControlStore(_settings(tmp_path).control_db_url)
    try:
        first = store.add_notification(
            "local",
            kind="success",
            title="运行完成",
            body="ok",
            dedupe_key="run-1",
        )
        again = store.add_notification(
            "local",
            kind="success",
            title="运行完成",
            body="ok",
            dedupe_key="run-1",
        )
        assert first is not None and again is not None
        assert first["id"] == again["id"]

        store.add_notification("local", kind="warning", title="需要审批")
        data = store.list_notifications("local")
        assert data["unread"] == 2
        assert len(data["notifications"]) == 2

        assert store.mark_notifications_read("local", all_=True) == 2
        assert store.list_notifications("local")["unread"] == 0
        assert store.clear_notifications("local") == 2
        assert store.list_notifications("local")["notifications"] == []
    finally:
        store.close()


def test_search_index_matches_chinese_substring(tmp_path) -> None:
    from sqlalchemy import inspect

    store = ControlStore(_settings(tmp_path).control_db_url)
    try:
        assert "control_session_fts" in inspect(store.engine).get_table_names()
        store.index_session_text(
            "local",
            "t1",
            title="修复登录",
            body="用户无法登录，检查 password 字段",
        )
        store.index_session_text(
            "local", "t2", title="其他", body="无关内容"
        )

        result = store.search_sessions("local", "无法登录")
        assert [row["thread_id"] for row in result["results"]] == ["t1"]
        assert "无法登录" in result["results"][0]["snippet"]

        # 少于 3 个字符时 FTS trigram 不适用，必须回退 LIKE。
        short = store.search_sessions("local", "登录")
        assert [row["thread_id"] for row in short["results"]] == ["t1"]

        # 重建不会清掉已有正文索引。
        store.rebuild_session_search(["local"])
        assert (
            store.search_sessions("local", "无关内容")["results"][0][
                "thread_id"
            ]
            == "t2"
        )
    finally:
        store.close()


def test_search_covers_last_message_and_audit_index(tmp_path) -> None:
    from sqlalchemy.orm import Session

    from coding_agent.control.schema import ControlSession

    store = ControlStore(_settings(tmp_path).control_db_url)
    try:
        with Session(store.engine) as session, session.begin():
            session.add(
                ControlSession(
                    tenant="local",
                    thread_id="s1",
                    title="另一个会话",
                    last_message="最后消息里有部署脚本",
                    created_at="2026-10-29T09:00:00+00:00",
                    updated_at="2026-10-29T09:00:00+00:00",
                )
            )
        store.index_audit(
            "local",
            [
                {
                    "hash": "h1",
                    "thread_id": "s2",
                    "ts": "2026-10-29T10:00:00+00:00",
                    "kind": "tool_call",
                    "tool": "shell_exec",
                    "level": "L0",
                    "decision": "auto",
                    "ok": True,
                }
            ],
        )

        by_message = store.search_sessions("local", "部署脚本")
        assert by_message["results"][0]["thread_id"] == "s1"
        assert by_message["results"][0]["match"] == "title"

        by_audit = store.search_sessions("local", "shell_exec")
        assert by_audit["results"][0]["thread_id"] == "s2"
        assert by_audit["results"][0]["match"] == "audit"
    finally:
        store.close()


def test_search_index_keeps_body_when_only_title_changes(tmp_path) -> None:
    store = ControlStore(_settings(tmp_path).control_db_url)
    try:
        store.index_session_text(
            "local", "t1", title="旧标题", body="正文里有关键词"
        )
        store.index_session_text("local", "t1", title="新标题")
        result = store.search_sessions("local", "关键词")
        assert result["results"][0]["thread_id"] == "t1"
        assert result["results"][0]["title"] == "新标题"
    finally:
        store.close()


def test_notification_and_search_api(tmp_path) -> None:
    settings = _settings(tmp_path)
    store = ControlStore(settings.control_db_url)
    try:
        store.add_notification(
            "local", kind="success", title="运行完成", thread_id="t1"
        )
        store.index_session_text(
            "local", "t1", title="登录问题", body="用户无法登录"
        )
    finally:
        store.close()

    client = TestClient(web_app.create_app(settings))
    notes = client.get("/api/notifications").json()
    assert notes["enabled"] is True
    assert notes["notifications"][0]["title"] == "运行完成"
    assert notes["unread"] == 1

    marked = client.post(
        "/api/notifications/read", json={"all": True}
    )
    assert marked.status_code == 200
    assert client.get("/api/notifications").json()["unread"] == 0

    found = client.get("/api/sessions/search?q=无法登录").json()
    assert found["enabled"] is True
    assert found["results"][0]["thread_id"] == "t1"

    cleared = client.delete("/api/notifications")
    assert cleared.status_code == 200
    assert client.get("/api/notifications").json()["notifications"] == []
