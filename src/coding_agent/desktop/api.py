"""pywebview 与 Web UI 之间的桌面桥（Phase 3）。

只暴露三个动作：选文件夹、在 VS Code 打开、把 `/mnt` 工作区复制到 WSL 原生
目录。所有参数都按列表传给子进程，不拼 shell。
"""

from __future__ import annotations

import functools
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import uuid
import webbrowser
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

from coding_agent.desktop.config import LauncherConfig
from coding_agent.desktop.wsl import home, run, to_wsl_path

_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_LOG = logging.getLogger(__name__)


def _text(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace").strip()


def _host_target(
    root: str, relative_path: str
) -> tuple[Path | None, dict | None]:
    """把本机工作区内的相对路径解析成绝对路径。

    拒绝绝对路径与 `..`，并用 resolve 后的再次包含判断挡住符号链接逃逸。
    """
    root_text = str(root or "").strip()
    rel_text = str(relative_path or "").strip().replace("\\", "/")
    if not root_text or not os.path.isabs(root_text):
        return None, {"ok": False, "error": "工作区必须是本机绝对路径"}
    rel = Path(rel_text)
    if rel.is_absolute() or ".." in rel.parts:
        return None, {"ok": False, "error": "路径必须位于工作区内"}
    root_path = Path(root_text).expanduser().resolve()
    target = (root_path / rel).resolve()
    try:
        target.relative_to(root_path)
    except ValueError:
        return None, {"ok": False, "error": "路径必须位于工作区内"}
    if not target.exists():
        return None, {"ok": False, "error": "文件不存在"}
    return target, None


def origin_of(url: str) -> str:
    parsed = urlparse(str(url or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def is_loopback_origin(origin: str) -> bool:
    parsed = urlparse(str(origin or ""))
    return parsed.scheme in {"http", "https"} and (
        (parsed.hostname or "").lower() in _LOOPBACK_HOSTS
    )


def _requires_trust(denied):
    def decorate(function):
        @functools.wraps(function)
        def wrapper(self, *args, **kwargs):
            if not self._trusted():
                _LOG.warning(
                    "桌面桥拒绝 %s：allowed=%s actual=%s",
                    function.__name__,
                    self.allowed_origin or "(empty)",
                    self._current_origin() or "(unknown)",
                )
                return denied() if callable(denied) else denied
            return function(self, *args, **kwargs)

        return wrapper

    return decorate


_DENIED = {
    "ok": False,
    "error": "桌面桥拒绝：当前页面来源不可信",
}


def _active_window():
    try:
        import webview

        return webview.windows[0] if webview.windows else None
    except Exception:  # noqa: BLE001 - 桥接调用可能在窗口销毁后到达
        return None


def _window_maximized(window) -> bool:
    if sys.platform == "win32":
        try:
            import ctypes

            handle = getattr(getattr(window, "native", None), "Handle", None)
            if handle is not None:
                return bool(ctypes.windll.user32.IsZoomed(int(handle.ToInt64())))
        except Exception:  # noqa: BLE001 - 取不到原生句柄时退回 .NET 状态
            pass
    try:
        return "Maximized" in str(window.native.WindowState)
    except Exception:  # noqa: BLE001
        return False


class DesktopApi:
    def __init__(
        self,
        config: LauncherConfig,
        *,
        allowed_origin: str = "",
        origin_provider: Callable[[], str] | None = None,
    ) -> None:
        self.config = config
        self.allowed_origin = origin_of(allowed_origin)
        self._origin_provider = origin_provider
        self._copies: dict[str, dict] = {}
        self._lock = threading.Lock()

    def _current_origin(self) -> str:
        if self._origin_provider is not None:
            return origin_of(self._origin_provider())
        try:
            import webview

            window = webview.windows[0]
            getter = getattr(window, "get_current_url", None)
            if not callable(getter):
                return ""
            return origin_of(str(getter() or ""))
        except Exception:  # noqa: BLE001 - 无法证明来源时 fail closed
            return ""

    def _trusted(self) -> bool:
        expected = origin_of(self.allowed_origin)
        if not expected or not is_loopback_origin(expected):
            return False
        return self._current_origin() == expected

    @_requires_trust(lambda: "")
    def pick_folder(self) -> str:
        """打开 Windows 原生文件夹选择器，返回 WSL 路径。"""
        try:
            import webview

            window = webview.windows[0]
            picked = window.create_file_dialog(webview.FileDialog.FOLDER)
            if picked:
                return to_wsl_path(picked[0])
        except Exception:  # noqa: BLE001 - 对话框失败按“未选择”处理
            pass
        return ""

    @_requires_trust(_DENIED)
    def open_in_vscode(self, path: str) -> dict:
        target = str(path or "").strip()
        if not target.startswith("/"):
            return {"ok": False, "error": "需要 WSL 绝对路径"}
        code = shutil.which("code") or shutil.which("code.cmd")
        if not code:
            return {
                "ok": False,
                "error": "未找到 VS Code 的 code 命令；请确认已安装并加入 PATH",
            }
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            subprocess.Popen(  # noqa: S603 - 列表参数，无 shell
                [
                    code,
                    "--remote",
                    f"wsl+{self.config.distro}",
                    target,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
        except OSError as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "target": target}

    @_requires_trust(_DENIED)
    def open_host_in_vscode(self, root: str, relative_path: str = "") -> dict:
        """在 VS Code 打开本机工作区内的文件。"""
        target, error = _host_target(root, relative_path)
        if error is not None:
            return error
        code = shutil.which("code") or shutil.which("code.cmd")
        if not code:
            return {
                "ok": False,
                "error": "未找到 VS Code 的 code 命令；请确认已安装并加入 PATH",
            }
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            subprocess.Popen(  # noqa: S603 - 列表参数，无 shell
                [code, str(target)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
        except OSError as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "target": str(target)}

    @_requires_trust(_DENIED)
    def reveal_in_explorer(self, root: str, relative_path: str = "") -> dict:
        """在系统文件管理器中定位本机工作区内的文件。"""
        target, error = _host_target(root, relative_path)
        if error is not None:
            return error
        if os.name == "nt":
            args = ["explorer.exe", f"/select,{target}"]
            flags = 0
        elif sys.platform == "darwin":
            args = ["open", "-R", str(target)]
            flags = 0
        else:
            folder = target if target.is_dir() else target.parent
            args = ["xdg-open", str(folder)]
            flags = 0
        try:
            subprocess.Popen(  # noqa: S603 - 列表参数，无 shell
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
        except OSError as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "target": str(target)}

    @_requires_trust(_DENIED)
    def copy_to_wsl(self, source: str, name: str = "") -> dict:
        """把 `/mnt/...` 工作区复制到 `~/.coding-agent/workspaces/<name>`。"""
        src = to_wsl_path(str(source or "").strip()).rstrip("/")
        if not src.startswith("/mnt/"):
            return {
                "ok": False,
                "error": "只支持从 Windows 盘（/mnt/...）复制到 WSL 原生目录",
            }
        safe = str(name or Path(src).name or "").strip()
        if not _NAME_RE.match(safe) or safe in {".", ".."}:
            return {"ok": False, "error": "目标名称只允许字母、数字、_ . -（1–64）"}
        try:
            root = home(self.config.distro)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"无法读取 WSL HOME：{exc}"}
        dest = f"{root}/.coding-agent/workspaces/{safe}"
        check = run(
            self.config.distro,
            f"test -e {shlex.quote(src)} && echo SRC; "
            f"test -e {shlex.quote(dest)} && echo DEST; true",
        )
        marks = set(_text(check.stdout).splitlines())
        if "SRC" not in marks:
            return {"ok": False, "error": f"源目录不存在：{src}"}
        if "DEST" in marks:
            return {"ok": False, "error": f"目标已存在，未覆盖：{dest}"}

        job_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._copies[job_id] = {
                "state": "running",
                "source": src,
                "target": dest,
                "error": "",
            }

        def worker() -> None:
            status = {"state": "error", "error": ""}
            try:
                make = run(
                    self.config.distro,
                    f"mkdir -p {shlex.quote(dest.rsplit('/', 1)[0])}",
                )
                if make.returncode != 0:
                    raise RuntimeError(_text(make.stderr) or "无法创建目标目录")
                flags = (
                    getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    if os.name == "nt"
                    else 0
                )
                proc = subprocess.run(  # noqa: S603 - 列表参数，无 shell
                    [
                        "wsl.exe",
                        "-d",
                        self.config.distro,
                        "--",
                        "cp",
                        "-a",
                        "--",
                        src,
                        dest,
                    ],
                    capture_output=True,
                    timeout=3600,
                    check=False,
                    creationflags=flags,
                )
                if proc.returncode == 0:
                    status = {"state": "done", "error": ""}
                else:
                    detail = _text(proc.stderr) or _text(proc.stdout)
                    status = {
                        "state": "error",
                        "error": detail or f"cp 退出码 {proc.returncode}",
                    }
            except Exception as exc:  # noqa: BLE001 - 后台失败要回报给 UI
                status = {"state": "error", "error": f"{type(exc).__name__}: {exc}"}
            with self._lock:
                self._copies[job_id].update(status)

        threading.Thread(
            target=worker, name=f"copy-workspace-{job_id}", daemon=True
        ).start()
        return {"ok": True, "job_id": job_id, "target": dest}

    @_requires_trust(
        lambda: {"state": "denied", "error": "当前页面来源不可信"}
    )
    def copy_status(self, job_id: str) -> dict:
        with self._lock:
            status = self._copies.get(str(job_id or ""))
            if status is None:
                return {"state": "unknown", "error": ""}
            return dict(status)

    @_requires_trust(_DENIED)
    def collect_diagnostics(self) -> dict:
        try:
            from coding_agent.desktop.diagnostics import (
                collect_diagnostics,
            )

            path = collect_diagnostics(self.config)
        except Exception as exc:  # noqa: BLE001 - 诊断失败要可读返回
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "path": str(path)}

    @_requires_trust(_DENIED)
    def reveal_diagnostics(self, path: str) -> dict:
        """在资源管理器中选中诊断包；只允许 app_home 下的文件。"""
        from coding_agent.desktop.config import app_home

        target = Path(str(path or "")).resolve()
        try:
            target.relative_to(app_home().resolve())
        except ValueError:
            return {"ok": False, "error": "只允许打开应用数据目录内的文件"}
        if not target.is_file():
            return {"ok": False, "error": "诊断包不存在"}
        try:
            subprocess.Popen(  # noqa: S603, S607 - explorer 是系统命令
                ["explorer", "/select,", str(target)]
            )
        except OSError as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True}

    @_requires_trust(_DENIED)
    def check_update(self) -> dict:
        from coding_agent.desktop.update import (
            check_update,
            manifest_url,
        )

        url = manifest_url()
        if not url:
            return {
                "configured": False,
                "ok": True,
                "available": False,
                "message": "未配置 AGENT_UPDATE_MANIFEST_URL",
            }
        from coding_agent import __version__

        return check_update(__version__, url)

    @_requires_trust(_DENIED)
    def open_url(self, url: str) -> dict:
        parsed = urlparse(str(url or ""))
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return {"ok": False, "error": "只允许打开 http(s) 链接"}
        try:
            webbrowser.open(str(url))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True}

    @_requires_trust(_DENIED)
    def window_state(self) -> dict:
        """返回自绘标题栏需要的窗口状态。"""
        window = _active_window()
        if window is None:
            return {"ok": False, "error": "窗口尚未就绪", "maximized": False}
        return {"ok": True, "maximized": _window_maximized(window)}

    @_requires_trust(_DENIED)
    def window_minimize(self) -> dict:
        window = _active_window()
        if window is None:
            return {"ok": False, "error": "窗口尚未就绪"}
        window.minimize()
        return {"ok": True, "maximized": False}

    @_requires_trust(_DENIED)
    def window_toggle_maximize(self) -> dict:
        window = _active_window()
        if window is None:
            return {"ok": False, "error": "窗口尚未就绪", "maximized": False}
        maximized = _window_maximized(window)
        if maximized:
            window.restore()
        else:
            window.maximize()
        return {"ok": True, "maximized": not maximized}

    @_requires_trust(_DENIED)
    def window_close(self) -> dict:
        window = _active_window()
        if window is None:
            return {"ok": False, "error": "窗口尚未就绪"}
        window.destroy()
        return {"ok": True}
