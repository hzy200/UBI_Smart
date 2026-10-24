"""桌面壳：本地 HTTP 服务只允许带随机 token 的 loopback 访问。"""

from __future__ import annotations

import httpx
import pytest

from coding_agent.config import Settings
from coding_agent.desktop.bootstrap import start_server


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        audit_dir=str(tmp_path / "audit"),
        checkpoint_path=":memory:",
        deepseek_api_key="sk-test",
        wsl_workspace="/tmp/ws",
    )


def _client(server) -> httpx.Client:
    return httpx.Client(
        base_url=f"http://127.0.0.1:{server.port}",
        headers={"Authorization": f"Bearer {server.token}"},
        timeout=5.0,
    )


def test_desktop_server_requires_token(settings) -> None:
    server = start_server(settings, acquire_lock=False)
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{server.port}", timeout=5.0
        ) as anonymous:
            assert anonymous.get("/healthz").status_code == 200
            assert anonymous.get("/api/capabilities").status_code == 401

        with _client(server) as client:
            response = client.get("/api/capabilities")
            assert response.status_code == 200
            data = response.json()
            assert data["read_only"] is True
            assert data["multi_user"] is False
            assert data["server_write_enabled"] is False
    finally:
        server.stop()


def test_desktop_server_is_single_instance(settings) -> None:
    first = start_server(settings)
    try:
        with pytest.raises(RuntimeError):
            start_server(settings)
    finally:
        first.stop()


def test_desktop_cancel_all_endpoint_is_desktop_only(settings) -> None:
    server = start_server(settings, acquire_lock=False)
    try:
        with _client(server) as client:
            response = client.post("/api/desktop/cancel_all")
            assert response.status_code == 200
            assert response.json()["cancelled"] == 0
    finally:
        server.stop()


def test_desktop_default_is_read_only_but_write_modes_need_server_flag(
    settings,
) -> None:
    server = start_server(settings, acquire_lock=False)
    try:
        with _client(server) as client:
            denied = client.post(
                "/api/sessions",
                json={
                    "workspace": "/tmp/ws",
                    "access_mode": "write_auto",
                },
            )
            assert denied.status_code == 403
            ok = client.post(
                "/api/sessions",
                json={
                    "workspace": "/tmp/ws",
                    "access_mode": "read_only",
                },
            )
            assert ok.status_code == 200
            assert ok.json()["access_mode"] == "read_only"
    finally:
        server.stop()
