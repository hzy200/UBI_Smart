"""Windows 真机 A2L 只读 e2e：TCP loopback + 真实文件 + 工具层。"""

from __future__ import annotations

import asyncio
import os
import threading
import time

import pytest

from coding_agent.config import Settings
from coding_agent.execution.contracts import ExecutionRequest
from coding_agent.execution.host_backend import HostExecutionBackend
from coding_agent.execution.host_file_access import HostFileAccess
from coding_agent.execution.service import ExecutionService
from coding_agent.host_broker.acceptor import HostBrokerAcceptor
from coding_agent.host_broker.consent import AutoConsent
from coding_agent.host_broker.lifecycle import BrokerLifecycle
from coding_agent.host_broker.op_journal import OpJournal
from coding_agent.host_broker.ops import HostReadOnlyOps
from coding_agent.host_broker.paths import HostPathPolicy
from coding_agent.host_broker.policy import GrantStore, HostPolicy
from coding_agent.host_broker.server import HostBrokerServer
from coding_agent.host_broker.sync_client import SyncHostCaller
from coding_agent.host_broker.toolchain import ToolchainRef, ToolchainRegistry
from coding_agent.host_broker.transport import connect_tcp, start_tcp_server
from coding_agent.sandbox.wsl_exec import ExecResult
from coding_agent.tools.artifacts import unpack
from coding_agent.tools.files import (
    LIST_TOOL_NAME,
    READ_TOOL_NAME,
    build_file_tools,
)
from coding_agent.tools.search import FIND_FILES, build_search_tools

pytestmark = [
    pytest.mark.host,
    pytest.mark.skipif(os.name != "nt", reason="Windows real-machine e2e"),
]


class FakeSandbox:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def run(self, command, *, cwd=None, timeout=None, capture_limit=None):
        return ExecResult(
            command=command,
            exit_code=1,
            stdout="",
            stderr="",
            duration_ms=0,
        )


class NoopBackend:
    def capabilities(self):
        return {"backend": "noop", "workspace": "/ws"}


class BrokerThread:
    def __init__(self, ops, lifecycle, journal) -> None:
        self.ops = ops
        self.lifecycle = lifecycle
        self.journal = journal
        self.loop = asyncio.new_event_loop()
        self.port = 0
        self._ready = threading.Event()
        self._server = None
        self._active: list[HostBrokerServer] = []
        self._thread = threading.Thread(
            target=self._run, name="a2l-e2e", daemon=True
        )

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._start())
        self.loop.run_forever()

    async def _start(self) -> None:
        async def handler(transport) -> None:
            server = HostBrokerServer(
                transport,
                broker_id="b-e2e",
                handler=self.ops.handle,
                journal=self.journal,
                lifecycle=self.lifecycle,
            )
            self._active.append(server)
            try:
                await server.run()
            finally:
                if server in self._active:
                    self._active.remove(server)

        self._server, self.port = await start_tcp_server(handler)
        self._ready.set()

    def start(self) -> None:
        self._thread.start()
        assert self._ready.wait(5), "A2L TCP server 未就绪"

    def stop(self) -> None:
        async def _stop() -> None:
            for server in list(self._active):
                await server.shutdown()
            if self._server is not None:
                self._server.close()
                await self._server.wait_closed()

        try:
            asyncio.run_coroutine_threadsafe(_stop(), self.loop).result(5)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self._thread.join(5)
            self.loop.close()


class A2LServerThread:
    """模拟 A2L 子进程：主动连出，然后跑 HostBrokerServer。"""

    def __init__(self, url, ops, grants, journal) -> None:
        self.url = url
        self.ops = ops
        self.grants = grants
        self.journal = journal
        self.loop = asyncio.new_event_loop()
        self.server = None
        self._thread = threading.Thread(
            target=self._run, name="a2l-server", daemon=True
        )

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._start())
        self.loop.run_forever()

    async def _start(self) -> None:
        transport = await connect_tcp(self.url)
        self.server = HostBrokerServer(
            transport,
            broker_id="b-accept",
            handler=self.ops.handle,
            grants=self.grants,
            consent=AutoConsent(True),
            journal=self.journal,
        )
        await self.server.run()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        if self.server is not None:
            try:
                asyncio.run_coroutine_threadsafe(
                    self.server.shutdown(), self.loop
                ).result(5)
            except Exception:
                pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(5)
        self.loop.close()


def _tools(tmp_path, access, settings, sandbox):
    files = build_file_tools(
        settings, sandbox, allow_write=False, file_access=access
    )
    search = build_search_tools(settings, sandbox, file_access=access)
    return {tool.name: tool for tool in [*files, *search]}


def test_windows_host_readonly_e2e(tmp_path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "a.py").write_text("print('hello')\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "b.py").write_text("x = 1\n", encoding="utf-8")

    policy = HostPolicy(
        deny_roots=[str(tmp_path / "Windows")],
        sensitive_roots=[str(tmp_path / "Documents")],
    )
    grants = GrantStore()
    grant = grants.issue(scope=[str(root)], verbs=["read"])
    ops = HostReadOnlyOps(
        policy=policy,
        path_policy=HostPathPolicy(),
        grants=grants,
        toolchains=ToolchainRegistry(
            [ToolchainRef(id="python-3.11", kind="python", version="3.11")]
        ),
        workspace_roots={"ws-1": str(root)},
        broker_id="b-e2e",
    )
    lifecycle = BrokerLifecycle(tmp_path / "a2l.lock")
    assert lifecycle.start() is True
    journal = OpJournal(tmp_path / "ops.sqlite3")
    broker = BrokerThread(ops, lifecycle, journal)
    broker.start()
    caller = SyncHostCaller(
        f"tcp://127.0.0.1:{broker.port}",
        client_id="e2e-client",
        heartbeat_s=0.2,
        request_timeout_s=5,
    )
    try:
        caller.start()
        access = HostFileAccess(
            caller,
            workspace_id="ws-1",
            root_handle="h-1",
            grant_id=grant.grant_id,
        )
        settings = Settings(_env_file=None, AGENT_WSL_WORKSPACE="/ws")
        sandbox = FakeSandbox(settings)
        tools = _tools(tmp_path, access, settings, sandbox)

        text, artifact = unpack(tools[READ_TOOL_NAME].func("a.py", "e2e"))
        assert "print('hello')" in text
        assert artifact["ok"] is True

        text, artifact = unpack(
            tools[LIST_TOOL_NAME].func(".", 1, 50, "e2e")
        )
        assert "src" in text and "a.py" in text

        text, artifact = unpack(
            tools[FIND_FILES].func("*.py", "e2e", None, 50)
        )
        assert "a.py" in text and "src/b.py" in text
        assert journal.unknown() == []

        host_backend = HostExecutionBackend(
            caller,
            workspace_id="ws-1",
            root_handle="h-1",
            grant_id=grant.grant_id,
        )
        service = ExecutionService(
            NoopBackend(),
            "/ws",
            targets={"host": host_backend},
            workspace_kind="host",
            journal_path=tmp_path / "receipts.jsonl",
        )
        result = service.execute(
            ExecutionRequest(
                op_id="e2e-read",
                kind="read_file",
                workspace_id="ws-1",
                workspace_kind="host",
                target="host",
                grant_id=grant.grant_id,
                payload={"path": "a.py"},
            )
        )
        assert result.status == "ok"
        assert "print('hello')" in result.data["content"]
    finally:
        caller.close()
        broker.stop()
        lifecycle.stop()


def test_acceptor_handshake_and_local_grant(tmp_path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "a.py").write_text("print('grant')\n", encoding="utf-8")

    grants = GrantStore()
    ops = HostReadOnlyOps(
        policy=HostPolicy(deny_roots=[], sensitive_roots=[]),
        path_policy=HostPathPolicy(),
        grants=grants,
        toolchains=ToolchainRegistry(),
        workspace_roots={"ws-1": str(root)},
        broker_id="b-accept",
    )
    journal = OpJournal(tmp_path / "grant-ops.sqlite3")
    acceptor = HostBrokerAcceptor(port=0, client_id="a3")
    port = acceptor.start()
    a2l = A2LServerThread(
        f"tcp://127.0.0.1:{port}", ops, grants, journal
    )
    a2l.start()
    try:
        for _ in range(100):
            if acceptor.clients():
                break
            time.sleep(0.02)
        assert acceptor.clients() == ["b-accept"]
        caller = acceptor.caller_for("b-accept")
        assert caller is not None
        access = HostFileAccess(
            caller,
            workspace_id="ws-1",
            root_handle="h-1",
            grant_id="",
        )
        # 没有 grant：SyncHostCaller 会走 grant.request，A2L 用 AutoConsent 签发。
        text = access.read_text("a.py", max_bytes=1024)
        assert "print('grant')" in text
    finally:
        acceptor.stop()
        a2l.stop()
