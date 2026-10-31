"""A2 前端托管开关、发布目录与缓存策略。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ubismart import web
from ubismart.config import Settings
from ubismart.web.auth import AuthConfig
from ubismart.web.middleware import cache_control_for_path


def _settings(tmp_path: Path, **updates: object) -> Settings:
    return Settings(
        _env_file=None,
        audit_dir=str(tmp_path / "audit"),
        checkpoint_path=":memory:",
        deepseek_api_key="sk-test",
        wsl_workspace="/tmp/ws",
        web_data_root=str(tmp_path / "web-data"),
        **updates,
    )


def _client(settings: Settings) -> TestClient:
    auth = AuthConfig(tokens={"token": "user"}, allow_anonymous_local=False)
    return TestClient(web.create_app(settings, auth=auth))


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer token"}


def test_default_hosting_keeps_package_static(settings) -> None:
    client = TestClient(web.create_app(settings))
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/manifest.webmanifest").status_code == 200


def test_serve_frontend_false_keeps_api_only(tmp_path: Path) -> None:
    client = _client(_settings(tmp_path, web_serve_frontend=False))

    assert client.get("/").status_code == 404
    assert client.get("/static/app.js").status_code == 404
    assert client.get("/healthz").status_code == 200
    # 只验证 API 路由仍然注册；503 可能是测试环境的控制面未就绪。
    assert client.get("/api/sessions", headers=_headers()).status_code != 404


def test_desktop_mode_forces_hosting(tmp_path: Path) -> None:
    settings = _settings(tmp_path, web_serve_frontend=False)
    client = TestClient(web.create_app(settings, desktop_mode=True))
    assert client.get("/").status_code == 200


def test_release_directory_is_used_for_root_and_static(tmp_path: Path) -> None:
    release = tmp_path / "release"
    static = release / "static"
    static.mkdir(parents=True)
    (release / "index.html").write_text("<html>release</html>", encoding="utf-8")
    (release / "sw.js").write_text("// sw", encoding="utf-8")
    (release / "manifest.webmanifest").write_text(
        '{"name":"x","start_url":"/","scope":"/"}', encoding="utf-8"
    )
    (static / "app.1234abcd5678.js").write_text("// app", encoding="utf-8")

    settings = _settings(tmp_path, web_frontend_dir=str(release))
    client = TestClient(web.create_app(settings))

    assert "release" in client.get("/").text
    asset = client.get("/static/app.1234abcd5678.js")
    assert asset.status_code == 200
    assert "immutable" in asset.headers["cache-control"]


def test_explicit_missing_frontend_dir_fails_closed(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path, web_frontend_dir=str(tmp_path / "missing")
    )
    with pytest.raises(ValueError, match="AGENT_WEB_FRONTEND_DIR"):
        web.create_app(settings)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/api/sessions", "no-store"),
        ("/api/v1/sessions", "no-store"),
        ("/healthz", "no-store"),
        ("/", "no-cache"),
        ("/config.json", "no-cache"),
        ("/sw.js", "no-cache"),
        ("/static/app.js", "no-cache"),
        ("/static/app.1234abcd5678.js", "public, max-age=31536000, immutable"),
        ("/static/vendor/marked.min.c6c72555c722.js", "public, max-age=31536000, immutable"),
    ],
)
def test_cache_control_for_path(path: str, expected: str) -> None:
    assert cache_control_for_path(path) == expected
