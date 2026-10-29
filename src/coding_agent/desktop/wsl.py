"""Windows 侧调用 WSL 的辅助函数（Phase 1）。"""

from __future__ import annotations

import re
import shlex
import subprocess
import time
from pathlib import Path

from coding_agent.platform.paths import strip_wsl_scheme

_WIN_DRIVE = re.compile(r"^([A-Za-z]):[\\/](.*)$")
# 刚 import 完或 WSL 服务刚唤醒时，wsl.exe 可能返回 0xC0000142
# （DLL 初始化失败）。等待片刻重试即可。
_TRANSIENT_CODES = {-1073741502, 3221225794, -1073741515, 3221225781}


def _decode(raw: bytes) -> str:
    if b"\x00" in raw:
        return raw.decode("utf-16-le", errors="ignore")
    return raw.decode("utf-8", errors="replace")


def _parse_distros(raw: bytes) -> list[str]:
    names = []
    for line in _decode(raw).splitlines():
        name = line.strip().strip("\ufeff\x00").strip()
        if name:
            names.append(name)
    return names


def distro_exists(name: str) -> bool:
    return name in list_distros()


def import_distro(
    name: str, install_dir: str, tar_path: str, *, timeout: int = 900
) -> None:
    Path(install_dir).parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            "wsl.exe",
            "--import",
            name,
            install_dir,
            tar_path,
            "--version",
            "2",
        ],
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"导入 WSL 发行版失败：{_text(proc.stderr) or _text(proc.stdout)}"
        )


def list_distros() -> list[str]:
    try:
        proc = subprocess.run(
            ["wsl.exe", "-l", "-q"],
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    return _parse_distros(proc.stdout)


def run(
    distro: str, script: str, *, timeout: int = 30, attempts: int = 4
) -> subprocess.CompletedProcess:
    last: subprocess.CompletedProcess | None = None
    for attempt in range(max(1, attempts)):
        last = subprocess.run(
            [
                "wsl.exe",
                "-d",
                distro,
                "--",
                "/bin/bash",
                "-lc",
                script,
            ],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        if last.returncode not in _TRANSIENT_CODES:
            return last
        if attempt + 1 < attempts:
            time.sleep(1.0)
    assert last is not None
    return last


def _text(raw: bytes) -> str:
    return _decode(raw).strip()


def home(distro: str) -> str:
    proc = run(distro, 'printf "%s" "$HOME"')
    value = _text(proc.stdout)
    if proc.returncode != 0 or not value.startswith("/"):
        raise RuntimeError(
            f"无法获取 {distro} 的 HOME："
            f"{_text(proc.stderr) or proc.returncode}"
        )
    return value


def python_status(distro: str, python: str) -> tuple[bool, str]:
    script = (
        f"{shlex.quote(python)} -c "
        "\"import fastapi, uvicorn, langgraph, pydantic, coding_agent; "
        "print('ok')\" 2>&1"
    )
    try:
        proc = run(distro, script, timeout=30)
    except subprocess.TimeoutExpired:
        return False, "检查超时"
    text = _text(proc.stdout) or _text(proc.stderr)
    return proc.returncode == 0 and "ok" in text, text


def ensure_dir(distro: str, path: str) -> None:
    proc = run(distro, f"mkdir -p {shlex.quote(path)}")
    if proc.returncode != 0:
        raise RuntimeError(
            f"无法创建 WSL 目录 {path}：{_text(proc.stderr) or proc.returncode}"
        )


def to_wsl_path(path: str) -> str:
    """Windows 路径 → WSL 路径；已是 POSIX 路径则原样返回。"""
    # Git Bash / MSYS 会把以 `/` 开头的参数改写成 Windows 路径；`wsl:` 前缀
    # 是文档约定的绕行方式，这里必须先去掉前缀再判断。
    text = strip_wsl_scheme(str(path or "").strip().strip('"'))
    if not text:
        return ""
    if text.startswith("\\\\wsl$\\") or text.startswith("//wsl$/"):
        parts = [p for p in text.replace("\\", "/").split("/") if p]
        # \\wsl$\Ubuntu\home\user -> ['wsl$', 'Ubuntu', 'home', 'user']
        if len(parts) >= 3:
            return "/" + "/".join(parts[2:])
    match = _WIN_DRIVE.match(text)
    if match:
        drive, rest = match.groups()
        return f"/mnt/{drive.lower()}/{rest.replace(chr(92), '/')}"
    return text.replace("\\", "/")
