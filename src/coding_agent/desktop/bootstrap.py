"""桌面壳的服务端启动/关闭。

pywebview 只负责窗口；所有 agent 能力仍由本地 FastAPI 提供。这样桌面与
未来的 Web 部署共用同一条事件/审批链路。
"""

from __future__ import annotations

import json
import secrets
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coding_agent.access import ACCESS_READ_ONLY
from coding_agent.config import Settings, get_settings
from coding_agent.desktop.http import local_open, wait_health
from coding_agent.web import create_app
from coding_agent.web.auth import AuthConfig
from coding_agent.workspace_lock import WorkspaceLock

_LOCAL_USER = "local"


@dataclass
class DesktopServer:
    app: Any
    token: str
    host: str
    port: int
    server: Any
    thread: threading.Thread
    lock: WorkspaceLock | None = None

    @property
    def url(self) -> str:
        # token 放在 fragment 里，不会出现在服务端访问日志/Referer。
        return f"http://{self.host}:{self.port}/#token={self.token}"

    def stop(self) -> None:
        try:
            self.server.should_exit = True
        except Exception:  # noqa: BLE001 - 关闭失败不应掩盖原异常
            pass
        self.thread.join(timeout=10)
        if self.lock is not None:
            self.lock.release()
            self.lock = None


def _ephemeral_auth() -> tuple[str, AuthConfig]:
    token = secrets.token_urlsafe(32)
    return token, AuthConfig(
        tokens={token: _LOCAL_USER},
        allow_anonymous_local=False,
    )


def _wait_started(server: Any, timeout_s: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if getattr(server, "started", False):
            return
        if not getattr(server, "alive", True):
            break
        time.sleep(0.05)
    raise RuntimeError("桌面本地服务启动超时")


def _actual_port(server: Any) -> int:
    for candidate in getattr(server, "servers", ()) or ():
        for socket in getattr(candidate, "sockets", ()) or ():
            try:
                return int(socket.getsockname()[1])
            except Exception:  # noqa: BLE001
                continue
    raise RuntimeError("无法取得桌面本地服务端口")


def _cancel_all(server: DesktopServer) -> None:
    request = urllib.request.Request(
        f"http://127.0.0.1:{server.port}/api/desktop/cancel_all",
        data=json.dumps({}).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {server.token}",
            "Content-Type": "application/json",
        },
    )
    with local_open(request, 8) as response:
        response.read()


def start_server(
    settings: Settings | None = None,
    *,
    workspace: str = "",
    allow_write: bool = False,
    port: int = 0,
    acquire_lock: bool = True,
) -> DesktopServer:
    """启动 loopback 本地服务；返回可关闭的句柄。"""
    import uvicorn

    settings = settings or get_settings()
    if workspace:
        settings = settings.model_copy(update={"wsl_workspace": workspace})
    token, auth = _ephemeral_auth()

    lock: WorkspaceLock | None = None
    if acquire_lock:
        root = Path(settings.resolved_audit_dir)
        lock = WorkspaceLock(root, f"desktop-gui:{settings.wsl_workspace or Path.cwd()}")
        if not lock.acquire():
            raise RuntimeError(
                "已有一个桌面 GUI 在使用同一工作区/数据目录；请先关闭它，"
                "或通过 --workspace 指定不同目录。"
            )

    app = create_app(
        settings,
        auth,
        allow_write=allow_write,
        multi_user=False,
        default_access_mode=ACCESS_READ_ONLY,
        stream_buffer=True,
        desktop_mode=True,
        client_source="desktop",
    )
    # 只接受本机 Host，降低 DNS rebinding 风险。
    from starlette.middleware.trustedhost import TrustedHostMiddleware

    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "localhost."],
    )

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=max(0, int(port)),
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(
        target=server.run,
        name="coding-agent-desktop-server",
        daemon=True,
    )
    thread.start()
    try:
        _wait_started(server)
        actual = _actual_port(server) if port == 0 else int(port)
        wait_health(f"http://127.0.0.1:{actual}/healthz")
    except Exception:
        server.should_exit = True
        thread.join(timeout=5)
        if lock is not None:
            lock.release()
        raise

    return DesktopServer(
        app=app,
        token=token,
        host="127.0.0.1",
        port=actual,
        server=server,
        thread=thread,
        lock=lock,
    )


def run_desktop(
    settings: Settings | None = None,
    *,
    workspace: str = "",
    allow_write: bool = False,
    port: int = 0,
) -> None:
    """启动本地服务与 pywebview 窗口；窗口关闭后停止服务。"""
    try:
        import webview
    except ImportError as exc:  # pragma: no cover - 取决于可选依赖
        raise RuntimeError(
            '未安装桌面依赖。请运行：pip install -e ".[web,desktop]"'
        ) from exc

    desktop = start_server(
        settings,
        workspace=workspace,
        allow_write=allow_write,
        port=port,
    )
    try:
        window = webview.create_window(
            "CodingAgent",
            desktop.url,
            width=1440,
            height=920,
            min_size=(980, 620),
            maximized=True,
            text_select=True,
            zoomable=True,
            confirm_close=True,
        )

        def on_closing() -> bool:
            # 取消运行放到后台；关闭事件本身必须立刻返回，否则
            # Windows 会把窗口标记为“未响应”。
            def cancel_later() -> None:
                try:
                    _cancel_all(desktop)
                except Exception:  # noqa: BLE001 - 关窗不能被清理失败卡死
                    pass

            threading.Thread(
                target=cancel_later, name="desktop-cancel", daemon=True
            ).start()
            return True

        if window is not None:
            window.events.closing += on_closing
        webview.start()
    finally:
        desktop.stop()
