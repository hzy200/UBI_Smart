"""工作区与项目 Python 环境探测（Phase 3）。

Agent 自己的解释器（专用发行版里的 `/opt/coding-agent/venv`）与项目解释器
必须分开：项目测试、LSP、依赖安装都用项目自己的环境。这里只做**只读探测**，
不创建 venv、不安装依赖、不执行项目代码。

路径模式：
- `native`：WSL 原生目录（`/home/...`），性能与 inotify 正常；
- `mnt`：Windows 盘挂载（`/mnt/d/...`），9P 较慢且 inotify 不可靠；
- `other`：未知/空路径。
"""

from __future__ import annotations

import os
import platform
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

_VENVS: tuple[tuple[str, str], ...] = (
    (".venv/bin/python", ".venv"),
    ("venv/bin/python", "venv"),
    ("env/bin/python", "env"),
)

_PROBE_TIMEOUT_S = 5.0


def workspace_mode(workspace: str) -> str:
    text = str(workspace or "").strip()
    if not text.startswith("/"):
        return "other"
    if text == "/mnt" or text.startswith("/mnt/"):
        return "mnt"
    return "native"


def _run(python: str, *args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            [python, *args],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _version(python: str) -> str:
    proc = _run(python, "--version")
    if proc is None or proc.returncode != 0:
        return ""
    text = (proc.stdout or proc.stderr).strip()
    return text.removeprefix("Python ").strip()


def _has_pytest(python: str) -> bool:
    proc = _run(python, "-c", "import pytest")
    return proc is not None and proc.returncode == 0


def _find_python(root: Path) -> tuple[str, str]:
    for rel, label in _VENVS:
        candidate = root / rel
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate), label
    fallback = shutil.which("python3") or "python3"
    return fallback, ""


def _package_manager(root: Path) -> tuple[str, str]:
    checks = (
        ("uv.lock", "uv"),
        ("poetry.lock", "poetry"),
        ("Pipfile.lock", "pipenv"),
        ("environment.yml", "conda"),
        ("pyproject.toml", "pip"),
        ("requirements.txt", "pip"),
    )
    for name, manager in checks:
        if (root / name).is_file():
            return manager, name
    if (root / "conda-meta").is_dir():
        return "conda", "conda-meta"
    return "", ""


def probe_project(
    workspace: str, *, agent_python: str | None = None
) -> dict:
    """探测 workspace 的项目解释器与包管理器；永不抛异常。"""
    text = str(workspace or "").strip()
    root = Path(text) if text else Path("")
    exists = bool(text) and root.is_dir()
    writable = bool(exists) and os.access(root, os.W_OK)
    mode = workspace_mode(text)

    warnings: list[str] = []
    if mode == "mnt":
        warnings.append(
            "/mnt 工作区走 9P 文件系统：大仓库的读写、watch 与测试会明显变慢，"
            "inotify 也不可靠；建议复制到 WSL 原生目录。"
        )
    if not text:
        warnings.append("未选择工作区。")
    elif not exists:
        warnings.append("工作区不存在；首次创建会话时会自动创建空目录。")
    elif not writable:
        warnings.append("工作区不可写；写模式与测试可能失败。")

    python, venv_label = _find_python(root) if exists else (
        shutil.which("python3") or "python3",
        "",
    )
    version = _version(python) if exists else ""
    pytest_ready = _has_pytest(python) if exists else False
    if exists and not venv_label:
        warnings.append(
            "未检测到项目虚拟环境（.venv/venv/env），将使用系统 python3；"
            "项目依赖请装进项目自己的环境。"
        )
    if exists and not pytest_ready:
        warnings.append(
            "项目解释器里没有 pytest；自动验证会退化为语法检查。"
        )

    manager, lockfile = _package_manager(root) if exists else ("", "")
    test_command = f"{shlex.quote(python)} -m pytest -q" if pytest_ready else ""

    return {
        "workspace": os.path.abspath(text) if text else "",
        "mode": mode,
        "exists": exists,
        "writable": writable,
        "agent_python": agent_python or sys.executable,
        "agent_version": platform.python_version(),
        "project": {
            "python": python,
            "version": version,
            "source": venv_label or "system",
            "pytest": pytest_ready,
        },
        "package_manager": manager,
        "lockfile": lockfile,
        "test_command": test_command,
        "warnings": warnings,
    }
