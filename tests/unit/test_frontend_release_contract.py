"""前后端独立发版的契约锚点。

版本号在 pyproject / package.json / version.ts / web/version.py 之间漂移时，
独立发版会变成"看起来能跑、实际契约不匹配"，这里把它变成可执行断言。
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

from fastapi.testclient import TestClient

from coding_agent import __version__, web
from coding_agent.config import Settings
from coding_agent.web.version import (
    CAPABILITIES_VERSION,
    MIN_FRONTEND_VERSION,
    WEB_API_VERSION,
    parse_semver,
)

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "src" / "coding_agent" / "web" / "frontend"


def _frontend_version() -> str:
    text = (FRONTEND / "src" / "version.ts").read_text(encoding="utf-8")
    match = re.search(
        r"FRONTEND_VERSION\s*=\s*['\"]([^'\"]+)['\"]", text
    )
    assert match, "version.ts 缺少 FRONTEND_VERSION"
    return match.group(1)


def _settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        audit_dir=str(tmp_path / "audit"),
        checkpoint_path=":memory:",
        deepseek_api_key="sk-test",
        wsl_workspace="/tmp/ws",
        web_data_root=str(tmp_path / "web-data"),
    )


def test_versions_are_in_sync() -> None:
    pyproject = tomllib.loads(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    package = json.loads(
        (FRONTEND / "package.json").read_text(encoding="utf-8")
    )
    frontend = _frontend_version()
    minimum = parse_semver(MIN_FRONTEND_VERSION)
    current = parse_semver(frontend)
    assert minimum is not None and current is not None
    assert current >= minimum
    assert {
        __version__,
        pyproject["project"]["version"],
        package["version"],
        frontend,
    } == {frontend}
    assert isinstance(CAPABILITIES_VERSION, int)
    assert CAPABILITIES_VERSION >= 1


def test_runtime_config_and_healthz_agree(tmp_path) -> None:
    client = TestClient(web.create_app(_settings(tmp_path)))
    config = client.get("/config.json").json()
    health = client.get("/healthz").json()

    assert config["apiVersion"] == health["api_version"] == WEB_API_VERSION
    assert health["min_frontend_version"] == MIN_FRONTEND_VERSION
    assert health["capabilities_version"] == CAPABILITIES_VERSION
    assert parse_semver(_frontend_version()) >= parse_semver(
        health["min_frontend_version"]
    )
    capabilities = client.get("/api/capabilities").json()
    assert capabilities["capabilities_version"] == CAPABILITIES_VERSION


def test_api_hosted_frontend_fallback_remains(tmp_path) -> None:
    """独立发版一个版本周期内，API 仍可托管旧首页作为回退入口。"""
    client = TestClient(web.create_app(_settings(tmp_path)))
    for path in ("/", "/config.json", "/static/app.js", "/static/style.css"):
        assert client.get(path).status_code == 200
    html = client.get("/").text
    assert 'id="boot"' in html
    assert "config.json" not in html  # 运行时配置由 JS 拉取，不写死在页面里
