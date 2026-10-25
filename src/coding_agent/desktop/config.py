"""Windows 薄客户端的本地配置与路径。"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path


def app_home() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(
        Path.home() / "AppData" / "Local"
    )
    return Path(base) / "CodingAgent"


def config_path() -> Path:
    return app_home() / "launcher.json"


def logs_dir() -> Path:
    return app_home() / "logs"


def state_dir() -> Path:
    return app_home() / "state"


def wsl_install_dir() -> Path:
    return app_home() / "wsl" / "CodingAgent"


def bundled_rootfs() -> Path | None:
    """安装包内的专用 WSL 发行版 rootfs（有则优先使用）。"""
    env = os.environ.get("CODING_AGENT_WSL_ROOTFS", "").strip()
    candidates = []
    if env:
        candidates.append(Path(env))
    candidates.append(
        Path(sys.executable).resolve().parent
        / "wsl"
        / "CodingAgent-rootfs.tar"
    )
    candidates.append(
        Path(__file__).resolve().parents[3]
        / "packaging"
        / "wsl"
        / "CodingAgent-rootfs.tar"
    )
    for path in candidates:
        if path.is_file():
            return path
    return None


@dataclass
class LauncherConfig:
    distro: str = "Ubuntu"
    workspace: str = ""
    python: str = ""
    allow_write: bool = False
    # RFC-0001：当前工作区与最近打开列表；旧 `workspace` 字段继续兼容。
    workspace_id: str = ""
    recent_workspaces: list[str] = field(default_factory=list)
    # M4：A2 控制面数据库；空 = 继续用 gui-sessions.json。
    # 例：mysql+pymysql://user:pass@127.0.0.1:3306/coding_agent
    control_db_url: str = ""

    @classmethod
    def load(cls) -> LauncherConfig | None:
        path = config_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        recent = data.get("recent_workspaces")
        return cls(
            distro=str(data.get("distro") or "Ubuntu"),
            workspace=str(data.get("workspace") or ""),
            python=str(data.get("python") or ""),
            allow_write=bool(data.get("allow_write", False)),
            workspace_id=str(data.get("workspace_id") or ""),
            recent_workspaces=(
                [str(item) for item in recent if item]
                if isinstance(recent, list)
                else []
            ),
            control_db_url=str(data.get("control_db_url") or "").strip(),
        )

    def save(self) -> None:
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not self.distro.strip():
            problems.append("未选择 WSL 发行版")
        if not self.workspace.strip().startswith("/"):
            problems.append("工作区必须是 WSL 内的绝对路径")
        if not self.python.strip().startswith("/"):
            problems.append("Python 必须是 WSL 内的绝对路径")
        return problems
