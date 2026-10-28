"""Phase B1/B2：A2 控制面的 schema、会话目录与迁移。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

from coding_agent import web
from coding_agent.config import Settings
from coding_agent.control.schema import (
    Base,
    ControlAuditIndex,
    ControlSession,
)
from coding_agent.control.store import ControlStore


def _url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'control.db'}"


def test_schema_is_mysql_compatible_and_versioned(tmp_path) -> None:
    store = ControlStore(_url(tmp_path))
    tables = set(Base.metadata.tables)
    assert {
        "control_sessions",
        "control_runs",
        "control_audit_index",
        "control_provider_profiles",
        "control_usage_daily",
        "control_meta",
        "control_users",
        "control_auth_sessions",
        "control_auth_events",
        "control_provider_keys",
        "control_user_quotas",
        "control_user_usage_daily",
        "control_quota_reservations",
        "control_usage_events",
        "control_usage_cursor",
    } <= tables
    ddl = str(
        CreateTable(ControlSession.__table__).compile(dialect=mysql.dialect())
    )
    assert "control_sessions" in ddl
    assert "tenant" in ddl and "thread_id" in ddl
    store.close()


def test_session_rows_roundtrip_and_tombstone(tmp_path) -> None:
    store = ControlStore(_url(tmp_path))
    store.save_sessions(
        "alice",
        [
            {
                "thread_id": "t1",
                "workspace": "/home/alice/ws",
                "access_mode": "read_only",
                "model": "m",
                "provider": "p",
                "uploaded_bytes": 12,
                "title": "第一",
            }
        ],
        set(),
    )
    rows, deleted = store.load_sessions("alice")
    assert rows[0]["thread_id"] == "t1"
    assert rows[0]["title"] == "第一"
    assert deleted == set()
    assert store.load_sessions("bob") == ([], set())

    store.save_sessions("alice", [], {"t1"})
    rows, deleted = store.load_sessions("alice")
    assert rows == []
    assert deleted == {"t1"}
    store.close()


def _settings(tmp_path: Path, *, db: bool = True) -> Settings:
    return Settings(
        _env_file=None,
        audit_dir=str(tmp_path / "audit"),
        checkpoint_path=":memory:",
        deepseek_api_key="sk-test",
        wsl_workspace="/tmp/ws",
        control_db_url=_url(tmp_path) if db else "",
    )


def test_web_sessions_survive_restart_with_control_db(tmp_path) -> None:
    settings = _settings(tmp_path)
    client = TestClient(web.create_app(settings))
    created = client.post(
        "/api/sessions",
        json={"thread_id": "t1", "workspace": "/tmp/ws"},
    )
    assert created.status_code == 200
    assert (
        client.post(
            "/api/sessions/t1/rename", json={"title": "数据库标题"}
        ).status_code
        == 200
    )
    client.post("/api/sessions", json={"thread_id": "t2"})

    # 新 app 实例模拟 A2 重启；配置必须来自数据库而不是进程内存。
    restarted = TestClient(web.create_app(settings))
    rows = restarted.get("/api/sessions").json()["sessions"]
    titles = {row["thread_id"]: row["title"] for row in rows}
    assert titles["t1"] == "数据库标题"
    assert "t2" in titles

    assert restarted.delete("/api/sessions/t1").status_code == 200
    second_restart = TestClient(web.create_app(settings))
    rows = second_restart.get("/api/sessions").json()["sessions"]
    assert "t1" not in {row["thread_id"] for row in rows}
    assert "t2" in {row["thread_id"] for row in rows}

    store = ControlStore(_url(tmp_path), auto_create=False)
    assert store.count_sessions("local") == 2  # t1 tombstone + t2
    store.close()


def test_legacy_json_is_imported_once(tmp_path) -> None:
    settings = _settings(tmp_path)
    audit = tmp_path / "audit"
    audit.mkdir(parents=True)
    (audit / "gui-sessions.json").write_text(
        json.dumps(
            [
                {
                    "thread_id": "legacy",
                    "workspace": "/tmp/legacy",
                    "access_mode": "read_only",
                    "title": "旧会话",
                }
            ]
        ),
        encoding="utf-8",
    )
    client = TestClient(web.create_app(settings))
    rows = client.get("/api/sessions").json()["sessions"]
    assert any(row["thread_id"] == "legacy" for row in rows)

    store = ControlStore(_url(tmp_path), auto_create=False)
    active, deleted = store.load_sessions("local")
    assert [row["thread_id"] for row in active] == ["legacy"]
    assert deleted == set()
    store.close()


def test_without_db_behavior_stays_file_based(tmp_path) -> None:
    settings = _settings(tmp_path, db=False)
    app = web.create_app(settings)
    assert app.state.control_store is None
    client = TestClient(app)
    client.post("/api/sessions", json={"thread_id": "file1"})
    path = tmp_path / "audit" / "gui-sessions.json"
    assert path.is_file()
    assert "file1" in path.read_text(encoding="utf-8")


def test_audit_index_is_idempotent(tmp_path) -> None:
    store = ControlStore(_url(tmp_path))
    rows = [
        {
            "hash": "h1",
            "ts": "2026-01-01T00:00:00+00:00",
            "thread_id": "t1",
            "kind": "tool_call",
            "tool": "shell_exec",
            "level": "L0",
            "decision": "auto",
            "ok": True,
        }
    ]
    assert store.index_audit("local", rows) == 1
    assert store.index_audit("local", rows) == 0
    from sqlalchemy import func, select
    from sqlalchemy.orm import Session

    with Session(store.engine) as session:
        count = session.scalar(
            select(func.count()).select_from(ControlAuditIndex)
        )
    assert count == 1
    store.close()


def test_audit_index_only_checks_the_current_batch(tmp_path) -> None:
    store = ControlStore(_url(tmp_path))
    from sqlalchemy import func, select
    from sqlalchemy.orm import Session

    def row(value: str) -> dict:
        return {
            "hash": value,
            "ts": "2026-01-01T00:00:00+00:00",
            "thread_id": "t1",
            "kind": "tool_call",
            "tool": "shell_exec",
            "level": "L0",
            "decision": "auto",
            "ok": True,
        }

    # 已有 1000 条历史记录；下一批包含 999 条旧记录 + 1 条新记录。
    assert store.index_audit("local", [row(f"h{i}") for i in range(1000)]) == 1000
    assert store.index_audit(
        "local", [row(f"h{i}") for i in range(999)] + [row("new")]
    ) == 1
    # 同一批里的重复 hash 也只能插入一次。
    assert store.index_audit("local", [row("dup"), row("dup")]) == 1

    with Session(store.engine) as session:
        count = session.scalar(
            select(func.count()).select_from(ControlAuditIndex)
        )
    assert count == 1002
    store.close()


def test_control_store_rejects_empty_url() -> None:
    with pytest.raises(ValueError):
        ControlStore("")


def test_mysql_engine_uses_bounded_pool() -> None:
    pytest.importorskip("pymysql")
    store = ControlStore(
        "mysql+pymysql://u:p@127.0.0.1:3306/coding_agent",
        auto_create=False,
    )
    try:
        assert store.engine.pool.size() == 5
    finally:
        store.close()
