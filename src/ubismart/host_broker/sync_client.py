"""同步 A2L 调用桥：给工具层/ExecutionBackend 用。

工具层是同步的，A2L 客户端是 asyncio。这里用一个后台事件循环线程持有
连接，把 `call()` 映射成 `HostBrokerClient.request()`；不把事件循环泄漏
到工具层。
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from typing import Any

from ubismart.host_broker.client import HostBrokerClient, HostBrokerError
from ubismart.host_broker.errors import BrokerOpError
from ubismart.host_broker.protocol import (
    DENIED_SCOPE,
    DENIED_VERB,
    GATEWAY_OFFLINE,
    GRANT_DENIED,
    GRANT_EXPIRED,
    GRANT_REQUIRED,
    GRANT_REVOKED,
)
from ubismart.host_broker.transport import (
    TlsConfig,
    Transport,
    connect_tcp,
)

# 目录选择/本机确认是人工操作，不能按普通 RPC 的 30 秒超时。
INTERACTIVE_TIMEOUT_S = 600.0


_READ_KINDS = frozenset(
    {
        "host.health",
        "host.workspace.register",
        "host.list_dir",
        "host.find_files",
        "host.stat",
        "host.read_text",
        "host.read_chunk",
        "host.toolchains",
        "host.env.inspect",
        "host.snapshot_list",
    }
)
_WRITE_KINDS = frozenset(
    {
        "host.write_begin",
        "host.write_chunk",
        "host.write_commit",
        "host.write_abort",
        "host.write_text",
        "host.write_many_commit",
        "host.delete",
        "host.mkdir",
        "host.snapshot_save",
        "host.restore",
    }
)


def verb_for_kind(kind: str) -> str:
    if kind in _READ_KINDS:
        return "read"
    if kind in _WRITE_KINDS:
        return "write"
    if kind == "host.exec":
        return "exec"
    if kind == "host.install":
        return "install"
    return ""


class GrantCache:
    """按 (workspace, verb) 缓存本地 grant，避免混用读写授权。"""

    def __init__(self, ttl_s: float = 1700.0) -> None:
        self.ttl_s = float(ttl_s)
        self._items: dict[tuple[str, str], tuple[str, float]] = {}
        self._lock = threading.Lock()

    def get(self, workspace_id: str, verb: str = "") -> str:
        if not workspace_id:
            return ""
        key = (workspace_id, str(verb))
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return ""
            grant_id, expires = item
            if expires <= time.monotonic():
                self._items.pop(key, None)
                return ""
            return grant_id

    def put(
        self, workspace_id: str, grant_id: str, verb: str = ""
    ) -> None:
        if not workspace_id or not grant_id:
            return
        if verb in {"exec", "install"}:
            # one-shot 授权不能复用。
            return
        with self._lock:
            self._items[(workspace_id, str(verb))] = (
                grant_id,
                time.monotonic() + self.ttl_s,
            )

    def clear(self, workspace_id: str, verb: str | None = None) -> None:
        with self._lock:
            if verb is None:
                for key in [
                    key
                    for key in self._items
                    if key[0] == workspace_id
                ]:
                    self._items.pop(key, None)
            else:
                self._items.pop((workspace_id, str(verb)), None)

    def clear_all(self) -> None:
        with self._lock:
            self._items.clear()


def call_with_grant(
    run,
    client: HostBrokerClient,
    *,
    kind: str,
    workspace: dict[str, Any],
    payload: dict[str, Any],
    grant_id: str,
    client_id: str,
    request_timeout_s: float,
    grants: GrantCache | None = None,
    verb: str = "",
) -> dict[str, Any]:
    """发起请求；遇到 GRANT_REQUIRED 时走 grant.request 并重试一次。"""
    workspace_id = str(workspace.get("workspace_id") or "")
    verb = str(verb or verb_for_kind(kind))
    if not grant_id and grants is not None:
        grant_id = grants.get(workspace_id, verb)
    send_timeout = (
        max(request_timeout_s, INTERACTIVE_TIMEOUT_S)
        if kind == "host.workspace.register"
        else request_timeout_s
    )

    def _send(current_grant: str, *, timeout: float | None = None):
        effective = timeout or send_timeout
        return run(
            client.request(
                kind,
                workspace=workspace,
                payload=payload,
                grant_id=current_grant,
                op_id=f"a2l-{client_id}-{uuid.uuid4().hex}",
                timeout=effective,
            ),
            timeout=effective + 5,
        )

    try:
        frame = _send(grant_id)
        # 缓存的 grant 过期/被撤销/动词或 scope 不匹配时清掉并重试一次。
        if grant_id and frame.error_code in {
            GRANT_EXPIRED,
            GRANT_REVOKED,
            DENIED_VERB,
            DENIED_SCOPE,
        }:
            if grants is not None:
                grants.clear(workspace_id, verb)
            grant_id = ""
            frame = _send(grant_id)
    except HostBrokerError as exc:
        raise BrokerOpError(exc.code, exc.message) from exc
    if frame.error_code == GRANT_REQUIRED:
        data = dict(frame.data or {})
        scope = data.get("scope")
        verbs = data.get("verbs")
        if isinstance(scope, list) and isinstance(verbs, list):
            grant_frame = run(
                client.request_grant(
                    scope=[str(item) for item in scope],
                    verbs=[str(item) for item in verbs],
                    ttl_s=int(data.get("ttl_s", 1800)),
                    reason=str(data.get("reason") or ""),
                    sensitive=bool(data.get("sensitive", False)),
                    one_shot=bool(
                        data.get("one_shot", False)
                        or verb in {"exec", "install"}
                    ),
                ),
                timeout=INTERACTIVE_TIMEOUT_S,
            )
            if grant_frame.type == "grant.denied":
                raise BrokerOpError(
                    GRANT_DENIED,
                    str(grant_frame.payload.get("reason") or "本地拒绝"),
                )
            if not grant_frame.grant_id:
                raise BrokerOpError(GRANT_DENIED, "本地授权未返回 grant_id")
            if grants is not None:
                grants.put(workspace_id, grant_frame.grant_id, verb)
            frame = _send(
                grant_frame.grant_id,
                timeout=max(send_timeout, request_timeout_s + 30),
            )
    if frame.error_code:
        raise BrokerOpError(frame.error_code, frame.error)
    return dict(frame.data)


class SyncHostCaller:
    def __init__(
        self,
        url: str,
        *,
        client_id: str,
        security_tier: str = "lowpriv",
        heartbeat_s: float = 15.0,
        request_timeout_s: float = 30.0,
        transport: Transport | None = None,
        tls: TlsConfig | None = None,
        a2l_id: str = "",
        server_fingerprint: str = "",
        device_fingerprint: str = "",
        require_encrypted: bool = False,
    ) -> None:
        self.url = str(url or "")
        self.client_id = str(client_id or "")
        self.security_tier = str(security_tier or "lowpriv")
        self.heartbeat_s = float(heartbeat_s)
        self.request_timeout_s = float(request_timeout_s)
        self.tls = tls
        self.a2l_id = str(a2l_id or "")
        self.server_fingerprint = str(server_fingerprint or "")
        self.device_fingerprint = str(device_fingerprint or "")
        self.require_encrypted = bool(require_encrypted)
        self._grants = GrantCache()
        self._transport = transport
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever,
            name="a2l-sync-caller",
            daemon=True,
        )
        self._client: HostBrokerClient | None = None
        self._lock = threading.Lock()
        self._started = False

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._thread.start()
            try:
                self._run(self._connect())
            except Exception:
                self.close()
                raise
            self._started = True

    async def _connect(self) -> None:
        transport = self._transport
        if transport is None:
            transport = await connect_tcp(self.url, tls=self.tls)
        client = HostBrokerClient(
            transport,
            client_id=self.client_id,
            security_tier=self.security_tier,
            heartbeat_s=self.heartbeat_s,
            request_timeout_s=self.request_timeout_s,
            a2l_id=self.a2l_id,
            server_fingerprint=self.server_fingerprint,
            device_fingerprint=self.device_fingerprint,
            require_encrypted=self.require_encrypted,
        )
        await client.connect()
        self._client = client

    def call(
        self,
        kind: str,
        *,
        workspace: dict[str, Any],
        payload: dict[str, Any],
        grant_id: str = "",
    ) -> dict[str, Any]:
        if not self._started:
            self.start()
        client = self._client
        if client is None or not client.is_online:
            raise BrokerOpError(GATEWAY_OFFLINE, "A2L 不在线")
        timeout = self.request_timeout_s
        if kind == "host.exec":
            timeout = max(
                timeout, float(payload.get("timeout_s", 60.0)) + 15.0
            )
        return call_with_grant(
            self._run,
            client,
            kind=kind,
            workspace=workspace,
            payload=payload,
            grant_id=grant_id,
            client_id=self.client_id,
            request_timeout_s=timeout,
            grants=self._grants,
            verb=verb_for_kind(kind),
        )

    def close(self) -> None:
        with self._lock:
            client = self._client
            self._client = None
            self._started = False
            self._grants.clear_all()
            if client is not None:
                try:
                    self._run(client.close())
                except Exception:
                    pass
            if self._thread.is_alive():
                self._loop.call_soon_threadsafe(self._loop.stop)
                self._thread.join(timeout=5)
            if not self._loop.is_closed():
                self._loop.close()

    def _run(self, coro, *, timeout: float | None = None):
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(
            timeout=timeout or (self.request_timeout_s + 5)
        )
