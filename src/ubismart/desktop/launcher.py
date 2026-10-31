"""Windows 桌面启动器：配置向导 → 启动 WSL A3 + Windows A2 → 打开 WebView。"""

from __future__ import annotations

import argparse
import hashlib
import html
import logging
import os
import secrets
import threading
import time
from pathlib import Path

from coding_agent.desktop.api import (
    DesktopApi,
    is_loopback_origin,
    origin_of,
)
from coding_agent.desktop.config import (
    LauncherConfig,
    bundled_rootfs,
    logs_dir,
    state_dir,
    wsl_install_dir,
)
from coding_agent.desktop.setup import run_setup
from coding_agent.desktop.supervisor import (
    AgentServer,
    ControlServer,
    HostBrokerProc,
    start_agent,
    start_control,
    start_host_broker,
)
from coding_agent.desktop.watchdog import WatchTarget, supervise_many
from coding_agent.desktop.wsl import distro_exists, import_distro, to_wsl_path
from coding_agent.session.workspace_lock import WorkspaceLock

_LOG = logging.getLogger("coding_agent.desktop")


def _logging() -> None:
    logs_dir().mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(logs_dir() / "launcher.log"),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        encoding="utf-8",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CodingAgent desktop launcher")
    parser.add_argument("--setup", action="store_true", help="重新运行配置向导")
    parser.add_argument(
        "--host-mode",
        action="store_true",
        help="使用旧的 Windows 宿主模式（不启动 WSL Server）",
    )
    parser.add_argument("--workspace", default="", help="覆盖 WSL 工作区")
    parser.add_argument(
        "--host-workspace",
        default="",
        help="Windows 本地 host 工作区；设置后启动 A2L",
    )
    parser.add_argument("--write", action="store_true", help="允许会话选择写模式")
    return parser


def _icon_path() -> str:
    """窗口图标；PyInstaller 会把 web/static 放到同一相对位置。"""
    candidate = Path(__file__).resolve().parent.parent / "web" / "static" / "icon.ico"
    return str(candidate) if candidate.is_file() else ""


def _restore_resize_border(window: object) -> None:
    """给无边框窗口补回可缩放边框。

    pywebview 的 frameless 会移除 WS_THICKFRAME，随之失去边缘缩放和正确的
    最大化工作区。标题栏仍由页面自绘，这里只恢复样式位和阴影边框。
    """
    if os.name != "nt":
        return
    try:
        import ctypes

        handle = getattr(getattr(window, "native", None), "Handle", None)
        if handle is None:
            return
        hwnd = int(handle.ToInt64())
        user32 = ctypes.windll.user32
        gwl_style = -16
        style = user32.GetWindowLongW(hwnd, gwl_style)
        # WS_THICKFRAME | WS_MAXIMIZEBOX | WS_MINIMIZEBOX
        user32.SetWindowLongW(hwnd, gwl_style, style | 0x00040000 | 0x00010000 | 0x00020000)
        # SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_FRAMECHANGED
        user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0020)
    except Exception:  # noqa: BLE001 - 样式恢复失败不应阻止窗口显示
        _LOG.exception("恢复无边框窗口缩放边框失败")


def _open_window(
    control: ControlServer,
    agent: AgentServer,
    config: LauncherConfig,
    host_broker: HostBrokerProc | None = None,
) -> None:
    try:
        import webview
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            '未安装桌面依赖。请运行：pip install -e ".[desktop]"'
        ) from exc

    origin = origin_of(control.url)
    if not is_loopback_origin(origin):
        raise RuntimeError(f"桌面窗口只允许加载 loopback 页面：{control.url}")
    window = webview.create_window(
        "CodingAgent",
        control.url,
        width=1440,
        height=920,
        min_size=(980, 620),
        maximized=True,
        frameless=True,
        easy_drag=False,
        background_color="#0f1117",
        text_select=True,
        zoomable=True,
        confirm_close=True,
        js_api=DesktopApi(config, allowed_origin=origin),
    )
    window.events.shown += lambda: _restore_resize_border(window)

    holder = {
        "agent": agent,
        "control": control,
        "host_broker": host_broker,
    }
    stop_event = threading.Event()
    lock = threading.Lock()

    def _stop_one(name: str, current: object) -> None:
        try:
            current.stop(timeout=3)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - 关窗不能被清理失败卡死
            _LOG.exception("停止%s失败", name)

    def _force_kill(items: list[tuple[str, object]]) -> None:
        for name, current in items:
            proc = getattr(current, "proc", None)
            if proc is None or proc.poll() is not None:
                continue
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                _LOG.exception("强制结束%s失败", name)

    def stop_all() -> None:
        with lock:
            items = [
                ("A2L", holder["host_broker"]),
                ("控制面", holder["control"]),
                ("Agent Server", holder["agent"]),
            ]
        items = [(name, current) for name, current in items if current is not None]
        # 并行停止；任何一个卡住都不能拖住另外两个。
        threads = []
        for name, current in items:
            thread = threading.Thread(
                target=_stop_one,
                args=(name, current),
                name=f"stop-{name}",
                daemon=True,
            )
            thread.start()
            threads.append(thread)
        for thread in threads:
            thread.join(timeout=8)
        _force_kill(items)

    shutdown_thread: threading.Thread | None = None

    def on_closing() -> bool:
        nonlocal shutdown_thread
        stop_event.set()
        # 关闭窗口的 UI 线程不能被进程清理阻塞，否则 Windows 会显示“未响应”。
        shutdown_thread = threading.Thread(
            target=stop_all, name="desktop-shutdown", daemon=True
        )
        shutdown_thread.start()
        return True

    def restart_agent() -> AgentServer:
        # 端口与 token 复用，A2 的 AGENT_INTERNAL_URL 无需变化。
        current = holder["agent"]
        fresh = start_agent(
            config,
            log_path=logs_dir() / "agent.log",
            port=current.port,
            token=current.token,
        )
        with lock:
            holder["agent"] = fresh
        if stop_event.is_set():
            fresh.stop()
        else:
            _LOG.warning("Agent Server 已重启，端口 %s", fresh.port)
        return fresh

    def restart_host_broker() -> HostBrokerProc | None:
        current = holder["host_broker"]
        root = str(config.host_workspace_root or "").strip()
        if not root:
            return None
        fresh = start_host_broker(
            config,
            workspace_root=root,
            workspace_id=(
                config.host_workspace_id
                or "ws-local"
            ),
            root_handle=(
                config.host_root_handle
                or "h_"
                + hashlib.sha256(root.encode("utf-8")).hexdigest()[:16]
            ),
            log_path=logs_dir() / "host-broker.log",
            port=current.port if current is not None else None,
            security_tier=config.host_security_tier,
            allow_write=config.host_allow_write,
            allow_exec=config.host_exec_enabled,
            toolchains=config.host_toolchains,
            snapshot_dir=config.host_snapshot_dir,
            policy_file=config.host_policy_file,
            policy_pubkey=config.host_policy_pubkey,
            policy_pubkey_file=config.host_policy_pubkey_file,
            attestation=config.host_attestation,
            attestation_key=config.host_attestation_key,
        )
        with lock:
            holder["host_broker"] = fresh
        if stop_event.is_set():
            fresh.stop()
        else:
            _LOG.warning("A2L 已重启，端口 %s", fresh.port)
        return fresh

    def restart_control() -> ControlServer:
        current = holder["control"]
        current_agent = holder["agent"]
        fresh = start_control(
            config,
            agent_port=current_agent.port,
            agent_token=current_agent.token,
            log_path=logs_dir() / "control.log",
            port=current.port,
            token=current.token,
        )
        with lock:
            holder["control"] = fresh
        if stop_event.is_set():
            fresh.stop()
        else:
            _LOG.warning("控制面已重启，端口 %s", fresh.port)
        return fresh

    if window is not None:
        window.events.closing += on_closing

    targets = [
        WatchTarget("agent", agent, restart_agent),
        WatchTarget("control", control, restart_control),
    ]
    if host_broker is not None:
        targets.append(
            WatchTarget(
                "host-broker", host_broker, restart_host_broker
            )
        )

    def watchdog() -> None:
        result = supervise_many(targets, stop_event)
        if result.startswith("failed"):
            _LOG.error("桌面服务自动恢复失败：%s", result)

    threading.Thread(
        target=watchdog, name="desktop-watchdog", daemon=True
    ).start()

    # 自动化冒烟：设置 AGENT_DESKTOP_SMOKE_CLOSE 秒后自动关窗。
    smoke_s = float(os.environ.get("AGENT_DESKTOP_SMOKE_CLOSE", "0") or 0)
    if smoke_s > 0:
        def closer() -> None:
            time.sleep(smoke_s)
            for candidate in list(webview.windows):
                try:
                    candidate.destroy()
                except Exception:  # noqa: BLE001
                    pass
        threading.Thread(target=closer, daemon=True).start()
    webview.start(icon=_icon_path())
    # 窗口已经消失，这里只等待后台清理完成后进程再退出。
    if shutdown_thread is not None:
        shutdown_thread.join(timeout=12)
        if shutdown_thread.is_alive():
            with lock:
                items = [
                    ("A2L", holder["host_broker"]),
                    ("控制面", holder["control"]),
                    ("Agent Server", holder["agent"]),
                ]
            _force_kill(
                [(name, current) for name, current in items if current is not None]
            )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    _logging()
    if args.host_mode:
        from coding_agent.desktop.bootstrap import run_desktop

        run_desktop()
        return 0

    config = LauncherConfig.load()
    if config is None and not args.setup:
        rootfs = bundled_rootfs()
        if rootfs is not None:
            try:
                if not distro_exists("CodingAgent"):
                    _LOG.info("导入专用 WSL 发行版 %s", rootfs)
                    import_distro(
                        "CodingAgent",
                        str(wsl_install_dir()),
                        str(rootfs),
                    )
                config = LauncherConfig(
                    distro="CodingAgent",
                    workspace=(
                        "/home/agent/.coding-agent/workspaces/default"
                    ),
                    python="/opt/coding-agent/venv/bin/python",
                )
                config.save()
            except Exception:  # noqa: BLE001 - 回退到手动向导
                _LOG.exception("导入专用 WSL 发行版失败，回退到向导")
                config = None
    if config is None or args.setup:
        try:
            config = run_setup()
        except Exception as exc:  # noqa: BLE001 - 向导失败要可读
            _LOG.exception("配置向导失败")
            raise RuntimeError(f"配置向导失败：{exc}") from exc
        if config is None:
            return 1
        config.save()

    changed = False
    if args.workspace:
        config.workspace = to_wsl_path(args.workspace)
        changed = True
    if args.write:
        config.allow_write = True
        changed = True
    if args.host_workspace:
        config.host_workspace_root = str(args.host_workspace)
        changed = True
    if changed:
        config.save()

    host_root = str(config.host_workspace_root or "").strip()
    host_workspace_id = config.host_workspace_id or "ws-local"
    host_root_handle = (
        config.host_root_handle
        or (
            "h_"
            + hashlib.sha256(host_root.encode("utf-8")).hexdigest()[:16]
            if host_root
            else ""
        )
    )
    host_a2l_id = "a2l-local"
    host_env: dict[str, str] = {}
    if host_root:
        try:
            control_port = int(
                os.environ.get("AGENT_DESKTOP_CONTROL_PORT", "8780")
                or 8780
            )
        except ValueError:
            control_port = 8780
        consent_token = secrets.token_urlsafe(24)
        host_env = {
            "AGENT_HOST_BROKER_ENABLED": "1",
            # 本机工作区必须走注册表：裸路径无法映射到 A2L 的 root_handle，
            # 否则前端选了文件夹、A3 仍会落到启动时的默认目录。
            "AGENT_WORKSPACE_REGISTRY": "1",
            "AGENT_WORKSPACE_KIND_DEFAULT": "host",
            "AGENT_HOST_WORKSPACE_ID": host_workspace_id,
            "AGENT_HOST_ROOT_HANDLE": host_root_handle,
            "AGENT_HOST_A2L_ID": host_a2l_id,
            # 授权请求走 A2 的聊天界面，不再弹独立的本机窗口。
            "AGENT_CONSENT_URL": f"http://127.0.0.1:{control_port}",
            "AGENT_CONSENT_TOKEN": consent_token,
            "AGENT_HOST_BROKER_ALLOW_WRITE": (
                "1" if config.host_allow_write else "0"
            ),
            "AGENT_HOST_BROKER_EXEC_ENABLED": (
                "1" if config.host_exec_enabled else "0"
            ),
            "AGENT_HOST_BROKER_TOOLCHAINS": config.host_toolchains,
            "AGENT_HOST_SNAPSHOT_DIR": config.host_snapshot_dir,
            "AGENT_HOST_BROKER_DEVICE_REGISTRY": (
                config.host_device_registry
            ),
            "AGENT_HOST_BROKER_DEVICE_POLICY": config.host_device_policy,
        }
        # _agent_env 读取进程环境；显式设置，保证 A3 启动监听。
        os.environ.update(host_env)

    lock = WorkspaceLock(state_dir(), "desktop-launcher")
    if not lock.acquire():
        _LOG.error("已有 CodingAgent 实例在运行")
        return 2
    try:
        agent = start_agent(config, log_path=logs_dir() / "agent.log")
        _LOG.info("agent server started on %s", agent.port)
        host_broker = None
        try:
            if host_root:
                host_broker = start_host_broker(
                    config,
                    workspace_root=host_root,
                    workspace_id=host_workspace_id,
                    root_handle=host_root_handle,
                    log_path=logs_dir() / "host-broker.log",
                    security_tier=config.host_security_tier,
                    allow_write=config.host_allow_write,
                    allow_exec=config.host_exec_enabled,
                    toolchains=config.host_toolchains,
                    snapshot_dir=config.host_snapshot_dir,
                    policy_file=config.host_policy_file,
                    policy_pubkey=config.host_policy_pubkey,
                    policy_pubkey_file=config.host_policy_pubkey_file,
                    attestation=config.host_attestation,
                    attestation_key=config.host_attestation_key,
                )
                _LOG.info("A2L started on %s", host_broker.port)
        except Exception:
            agent.stop()
            raise
        try:
            control = start_control(
                config,
                agent_port=agent.port,
                agent_token=agent.token,
                log_path=logs_dir() / "control.log",
                extra_env=host_env,
            )
        except Exception:
            if host_broker is not None:
                host_broker.stop()
            agent.stop()
            raise
        _LOG.info("control plane started on %s", control.port)
        if host_broker is None:
            _open_window(control, agent, config)
        else:
            _open_window(control, agent, config, host_broker)
        return 0
    except Exception as exc:  # noqa: BLE001 - 启动失败要写日志并返回非零
        _LOG.exception("桌面启动失败")
        try:
            import webview

            webview.create_window(
                "CodingAgent 启动失败",
                html=(
                    "<h3>CodingAgent 启动失败</h3>"
                    f"<pre>{html.escape(type(exc).__name__ + ': ' + str(exc))}</pre>"
                    f"<p>日志：{html.escape(str(logs_dir() / 'launcher.log'))}</p>"
                ),
                width=720,
                height=420,
            )
            webview.start(icon=_icon_path())
        except Exception:  # noqa: BLE001
            pass
        return 3
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
