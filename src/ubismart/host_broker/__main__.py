"""A2L 客户端子进程入口。

随客户端启动，主动外连 A3；不监听端口、不注册系统服务。
Phase 1 只暴露只读能力；授权通过本地 stdin 确认（生产替换为托盘/面板）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from coding_agent.host_broker.attestation import (
    AttestationUnavailable,
    build_device_identity,
)
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
from coding_agent.host_broker.policy_doc import (
    PolicyDocument,
    PolicyError,
    load_signed_policy,
)
from coding_agent.host_broker.server import (
    EXEC_REQUEST_OPS,
    INSTALL_REQUEST_OPS,
    PHASE1_REQUEST_OPS,
    WRITE_REQUEST_OPS,
    HostBrokerServer,
)
from coding_agent.host_broker.snapshots import HostSnapshotStore
from coding_agent.host_broker.store import (
    WorkspaceRootStore,
    default_store_path,
)
from coding_agent.host_broker.toolchain import (
    ToolchainRef,
    ToolchainRegistry,
)
from coding_agent.host_broker.transport import (
    TlsConfig,
    certificate_fingerprint,
    connect_tcp,
)
from coding_agent.host_broker.win_isolation import (
    AppContainerRunner,
    IsolationError,
    IsolationReport,
    JobLimits,
    JobObject,
    set_low_integrity,
)
from coding_agent.host_broker.workspace_registry import TkWorkspaceRegistrar


def _load_toolchains(items: list[str]) -> ToolchainRegistry:
    refs: list[ToolchainRef] = []
    for raw in items:
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise SystemExit(f"--toolchain-json 不是合法 JSON：{exc}") from exc
        if not isinstance(data, dict):
            raise SystemExit("--toolchain-json 必须是对象")
        try:
            refs.append(
                ToolchainRef(
                    id=str(data.get("id") or ""),
                    kind=str(data.get("kind") or ""),
                    version=str(data.get("version") or ""),
                    root=str(data.get("root") or ""),
                    commands=tuple(
                        str(item) for item in data.get("commands") or ()
                    ),
                    output_roots=tuple(
                        str(item) for item in data.get("output_roots") or ()
                    ),
                )
            )
        except ValueError as exc:
            raise SystemExit(f"工具链注册失败：{exc}") from exc
    try:
        return ToolchainRegistry(refs)
    except ValueError as exc:
        raise SystemExit(f"工具链注册失败：{exc}") from exc


def _apply_policy(
    args: argparse.Namespace,
) -> PolicyDocument | None:
    if not args.policy_file:
        return None
    source = args.policy_pubkey_file or args.policy_pubkey
    if not source:
        raise PolicyError(
            "配置了策略文件但未提供公钥（--policy-pubkey[-file]）"
        )
    document = load_signed_policy(args.policy_file, source)
    document.check(a2l_id=args.broker_id)
    if document.allow_write is not None:
        args.allow_write = document.allow_write
    if document.allow_exec is not None:
        args.allow_exec = document.allow_exec
    if document.job_max_processes is not None:
        args.job_max_processes = document.job_max_processes
    if document.job_memory_mb is not None:
        args.job_memory_mb = document.job_memory_mb
    if document.attestation is not None:
        args.attestation = document.attestation
    if document.toolchains:
        args.toolchain_json = [
            json.dumps(item, ensure_ascii=False)
            for item in document.toolchains
        ]
    return document


def _args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CodingAgent A2L host broker")
    parser.add_argument("--url", required=True, help="tcp://127.0.0.1:8791")
    parser.add_argument("--workspace-root", required=True)
    parser.add_argument("--workspace-id", default="ws-local")
    parser.add_argument("--root-handle", default="h-local")
    parser.add_argument(
        "--security-tier",
        default="lowpriv",
        choices=["lowpriv", "appcontainer", "user-t1"],
    )
    parser.add_argument("--job-max-processes", type=int, default=32)
    parser.add_argument("--job-memory-mb", type=int, default=2048)
    parser.add_argument(
        "--low-integrity",
        action="store_true",
        help="把 A2L 进程降到低完整性（只读场景推荐）",
    )
    parser.add_argument(
        "--allow-write",
        action="store_true",
        help="开放 host 写/删除/快照（仍需 grant）",
    )
    parser.add_argument(
        "--allow-exec",
        action="store_true",
        help="开放 host.exec（仅 AppContainer 档且需注册工具链）",
    )
    parser.add_argument(
        "--toolchain-json",
        action="append",
        default=[],
        help=(
            "显式注册工具链 JSON，可重复；例："
            '{"id":"python-3.11","kind":"python","root":"C:/Python311",'
            '"commands":["python"],"output_roots":["build"]}'
        ),
    )
    parser.add_argument(
        "--snapshot-dir",
        default=os.environ.get("AGENT_HOST_SNAPSHOT_DIR", ""),
    )
    parser.add_argument(
        "--policy-file",
        default=os.environ.get("AGENT_HOST_BROKER_POLICY_FILE", ""),
    )
    parser.add_argument(
        "--policy-pubkey",
        default=os.environ.get("AGENT_HOST_BROKER_POLICY_PUBKEY", ""),
    )
    parser.add_argument(
        "--policy-pubkey-file",
        default=os.environ.get(
            "AGENT_HOST_BROKER_POLICY_PUBKEY_FILE", ""
        ),
    )
    parser.add_argument(
        "--attestation",
        default=os.environ.get("AGENT_HOST_BROKER_ATTESTATION", "off"),
        choices=["off", "auto", "require", "tpm"],
    )
    parser.add_argument(
        "--attestation-key",
        default=os.environ.get("AGENT_HOST_BROKER_ATTESTATION_KEY", ""),
    )
    parser.add_argument(
        "--print-device-identity",
        action="store_true",
        help="打印设备公钥（供管理员预登记）后退出",
    )
    parser.add_argument("--broker-id", default="a2l-local")
    parser.add_argument(
        "--tls-ca", default=os.environ.get("AGENT_HOST_BROKER_TLS_CA", "")
    )
    parser.add_argument(
        "--tls-cert",
        default=os.environ.get("AGENT_HOST_BROKER_DEVICE_CERT", ""),
    )
    parser.add_argument(
        "--tls-key",
        default=os.environ.get("AGENT_HOST_BROKER_DEVICE_KEY", ""),
    )
    parser.add_argument(
        "--tls-server-name",
        default=os.environ.get("AGENT_HOST_BROKER_TLS_SERVER_NAME", ""),
    )
    parser.add_argument("--require-tls", action="store_true")
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


class _SetupError(RuntimeError):
    def __init__(self, code: int, message: str = "") -> None:
        super().__init__(message)
        self.code = code


def _prepare_identity(
    args: argparse.Namespace,
) -> tuple[Any | None, int | None]:
    """策略 + 设备证明 + 可选的公钥打印；返回 (identity, exit_code)。"""
    try:
        _apply_policy(args)
    except PolicyError as exc:
        print(f"A2L 策略校验失败：{exc}", file=sys.stderr)
        return None, 2
    identity = None
    if str(args.attestation or "off").lower() != "off":
        try:
            identity = build_device_identity(
                args.attestation,
                a2l_id=args.broker_id,
                key_path=args.attestation_key or None,
            )
        except AttestationUnavailable as exc:
            print(
                f"A2L 设备证明初始化失败：{exc}", file=sys.stderr
            )
            return None, 2
    if args.print_device_identity:
        if identity is None:
            print(
                "未启用设备证明（--attestation off）", file=sys.stderr
            )
            return None, 2
        print(
            json.dumps(
                {
                    "a2l_id": args.broker_id,
                    "kind": identity.kind,
                    "public_key": identity.public_key(),
                },
                ensure_ascii=False,
            )
        )
        return None, 0
    return identity, None


def _build_workspaces(
    args: argparse.Namespace, path_policy: HostPathPolicy
) -> tuple[str, dict[str, str], TkWorkspaceRegistrar]:
    try:
        root_info = path_policy.validate_root(args.workspace_root)
    except HostPathError as exc:
        raise _SetupError(2, f"A2L 根目录不合法：{exc}") from exc
    root = root_info.root
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
                print(
                    f"A2L 忽略失效的工作区映射 {workspace_id}：{exc}",
                    file=sys.stderr,
                )
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
    return root, roots, registrar


def _build_isolation(
    args: argparse.Namespace,
    roots: dict[str, str],
    toolchains: Any,
    path_policy: HostPathPolicy,
) -> tuple[Any | None, Any | None, IsolationReport]:
    job: JobObject | None = None
    reader: AppContainerRunner | None = None
    report = IsolationReport(security_tier=args.security_tier)
    if args.security_tier not in {"lowpriv", "appcontainer"}:
        print(
            "警告：user-t1 仅提供 T1 防误操作，不宣称 T2",
            file=sys.stderr,
        )
        return None, None, report
    try:
        job = JobObject(
            JobLimits(
                max_processes=args.job_max_processes,
                job_memory_mb=args.job_memory_mb,
            )
        )
        job.assign()
        low_integrity = False
        if args.low_integrity:
            low_integrity = set_low_integrity()
            if not low_integrity:
                raise IsolationError("低完整性设置失败")
        if args.security_tier == "appcontainer":
            write_roots: list[str] = []
            for workspace_root in roots.values():
                for ref in toolchains:
                    for relative in ref.output_roots:
                        try:
                            target = path_policy.resolve_child(
                                workspace_root, relative
                            )
                        except HostPathError as exc:
                            raise IsolationError(
                                f"工具链 output_root 不合法：{exc}"
                            ) from exc
                        os.makedirs(target, exist_ok=True)
                        write_roots.append(target)
            reader = AppContainerRunner(
                list(roots.values()),
                exec_roots=toolchains.exec_roots(),
                write_roots=write_roots,
            )
        report = IsolationReport(
            job_object=True,
            low_integrity=low_integrity,
            appcontainer=reader is not None,
            security_tier=args.security_tier,
        )
    except IsolationError as exc:
        if reader is not None:
            reader.close()
        if job is not None:
            job.close()
        raise _SetupError(
            2, f"A2L 隔离初始化失败：{exc}"
        ) from exc
    return job, reader, report


async def _run(args: argparse.Namespace) -> int:
    device_identity, exit_code = _prepare_identity(args)
    if exit_code is not None:
        return exit_code
    path_policy = HostPathPolicy()
    try:
        _root, roots, registrar = _build_workspaces(
            args, path_policy
        )
    except _SetupError as exc:
        print(str(exc), file=sys.stderr)
        return exc.code
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
    toolchains = _load_toolchains(args.toolchain_json)
    snapshots = (
        HostSnapshotStore(
            args.snapshot_dir or None, path_policy=path_policy
        )
        if args.allow_write
        else None
    )
    try:
        job, reader, report = _build_isolation(
            args, roots, toolchains, path_policy
        )
    except _SetupError as exc:
        print(str(exc), file=sys.stderr)
        return exc.code
    ops = HostReadOnlyOps(
        policy=policy,
        path_policy=path_policy,
        grants=grants,
        toolchains=toolchains,
        workspace_roots=roots,
        broker_id=args.broker_id,
        security_tier=args.security_tier,
        threat_model="T1",
        workspace_registrar=registrar,
        appcontainer=reader,
        isolation=report.as_dict(),
        snapshots=snapshots,
        allow_write=args.allow_write,
        allow_exec=args.allow_exec,
    )
    lifecycle = BrokerLifecycle(
        client_id=args.workspace_id,
    )
    if not lifecycle.start():
        print("A2L 已有实例在运行", file=sys.stderr)
        return 3
    tls = None
    device_fingerprint = ""
    if args.tls_cert or args.tls_key:
        if not (args.tls_cert and args.tls_key):
            print("TLS 证书与私钥必须同时提供", file=sys.stderr)
            return 2
        tls = TlsConfig(
            ca=args.tls_ca,
            cert=args.tls_cert,
            key=args.tls_key,
            server_name=args.tls_server_name,
        )
        device_fingerprint = certificate_fingerprint(args.tls_cert)
    elif args.require_tls:
        print("--require-tls 需要 --tls-cert/--tls-key", file=sys.stderr)
        return 2
    else:
        print(
            "警告：A2L 未启用 TLS；仅建议 loopback 测试环境使用",
            file=sys.stderr,
        )
    try:
        last_error: Exception | None = None
        transport = None
        for _ in range(20):
            try:
                transport = await connect_tcp(args.url, tls=tls)
                break
            except OSError as exc:
                last_error = exc
                await asyncio.sleep(0.5)
        if transport is None:
            raise last_error or ConnectionError("无法连接 A3")
        print(f"A2L connected to {args.url}", file=sys.stderr, flush=True)
        allowed_ops = set(PHASE1_REQUEST_OPS) | set(INSTALL_REQUEST_OPS)
        if args.allow_write:
            allowed_ops |= set(WRITE_REQUEST_OPS)
        if args.allow_exec:
            allowed_ops |= set(EXEC_REQUEST_OPS)
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
            device_fingerprint=device_fingerprint,
            require_encrypted=bool(tls) or args.require_tls,
            allowed_ops=frozenset(allowed_ops),
            device_identity=device_identity,
        )
        await server.run()
    finally:
        journal.close()
        if reader is not None:
            reader.close()
        # job.close() 会随最后一个句柄关闭杀掉本进程，必须最后执行。
        lifecycle.stop()
        if job is not None:
            job.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
