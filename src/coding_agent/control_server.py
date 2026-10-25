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

from coding_agent.config import get_settings


def _handshake(settings: Any) -> tuple[Any | None, dict[str, Any]]:
    """启动时探测 A3；失败不阻断 A2，状态交给 /healthz 与前端。"""
    if not str(settings.agent_internal_url or "").strip():
        return None, {"state": "in_process"}

    from coding_agent.agent_protocol import check_compatibility
    from coding_agent.control.agent_client import HttpAgentClient

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

    from coding_agent.control.migrate import upgrade

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


def main() -> int:
    parser = argparse.ArgumentParser(description="CodingAgent Control Plane")
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
    args = parser.parse_args()

    settings = get_settings()
    if args.migrate or args.migrate_only:
        code = _migrate_control_db(settings)
        if args.migrate_only:
            return code

    # 只有真正启动 HTTP 服务时才加载 web/runtime 栈；--migrate-only 不需要。
    import uvicorn

    from coding_agent.web import create_app
    from coding_agent.web.auth import AuthConfig

    token = sys.stdin.readline().strip() if args.token_stdin else ""
    if args.desktop and not token:
        print("--desktop 必须配合 --token-stdin", file=sys.stderr)
        raise SystemExit(2)
    if args.desktop and args.multi_user:
        print("--desktop 与 --multi-user 不能同时使用", file=sys.stderr)
        raise SystemExit(2)
    auth = (
        AuthConfig(tokens={token: "local"}, allow_anonymous_local=False)
        if token
        else AuthConfig.from_settings(settings)
    )
    loopback = args.host in {"127.0.0.1", "::1", "localhost"}
    if args.multi_user and not auth.enabled:
        print("--multi-user 必须同时配置 Web 认证 token", file=sys.stderr)
        raise SystemExit(2)
    if not loopback and not auth.enabled:
        print(
            "非 loopback 绑定必须配置 AGENT_WEB_AUTH_TOKENS 或 "
            "AGENT_WEB_AUTH_TOKENS_FILE",
            file=sys.stderr,
        )
        raise SystemExit(2)

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
    config = uvicorn.Config(
        app,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        access_log=False,
        ssl_keyfile=args.ssl_keyfile or None,
        ssl_certfile=args.ssl_certfile or None,
    )
    server = uvicorn.Server(config)
    app.state.shutdown = lambda: setattr(server, "should_exit", True)
    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
