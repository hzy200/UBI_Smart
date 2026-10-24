"""WSL/Linux 内运行的 Agent Server（Phase 0）。

Windows 只负责打开浏览器/WebView 并传入 token；AgentRuntime、工具、沙箱、
审计、checkpoint 都在这个进程里。默认只绑 loopback。
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import sys

import uvicorn

from coding_agent.access import ACCESS_READ_ONLY
from coding_agent.config import get_settings
from coding_agent.web import create_app
from coding_agent.web.auth import AuthConfig


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CodingAgent WSL/Linux server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--workspace", default="", help="WSL 内工作区绝对路径")
    parser.add_argument("--write", action="store_true")
    parser.add_argument(
        "--token",
        default="",
        help="访问 token；不传则读 AGENT_SERVER_TOKEN 或随机生成",
    )
    parser.add_argument(
        "--token-stdin",
        action="store_true",
        help="从 stdin 读取 token（避免出现在进程列表）",
    )
    parser.add_argument("--log-level", default="warning")
    return parser


def _free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def main() -> None:
    args = _parser().parse_args()
    settings = get_settings()
    if args.workspace:
        settings = settings.model_copy(
            update={"wsl_workspace": args.workspace}
        )
    # 在 Linux/WSL 内 auto 会选择 local 执行后端；在 Windows 上保留 auto，
    # 避免误启动 server 时把 /bin/bash 当成可用执行器。
    if os.name != "nt":
        settings = settings.model_copy(update={"execution_backend": "local"})

    token = args.token or os.environ.get("AGENT_SERVER_TOKEN", "")
    if not token and args.token_stdin:
        token = sys.stdin.readline().strip()
    token = token or secrets.token_urlsafe(32)
    auth = AuthConfig(
        tokens={token: "local"},
        allow_anonymous_local=False,
    )
    host = args.host
    port = int(args.port)
    if port == 0:
        port = _free_port(host)
    app = create_app(
        settings,
        auth,
        allow_write=args.write,
        multi_user=False,
        default_access_mode=ACCESS_READ_ONLY,
        stream_buffer=True,
        desktop_mode=True,
        client_source="desktop",
    )
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level=args.log_level,
        access_log=False,
    )
    server = uvicorn.Server(config)
    # 桌面关窗时由 launcher 调用，避免直接 kill wsl.exe 留下孤儿进程。
    app.state.shutdown = lambda: setattr(server, "should_exit", True)
    print(
        json.dumps(
            {
                "event": "coding_agent_server",
                "host": host,
                "port": port,
                "workspace": settings.wsl_workspace,
                "version": "0.1.0",
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    server.run()


if __name__ == "__main__":
    main()
