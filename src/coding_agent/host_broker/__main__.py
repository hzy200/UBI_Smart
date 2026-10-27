"""A2L 客户端子进程入口。

随客户端启动，主动外连 A3；不监听端口、不注册系统服务。
Phase 1 只暴露只读能力；授权通过本地 stdin 确认（生产替换为托盘/面板）。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from coding_agent.host_broker.consent import (
    AutoConsent,
    RemoteConsent,
    StdinConsent,
    TkConsent,
)
from coding_agent.host_broker.lifecycle import BrokerLifecycle
from coding_agent.host_broker.op_journal import OpJournal
from coding_agent.host_broker.ops import HostReadOnlyOps
from coding_agent.host_broker.paths import HostPathError, HostPathPolicy
from coding_agent.host_broker.policy import GrantStore, HostPolicy
from coding_agent.host_broker.server import HostBrokerServer
from coding_agent.host_broker.store import (
    WorkspaceRootStore,
    default_store_path,
)
from coding_agent.host_broker.toolchain import ToolchainRegistry
from coding_agent.host_broker.transport import connect_tcp
from coding_agent.host_broker.workspace_registry import TkWorkspaceRegistrar


def _args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CodingAgent A2L host broker")
    parser.add_argument("--url", required=True, help="tcp://127.0.0.1:8791")
    parser.add_argument("--workspace-root", required=True)
    parser.add_argument("--workspace-id", default="ws-local")
    parser.add_argument("--root-handle", default="h-local")
    parser.add_argument(
        "--security-tier", default="lowpriv", choices=["lowpriv", "user-t1"]
    )
    parser.add_argument("--broker-id", default="a2l-local")
    parser.add_argument("--journal", default="")
    parser.add_argument(
        "--workspace-store",
        default="",
        help="本地 workspace_id ↔ root 映射 SQLite；默认在 LOCALAPPDATA",
    )
    parser.add_argument(
        "--consent",
        default="tk",
        choices=["tk", "stdin", "auto"],
        help="本地同意方式；auto 仅测试/演示",
    )
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="兼容旧参数；等价于 --consent auto",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    path_policy = HostPathPolicy()
    try:
        root_info = path_policy.validate_root(args.workspace_root)
    except HostPathError as exc:
        print(f"A2L 根目录不合法：{exc}", file=sys.stderr)
        return 2
    root = root_info.root
    policy = HostPolicy.from_settings(
        type(
            "S",
            (),
            {
                "host_deny_roots": "",
                "host_sensitive_roots": "",
                "host_root_policy": "local_ntfs_only",
            },
        )()
    )
    grants = GrantStore()
    journal_path = (
        Path(args.journal)
        if args.journal
        else Path.home() / ".coding-agent" / "a2l-ops.sqlite3"
    )
    journal = OpJournal(journal_path)
    mode = "auto" if args.auto_approve else args.consent
    if mode == "auto":
        consent = AutoConsent(True)
        print(
            "警告：auto 同意仅用于测试，生产必须使用本地 UI",
            file=sys.stderr,
        )
    elif mode == "stdin":
        consent = StdinConsent()
    elif (
        os.environ.get("AGENT_CONSENT_URL", "").strip()
        and os.environ.get("AGENT_CONSENT_TOKEN", "").strip()
    ):
        consent = RemoteConsent(
            os.environ["AGENT_CONSENT_URL"],
            os.environ["AGENT_CONSENT_TOKEN"],
        )
    else:
        consent = TkConsent()
    store: WorkspaceRootStore | None
    try:
        store = WorkspaceRootStore(
            args.workspace_store or default_store_path()
        )
    except Exception as exc:  # noqa: BLE001 - 本次仍可内存注册
        print(f"A2L 工作区映射库不可用：{exc}", file=sys.stderr)
        store = None
    roots: dict[str, str] = {}
    if store is not None:
        for workspace_id, path in store.load().items():
            try:
                roots[workspace_id] = path_policy.validate_root(path).root
            except HostPathError as exc:
                # 盘符/权限在两次启动之间可能变化；坏映射不阻断启动。
                print(
                    f"A2L 忽略失效的工作区映射 {workspace_id}：{exc}",
                    file=sys.stderr,
                )
    # 启动参数里的默认根始终可用，并覆盖同 id 的旧映射。
    roots[args.workspace_id] = root
    if store is not None:
        try:
            store.upsert(
                args.workspace_id,
                root,
                display_name=Path(root).name or "host",
                root_handle=args.root_handle,
                a2l_id=args.broker_id,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"A2L 工作区映射保存失败：{exc}", file=sys.stderr)
    registrar = TkWorkspaceRegistrar(
        roots,
        a2l_id=args.broker_id,
        path_policy=path_policy,
        store=store,
    )
    ops = HostReadOnlyOps(
        policy=policy,
        path_policy=path_policy,
        grants=grants,
        toolchains=ToolchainRegistry(),
        workspace_roots=roots,
        broker_id=args.broker_id,
        security_tier=args.security_tier,
        threat_model="T1",
        workspace_registrar=registrar,
    )
    lifecycle = BrokerLifecycle(
        client_id=args.workspace_id,
    )
    if not lifecycle.start():
        print("A2L 已有实例在运行", file=sys.stderr)
        return 3
    try:
        last_error: Exception | None = None
        transport = None
        for _ in range(20):
            try:
                transport = await connect_tcp(args.url)
                break
            except OSError as exc:
                last_error = exc
                await asyncio.sleep(0.5)
        if transport is None:
            raise last_error or ConnectionError("无法连接 A3")
        print(f"A2L connected to {args.url}", file=sys.stderr, flush=True)
        server = HostBrokerServer(
            transport,
            broker_id=args.broker_id,
            security_tier=args.security_tier,
            threat_model="T1",
            handler=ops.handle,
            journal=journal,
            lifecycle=lifecycle,
            grants=grants,
            consent=consent,
        )
        await server.run()
    finally:
        lifecycle.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
