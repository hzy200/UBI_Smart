"""Phase D4：Linux systemd 单元、安装脚本与迁移入口。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from ubismart.config import Settings
from ubismart.servers import control_server

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
    assert "ExecStart=/opt/coding-agent/bin/UBISmartAgent" in text


def test_control_unit_orders_agent_and_runs_migration() -> None:
    text = _unit("coding-agent-control.service")
    assert "coding-agent-agent.service" in text.split("[Service]")[0]
    assert "Wants=network-online.target coding-agent-agent.service" in text
    assert (
        "ExecStartPre=/opt/coding-agent/bin/UBISmartControl --migrate-only"
        in text
    )
    assert "ExecStart=/opt/coding-agent/bin/UBISmartControl" in text
    assert "ProtectHome=true" in text
    assert "RestrictNamespaces=true" in text
    assert "PrivateDevices=true" in text


def test_install_script_is_valid_shell() -> None:
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("没有 bash")
    script = (SYSTEMD / "install.sh").read_text(encoding="utf-8")
    # 脚本走 stdin 而不是文件名：Windows 上的 `bash.EXE` 可能是 WSL 启动器，
    # 它会把 `C:\...\install.sh` 里的反斜杠吃掉，误报 "No such file"（rc=127）。
    # 用 bytes 而不是 text=True：Windows 的文本管道会把 `\n` 翻译成 `\r\n`，
    # bash 会把行尾的 `\r` 当成语法错误。显式 UTF-8 解码 WSL 的 stderr，
    # 否则宿主默认的 GBK 解码会在 subprocess 读取线程里抛 UnicodeDecodeError。
    result = subprocess.run(
        [bash, "-n"],
        input=script.encode("utf-8"),
        capture_output=True,
        check=False,
        timeout=60,
    )
    stderr = result.stderr.decode("utf-8", "replace")
    assert result.returncode == 0, stderr


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
    from ubismart.control.migrate import has_table

    assert has_table(url, "control_meta")
