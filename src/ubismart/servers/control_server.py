"""A2 控制面原生进程（Phase B 打包）。

浏览器/桌面壳只连这里；运行请求再经 `AGENT_INTERNAL_URL` 转到 Agent Server。
默认只绑 loopback；非 loopback 必须配置 Web 认证 token。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ubismart.config import get_settings


def _internal_url_is_remote(settings: Any) -> bool:
    """A3 是否在另一台机器上（A2→A3 跨机 = context 必须验签）。"""
    url = str(settings.agent_internal_url or "").strip()
    if not url:
        return False
    host = (urlsplit(url).hostname or "").strip().lower()
    return bool(host) and host not in {"127.0.0.1", "::1", "localhost"}


def _handshake(settings: Any) -> tuple[Any | None, dict[str, Any]]:
    """启动时探测 A3；失败不阻断 A2，状态交给 /healthz 与前端。"""
    if not str(settings.agent_internal_url or "").strip():
        return None, {"state": "in_process"}

    from ubismart.agent_protocol import check_compatibility
    from ubismart.control.agent_client import HttpAgentClient

    client = HttpAgentClient(
        settings.agent_internal_url,
        settings.agent_internal_token,
        timeout=5.0,
        hmac_key=getattr(settings, "agent_internal_hmac_key", ""),
    )
    try:
        meta = asyncio.run(client.probe())
    except Exception as exc:  # noqa: BLE001 - A3 未就绪是预期状态
        return client, {
            "state": "unavailable",
            "error": f"{type(exc).__name__}: {exc}",
        }
    compat = check_compatibility(meta)
    return client, {
        "state": "ok" if compat.ok else "incompatible",
        "meta": meta.to_dict(),
        "reason": compat.reason,
        "warning": compat.warning,
    }


def _migrate_control_db(settings: Any) -> int:
    """把控制面数据库升级到 head；返回进程退出码。"""
    url = str(settings.control_db_url or "").strip()
    if not url:
        print(
            json.dumps(
                {
                    "event": "control_db_migrate_skipped",
                    "reason": "AGENT_CONTROL_DB_URL 为空",
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 0

    from ubismart.control.migrate import upgrade

    backup_dir = os.environ.get("AGENT_CONTROL_BACKUP_DIR", "").strip()
    try:
        result = upgrade(
            url,
            backup=True,
            backup_dir=(
                Path(backup_dir)
                if backup_dir
                else Path(settings.resolved_audit_dir).parent / "backups"
            ),
        )
    except Exception as exc:  # noqa: BLE001 - CLI 要可读
        print(
            json.dumps(
                {
                    "event": "control_db_migrate_failed",
                    "error": f"{type(exc).__name__}: {exc}",
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
            flush=True,
        )
        return 1
    print(
        json.dumps(
            {"event": "control_db_migrated", **result},
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0


def _require(ok: bool, message: str) -> None:
    """启动期硬校验：不满足就退出（码 2），不做"先跑起来再说"的降级。"""
    if ok:
        return
    print(message, file=sys.stderr)
    raise SystemExit(2)


def _validate_startup(
    settings: Any,
    args: argparse.Namespace,
    auth: Any,
    *,
    loopback: bool,
    token: str,
) -> None:
    """把所有"启动即拒绝"的判据收在一处，改动时只改这一个地方。

    这些不是可恢复的配置错误：把界面暴露在非 loopback 而没认证、或让 A2→A3
    链路裸奔，都属于"跑起来就是错"，所以统一 `SystemExit(2)`。
    """
    _require(not (args.desktop and not token), "--desktop 必须配合 --token-stdin")
    _require(
        not (args.desktop and args.multi_user),
        "--desktop 与 --multi-user 不能同时使用",
    )
    _require(
        not (args.multi_user and not auth.enabled),
        "--multi-user 必须同时配置 Web 认证 token",
    )
    _require(
        loopback or auth.enabled,
        "非 loopback 绑定必须配置 AGENT_WEB_AUTH_TOKENS 或 "
        "AGENT_WEB_AUTH_TOKENS_FILE",
    )
    # 账号登录走 Cookie；非 loopback 却没开 Secure 时，浏览器可能把会话
    # Cookie 发到明文 HTTP 上。与其依赖部署人员记得配置，不如启动即拒绝。
    _require(
        loopback
        or not bool(getattr(settings, "web_accounts_enabled", False))
        or bool(getattr(settings, "web_cookie_secure", False)),
        "非 loopback 绑定且启用账号 Cookie 登录时必须设置 "
        "AGENT_WEB_COOKIE_SECURE=true",
    )
    # A2→A3 跨机时，链路上能改 X-Agent-Context 的不止本机：没有 HMAC 就等于
    # 把 tenant / workspace / a2l_id 的决定权交给了中间人。
    _require(
        not _internal_url_is_remote(settings)
        or bool(str(settings.agent_internal_hmac_key or "").strip()),
        "AGENT_INTERNAL_URL 指向非 loopback 主机时必须配置 "
        "AGENT_INTERNAL_HMAC_KEY",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="UBISmart Control Plane")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8780)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--multi-user", action="store_true")
    parser.add_argument(
        "--desktop",
        action="store_true",
        help="桌面模式：启用桌面路由并强制单用户",
    )
    parser.add_argument(
        "--token-stdin",
        action="store_true",
        help="从 stdin 读取前端访问 token（避免出现在进程列表）",
    )
    parser.add_argument(
        "--migrate",
        action="store_true",
        help="启动前把控制面数据库升级到 head（迁移前自动备份）",
    )
    parser.add_argument(
        "--migrate-only",
        action="store_true",
        help="只执行迁移后退出（供 systemd ExecStartPre 使用）",
    )
    parser.add_argument("--ssl-keyfile", default="")
    parser.add_argument("--ssl-certfile", default="")
    parser.add_argument("--log-level", default="warning")
    parser.add_argument(
        "--backup",
        action="store_true",
        help="只备份控制面数据库后退出（供 systemd timer 使用）",
    )
    parser.add_argument("--backup-out", default="")
    parser.add_argument("--backup-keep", type=int, default=14)
    args = parser.parse_args()

    settings = get_settings()
    from ubismart.platform.observability import configure_logging

    configure_logging(
        level=args.log_level,
        fmt=str(getattr(settings, "log_format", "text") or "text"),
    )
    if args.backup:
        url = str(settings.control_db_url or "").strip()
        if not url:
            print(
                "未配置 AGENT_CONTROL_DB_URL，跳过备份",
                file=sys.stderr,
            )
            return 2
        from ubismart.control.backup import backup

        try:
            result = backup(
                url,
                args.backup_out or None,
                keep=max(0, int(args.backup_keep)),
            )
        except Exception as exc:  # noqa: BLE001 - CLI 要可读
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            return 1
        print(
            json.dumps(
                {
                    "ok": True,
                    "path": result.path,
                    "kind": result.kind,
                    "bytes": result.bytes,
                },
                ensure_ascii=False,
            )
        )
        return 0
    if args.migrate or args.migrate_only:
        code = _migrate_control_db(settings)
        if args.migrate_only:
            return code

    # 只有真正启动 HTTP 服务时才加载 web/runtime 栈；--migrate-only 不需要。
    import uvicorn

    from ubismart.web import create_app
    from ubismart.web.auth import AuthConfig

    token = sys.stdin.readline().strip() if args.token_stdin else ""
    auth = (
        AuthConfig(tokens={token: "local"}, allow_anonymous_local=False)
        if token
        else AuthConfig.from_settings(settings)
    )
    loopback = args.host in {"127.0.0.1", "::1", "localhost"}
    _validate_startup(settings, args, auth, loopback=loopback, token=token)

    agent_client, agent_status = _handshake(settings)
    app = create_app(
        settings,
        auth,
        allow_write=args.write,
        multi_user=False
        if args.desktop
        else (args.multi_user if args.multi_user else None),
        stream_buffer=True,
        desktop_mode=args.desktop,
        client_source="desktop" if args.desktop else "web",
        agent_client=agent_client,
    )
    app.state.agent_status = agent_status
    if agent_status["state"] not in {"ok", "in_process"}:
        print(
            json.dumps(
                {"event": "agent_handshake_failed", **agent_status},
                ensure_ascii=False,
            ),
            file=sys.stderr,
            flush=True,
        )
    print(
        json.dumps(
            {
                "event": "coding_agent_control_plane",
                "host": args.host,
                "port": args.port,
                "agent_internal": bool(settings.agent_internal_url),
                "agent_state": agent_status["state"],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    drain_timeout = int(getattr(settings, "drain_timeout_s", 60) or 0)
    config = uvicorn.Config(
        app,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        access_log=False,
        # SIGTERM 后先停止接新连接，再给在途请求（含 SSE 收尾）留出
        # 与 A3 相同的等待窗口；0 = 立即关闭。
        timeout_graceful_shutdown=drain_timeout or None,
        ssl_keyfile=args.ssl_keyfile or None,
        ssl_certfile=args.ssl_certfile or None,
    )
    server = uvicorn.Server(config)
    app.state.shutdown = lambda: setattr(server, "should_exit", True)
    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
