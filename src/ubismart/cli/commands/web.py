"""Web 控制台命令。"""

from __future__ import annotations

import ipaddress
from pathlib import Path

import typer
from rich.markup import escape

from ubismart.cli.context import console, get_settings
from ubismart.cli.options import _WORKSPACE_OPTION
from ubismart.config import apply_security_profile
from ubismart.session.workspace_lock import WorkspaceLock


def _is_loopback_bind(host: str) -> bool:
    text = str(host or "").strip().lower()
    if text in {"localhost", "localhost."}:
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False

def web(
    host: str = typer.Option("127.0.0.1", "--host", help="监听地址"),
    port: int = typer.Option(8765, "--port", help="监听端口"),
    workspace: str = _WORKSPACE_OPTION,
    security_profile: str = typer.Option(
        "untrusted",
        "--security-profile",
        help="local | untrusted（Web 默认 untrusted：bwrap + 禁网 + ask）",
    ),
    ssl_keyfile: str = typer.Option("", "--ssl-keyfile", help="TLS 私钥路径"),
    ssl_certfile: str = typer.Option("", "--ssl-certfile", help="TLS 证书路径"),
    allow_remote: bool = typer.Option(
        False, "--allow-remote", help="允许非 loopback 监听（必须配置 token）"
    ),
    behind_proxy: bool = typer.Option(
        False, "--behind-proxy", help="TLS/认证由可信反向代理终止"
    ),
    allow_insecure_remote: bool = typer.Option(
        False,
        "--allow-insecure-remote",
        help="显式允许无 TLS 的远程监听（不推荐）",
    ),
    no_frontend: bool = typer.Option(
        False,
        "--no-frontend",
        help="只提供 API，不托管 /、/static、sw.js（独立静态站部署）",
    ),
) -> None:
    """启动 Web 界面。

    默认只监听 loopback 且使用不可信仓库 profile。公网部署必须配置
    `AGENT_WEB_AUTH_TOKENS`，并通过 TLS（本进程或反向代理）。
    """
    try:
        import uvicorn
    except ImportError as exc:
        hint = escape('pip install -e ".[web]"')
        console.print(f"[red]未安装 Web 依赖。请运行：{hint}[/]")
        raise typer.Exit(code=2) from exc

    from ubismart.web import create_app
    from ubismart.web.auth import AuthConfig

    settings = get_settings()
    updates: dict[str, object] = {"security_profile": security_profile}
    if workspace:
        updates["wsl_workspace"] = workspace
    if no_frontend:
        updates["web_serve_frontend"] = False
    settings = apply_security_profile(settings.model_copy(update=updates))

    try:
        auth = AuthConfig.from_settings(settings)
    except ValueError as exc:
        console.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(code=2) from exc

    if behind_proxy and not auth.enabled:
        console.print(
            "[red]--behind-proxy 必须配置 AGENT_WEB_AUTH_TOKENS。[/] "
            "否则反向代理转发来的请求会被当成 127.0.0.1 匿名访问。"
        )
        raise typer.Exit(code=2)

    loopback = _is_loopback_bind(host)
    if not loopback:
        if not allow_remote:
            console.print(
                "[red]拒绝监听非 loopback 地址。[/] "
                "公网部署请显式加 --allow-remote，并配置 AGENT_WEB_AUTH_TOKENS。"
            )
            raise typer.Exit(code=2)
        if not auth.enabled:
            console.print(
                "[red]远程监听必须配置 AGENT_WEB_AUTH_TOKENS"
                "（或 AGENT_WEB_AUTH_TOKENS_FILE）。[/]"
            )
            raise typer.Exit(code=2)
        has_tls = bool(ssl_keyfile and ssl_certfile)
        if not has_tls and not behind_proxy and not allow_insecure_remote:
            console.print(
                "[red]远程监听需要 TLS：[/] 提供 --ssl-keyfile/--ssl-certfile，"
                "或声明 --behind-proxy，或显式 --allow-insecure-remote。"
            )
            raise typer.Exit(code=2)

    if bool(ssl_keyfile) != bool(ssl_certfile):
        console.print("[red]--ssl-keyfile 与 --ssl-certfile 必须同时提供。[/]")
        raise typer.Exit(code=2)

    data_root = (
        Path(settings.web_data_root)
        if settings.web_data_root
        else Path.cwd() / ".agent" / "web" / "users"
    )
    lock = WorkspaceLock(data_root, f"web-server:{data_root.resolve()}")
    try:
        acquired = lock.acquire()
    except OSError as exc:
        console.print(f"[red]无法创建 Web 单进程锁：{escape(str(exc))}[/]")
        raise typer.Exit(code=2) from exc
    if not acquired:
        console.print(
            "[red]已有一个 Web 进程在使用同一数据目录。[/] "
            "当前 Web 按单进程共享资源池运行；请先停止另一个进程，"
            "或通过 AGENT_WEB_DATA_ROOT 指定不同目录。"
        )
        raise typer.Exit(code=2)

    mode = "Bearer Token" if auth.enabled else "loopback 匿名"
    console.print(
        f"[dim]打开 http://{host}:{port}（profile={security_profile}，"
        f"认证={mode}，Ctrl+C 停止）[/]"
    )
    try:
        uvicorn.run(
            create_app(
                settings,
                auth,
                hsts=bool(ssl_keyfile or behind_proxy),
                stream_buffer=True,
            ),
            host=host,
            port=port,
            log_level="warning",
            ssl_keyfile=ssl_keyfile or None,
            ssl_certfile=ssl_certfile or None,
            proxy_headers=behind_proxy,
        )
    finally:
        lock.release()
