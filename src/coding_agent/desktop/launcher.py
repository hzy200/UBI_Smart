"""Windows 桌面启动器：配置向导 → 启动 WSL A3 + Windows A2 → 打开 WebView。"""

from __future__ import annotations

import argparse
import html
import logging
import os
import threading
import time

from coding_agent.desktop.api import DesktopApi
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
    start_agent,
    start_control,
)
from coding_agent.desktop.watchdog import WatchTarget, supervise_many
from coding_agent.desktop.wsl import distro_exists, import_distro, to_wsl_path
from coding_agent.workspace_lock import WorkspaceLock

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
    parser.add_argument("--write", action="store_true", help="允许会话选择写模式")
    return parser


def _open_window(
    control: ControlServer, agent: AgentServer, config: LauncherConfig
) -> None:
    try:
        import webview
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            '未安装桌面依赖。请运行：pip install -e ".[desktop]"'
        ) from exc

    window = webview.create_window(
        "CodingAgent",
        control.url,
        width=1440,
        height=920,
        min_size=(980, 620),
        confirm_close=True,
        js_api=DesktopApi(config),
    )

    holder = {"agent": agent, "control": control}
    stop_event = threading.Event()
    lock = threading.Lock()

    def stop_all() -> None:
        with lock:
            current_control = holder["control"]
            current_agent = holder["agent"]
        for name, current in (
            ("控制面", current_control),
            ("Agent Server", current_agent),
        ):
            try:
                current.stop()
            except Exception:  # noqa: BLE001 - 关窗不能被清理失败卡死
                _LOG.exception("停止%s失败", name)

    def on_closing() -> bool:
        stop_event.set()
        stop_all()
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
    webview.start()


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
    if changed:
        config.save()

    lock = WorkspaceLock(state_dir(), "desktop-launcher")
    if not lock.acquire():
        _LOG.error("已有 CodingAgent 实例在运行")
        return 2
    try:
        agent = start_agent(config, log_path=logs_dir() / "agent.log")
        _LOG.info("agent server started on %s", agent.port)
        try:
            control = start_control(
                config,
                agent_port=agent.port,
                agent_token=agent.token,
                log_path=logs_dir() / "control.log",
            )
        except Exception:
            agent.stop()
            raise
        _LOG.info("control plane started on %s", control.port)
        _open_window(control, agent, config)
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
            webview.start()
        except Exception:  # noqa: BLE001
            pass
        return 3
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
