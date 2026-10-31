"""桌面 host 工作区配置与 A2L 启动命令。"""

from __future__ import annotations

from ubismart.desktop.config import LauncherConfig
from ubismart.desktop.supervisor import build_host_broker_command


def test_host_broker_command_carries_handle_and_url() -> None:
    command = build_host_broker_command(
        url="tcp://127.0.0.1:8791",
        workspace_root=r"D:\Projects\demo",
        workspace_id="ws-1",
        root_handle="h-1",
        security_tier="lowpriv",
    )
    assert "-m" in command and "ubismart.host_broker" in command
    assert "--url" in command and "tcp://127.0.0.1:8791" in command
    assert "--workspace-root" in command and r"D:\Projects\demo" in command
    assert "--root-handle" in command and "h-1" in command


def test_launcher_config_accepts_host_workspace() -> None:
    config = LauncherConfig(
        distro="Ubuntu",
        workspace="/home/me/ws",
        python="/home/me/py",
        host_workspace_root=r"D:\Projects\demo",
        host_workspace_id="ws-1",
        host_root_handle="h-1",
    )
    assert config.validate() == []


def test_launcher_config_rejects_relative_host_workspace() -> None:
    config = LauncherConfig(
        distro="Ubuntu",
        workspace="/home/me/ws",
        python="/home/me/py",
        host_workspace_root="relative/path",
    )
    assert any("host 工作区" in item for item in config.validate())
