"""桌面诊断包（Phase 5）。

把排障所需的信息打成一个 zip：版本、启动器配置、日志尾部、WSL 概况。
日志会做基本的密钥脱敏；诊断包不读取工作区源码，也不包含 API Key。
"""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys
import zipfile
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from ubismart import __version__
from ubismart.desktop.config import (
    LauncherConfig,
    app_home,
    config_path,
    logs_dir,
    state_dir,
)
from ubismart.desktop.wsl import home, list_distros, run

MAX_LOG_BYTES = 2 * 1024 * 1024

_SECRET_PATTERNS = (
    (re.compile(r"sk-[A-Za-z0-9_-]{8,}"), "sk-[redacted]"),
    (
        re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
        r"\1[redacted]",
    ),
    (
        re.compile(
            r"(?i)((?:api[_-]?key|token|secret)\s*[=:]\s*)"
            r"([^\s\"']{6,})"
        ),
        r"\1[redacted]",
    ),
)


def sanitize(text: str) -> str:
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _tail(path: Path) -> bytes:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - MAX_LOG_BYTES))
            return handle.read()
    except OSError:
        return b""


def _wsl_info(config: LauncherConfig) -> str:
    lines = [f"configured distro: {config.distro}"]
    try:
        lines.append("distros: " + ", ".join(list_distros()))
    except Exception as exc:  # noqa: BLE001 - 诊断不应因探测失败而中断
        lines.append(f"distros error: {type(exc).__name__}: {exc}")
    try:
        proc = subprocess.run(
            ["wsl.exe", "--version"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        raw = proc.stdout or proc.stderr
        text = (
            raw.decode("utf-16-le", errors="ignore")
            if b"\x00" in raw
            else raw.decode("utf-8", errors="replace")
        )
        lines.append("wsl --version:")
        lines.append(text.strip())
    except Exception as exc:  # noqa: BLE001
        lines.append(f"wsl --version error: {type(exc).__name__}: {exc}")
    try:
        lines.append(f"HOME: {home(config.distro)}")
        probe = run(
            config.distro,
            'uname -a; python3 --version 2>&1; '
            'test -d "$HOME/.coding-agent" && echo data-dir-ok',
            timeout=20,
        )
        raw = probe.stdout or probe.stderr
        lines.append(
            raw.decode("utf-8", errors="replace").strip()
            if isinstance(raw, bytes)
            else str(raw)
        )
    except Exception as exc:  # noqa: BLE001
        lines.append(f"probe error: {type(exc).__name__}: {exc}")
    return sanitize("\n".join(lines))


def collect_diagnostics(
    config: LauncherConfig, *, now: datetime | None = None
) -> Path:
    target_dir = app_home() / "diagnostics"
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    path = target_dir / f"UBISmart-diagnostics-{stamp}.zip"
    summary = {
        "version": __version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "executable": sys.executable,
        "app_home": str(app_home()),
        "logs_dir": str(logs_dir()),
        "state_dir": str(state_dir()),
        "config": asdict(config),
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "summary.json",
            sanitize(json.dumps(summary, ensure_ascii=False, indent=2)),
        )
        if config_path().is_file():
            archive.writestr(
                "launcher.json",
                sanitize(config_path().read_text(encoding="utf-8")),
            )
        for name in ("launcher.log", "agent.log", "control.log", "server.log"):
            source = logs_dir() / name
            raw = _tail(source)
            if raw:
                archive.writestr(
                    f"logs/{name}",
                    sanitize(raw.decode("utf-8", errors="replace")),
                )
        archive.writestr("wsl.txt", sanitize(_wsl_info(config)))
        archive.writestr(
            "README.txt",
            "UBISmart 诊断包：版本、配置、日志尾部与 WSL 概况。\n"
            "日志已做基本密钥脱敏；不包含工作区源码或 API Key。\n",
        )
    return path
