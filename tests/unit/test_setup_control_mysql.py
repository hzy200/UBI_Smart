"""控制面 MySQL 初始化脚本。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from setup_control_mysql import (  # noqa: E402
    _password,
    build_statements,
    main,
    target_url,
)


def test_help() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "setup_control_mysql.py"), "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    assert result.returncode == 0
    assert "--admin-url" in result.stdout
    assert "--show-url" in result.stdout


def test_build_statements() -> None:
    statements = build_statements(
        "coding_agent", "coding_agent", "a" * 24
    )
    assert statements[0].startswith("CREATE DATABASE")
    assert "utf8mb4" in statements[0]
    assert any("CREATE USER" in item for item in statements)
    assert any("GRANT SELECT" in item for item in statements)


def test_invalid_identifier_is_rejected() -> None:
    with pytest.raises(ValueError):
        build_statements("bad-name", "user", "a" * 24)


def test_password_validation() -> None:
    with pytest.raises(ValueError):
        _password("short")
    with pytest.raises(ValueError):
        _password("has'quote" + "a" * 20)
    assert _password("a" * 24) == "a" * 24


def test_target_url_redaction() -> None:
    url = target_url(
        "127.0.0.1", 3306, "coding_agent", "agent", "a" * 24
    )
    redacted = str(url.set(password="***"))
    assert "***" in redacted
    assert "a" * 24 not in redacted


def test_dry_run(capsys) -> None:
    assert main(["--dry-run"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["dry_run"] is True
    assert payload["url"].endswith("coding_agent")
    assert "<redacted>" in json.dumps(payload)
