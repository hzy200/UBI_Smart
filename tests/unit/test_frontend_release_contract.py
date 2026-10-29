"""前后端独立发版的契约锚点。

前后端版本可以各自递增，但发布清单、API 版本与最小兼容版本必须一致。
脚本生成的发布包也在这里跑一次真实校验，避免“脚本能跑、产物不合契约”。
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
    MIN_API_VERSION,
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


def test_versions_are_independently_releasable() -> None:
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
    # 前端内部版本一致；后端由 hatch 从 __version__ 动态读取，只有一处。
    assert package["version"] == frontend
    assert "version" not in pyproject["project"]
    assert pyproject["project"]["dynamic"] == ["version"]
    assert pyproject["tool"]["hatch"]["version"]["path"] == (
        "src/coding_agent/__init__.py"
    )
    assert __version__
    assert isinstance(CAPABILITIES_VERSION, int)
    assert CAPABILITIES_VERSION >= 1


def test_frontend_sources_do_not_fetch_api_directly() -> None:
    """所有请求必须经 apiUrl/authFetch，才能注入基址与版本段。"""
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in (FRONTEND / "src").rglob("*.ts")
        if re.search(r"fetch\(\s*['\"`]/api", path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_release_bundle_matches_version_contract(tmp_path) -> None:
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    from build_frontend_release import build_release
    from check_frontend_release import check

    release = build_release(
        tmp_path, version=_frontend_version(), skip_build=True
    )
    assert check(release) == []

    manifest = json.loads(
        (release / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["version"] == _frontend_version()
    assert manifest["apiVersion"] == WEB_API_VERSION
    assert manifest["minApiVersion"] == MIN_API_VERSION
    assert manifest["minFrontendVersion"] == MIN_FRONTEND_VERSION
    assert manifest["capabilitiesVersion"] == CAPABILITIES_VERSION


def test_runtime_config_and_healthz_agree(tmp_path) -> None:
    client = TestClient(web.create_app(_settings(tmp_path)))
    config = client.get("/config.json").json()
    health = client.get("/healthz").json()

    assert config["apiVersion"] == health["api_version"] == WEB_API_VERSION
    assert health["min_api_version"] == MIN_API_VERSION
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
