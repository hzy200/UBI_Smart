"""Phase D3：控制面数据库不可用时的只读降级。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from ubismart import web
from ubismart.config import Settings


def _settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        audit_dir=str(tmp_path / "audit"),
        checkpoint_path=":memory:",
        deepseek_api_key="sk-test",
        wsl_workspace="/tmp/ws",
        control_db_url=f"sqlite:///{tmp_path / 'control.db'}",
    )


def test_app_starts_degraded_when_store_unavailable(
    tmp_path, monkeypatch
) -> None:
    from ubismart.control import store as store_module

    def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(store_module, "ControlStore", boom)

    app = web.create_app(_settings(tmp_path))
    client = TestClient(app)

    health = client.get("/healthz").json()
    assert health["status"] == "ok"
    assert health["db_state"] == "degraded"

    assert (
        client.post(
            "/api/sessions", json={"thread_id": "t1"}
        ).status_code
        == 503
    )
    # 读路径仍可用：回退到本地会话文件（可能为空）。
    assert client.get("/api/sessions").status_code == 200


def test_write_failure_switches_to_degraded(tmp_path) -> None:
    settings = _settings(tmp_path)
    app = web.create_app(settings)
    client = TestClient(app)

    created = client.post("/api/sessions", json={"thread_id": "t1"})
    assert created.status_code == 200

    def boom(*args, **kwargs):
        raise RuntimeError("write failed")

    app.state.control_store.save_sessions = boom  # type: ignore[union-attr]
    renamed = client.post(
        "/api/sessions/t1/rename", json={"title": "新标题"}
    )
    assert renamed.status_code == 200  # 内存写成功，单次失败不丢操作
    assert client.get("/healthz").json()["db_state"] == "degraded"

    blocked = client.post(
        "/api/sessions/t1/rename", json={"title": "再次"}
    )
    assert blocked.status_code == 503
