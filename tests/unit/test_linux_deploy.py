"""Phase D4：Linux systemd 单元、安装脚本与迁移入口。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from coding_agent.config import Settings
from coding_agent.servers import control_server

ROOT = Path(__file__).resolve().parents[2]
SYSTEMD = ROOT / "packaging" / "systemd"


def _unit(name: str) -> str:
    return (SYSTEMD / name).read_text(encoding="utf-8")


def test_agent_unit_is_hardened_and_owns_state() -> None:
    text = _unit("coding-agent-agent.service")
    assert "User=coding-agent" in text
    assert "Group=coding-agent" in text
    assert "StateDirectory=coding-agent" in text
    assert "StateDirectoryMode=0700" in text
    assert "ReadWritePaths=/srv/coding-agent/workspaces" in text
    assert "NoNewPrivileges=true" in text
    assert "ProtectSystem=strict" in text
    assert "UMask=0077" in text
    assert "ExecStart=/opt/coding-agent/bin/CodingAgentAgent" in text


def test_control_unit_orders_agent_and_runs_migration() -> None:
    text = _unit("coding-agent-control.service")
    assert "coding-agent-agent.service" in text.split("[Service]")[0]
    assert "Wants=network-online.target coding-agent-agent.service" in text
    assert (
        "ExecStartPre=/opt/coding-agent/bin/CodingAgentControl --migrate-only"
        in text
    )
    assert "ExecStart=/opt/coding-agent/bin/CodingAgentControl" in text
    assert "ProtectHome=true" in text
    assert "RestrictNamespaces=true" in text
    assert "PrivateDevices=true" in text


def test_install_script_is_valid_shell() -> None:
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("没有 bash")
    result = subprocess.run(
        [bash, "-n", str(SYSTEMD / "install.sh")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_install_script_creates_user_dirs_and_secrets() -> None:
    text = (SYSTEMD / "install.sh").read_text(encoding="utf-8")
    assert "useradd --system" in text
    assert 'install -d -m 0700 -o "$USER_NAME"' in text
    assert "AGENT_INTERNAL_TOKEN" in text
    assert "AGENT_WEB_AUTH_TOKENS" in text
    assert "install-secrets.txt" in text
    assert "--uninstall" in text
    assert "--no-start" in text


def test_migrate_only_skips_without_database(tmp_path, capsys) -> None:
    settings = Settings(
        _env_file=None,
        audit_dir=str(tmp_path / "audit"),
        control_db_url="",
    )
    assert control_server._migrate_control_db(settings) == 0
    assert "skipped" in capsys.readouterr().out


def test_migrate_only_creates_sqlite_schema(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'control.db'}"
    settings = Settings(
        _env_file=None,
        audit_dir=str(tmp_path / "audit"),
        control_db_url=url,
    )
    assert control_server._migrate_control_db(settings) == 0
    from coding_agent.control.migrate import has_table

    assert has_table(url, "control_meta")
