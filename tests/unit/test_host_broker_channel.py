"""A2L 内存通道：握手、请求、暂停、取消、离线。"""

from __future__ import annotations

import asyncio

import pytest

from coding_agent.host_broker.client import (
    HostBrokerClient,
    HostBrokerError,
    HostBrokerOffline,
)
from coding_agent.host_broker.consent import AutoConsent
from coding_agent.host_broker.lifecycle import BrokerLifecycle
from coding_agent.host_broker.op_journal import OP_FINISHED, OpJournal
from coding_agent.host_broker.policy import GrantStore
from coding_agent.host_broker.protocol import (
    A2L_PAUSED,
    CANCELLED,
    GRANT_RATE_LIMITED,
    GRANT_USED,
    OP_NOT_ENABLED_IN_PHASE1,
    PROTOCOL_VERSION,
    UNAUTHENTICATED,
    decode_frame,
    encode_frame,
    make_hello,
    make_request,
    make_response,
)
from coding_agent.host_broker.server import HostBrokerServer
from coding_agent.host_broker.transport import memory_transport_pair


def _workspace() -> dict[str, str]:
    return {"workspace_id": "ws-1", "kind": "host", "root_handle": "h-1"}


def test_handshake_request_and_journal(tmp_path) -> None:
    async def scenario() -> None:
        client_t, server_t = memory_transport_pair()
        lifecycle = BrokerLifecycle(tmp_path / "a2l.lock", client_id="c-1")
        assert lifecycle.start() is True
        journal = OpJournal(tmp_path / "ops.sqlite3")
        seen = []

        async def handler(frame):
            seen.append(frame)
            return make_response(
                frame, data={"kind": frame.kind, "payload": frame.payload}
            )

        server = HostBrokerServer(
            server_t,
            broker_id="b-1",
            handler=handler,
            journal=journal,
            lifecycle=lifecycle,
        )
        server_task = asyncio.create_task(server.run())
        client = HostBrokerClient(
            client_t, client_id="c-1", heartbeat_s=0.1, request_timeout_s=2
        )
        hello = await client.connect(timeout=2)
        assert hello.meta["security_tier"] == "lowpriv"
        assert hello.meta["protocol_version"] == PROTOCOL_VERSION

        response = await client.request(
            "host.read_text",
            workspace=_workspace(),
            payload={"path": "a.py"},
            op_id="op-1",
        )
        assert response.status == "ok"
        assert response.data["kind"] == "host.read_text"
        assert seen and seen[0].op_id == "op-1"
        record = journal.get("op-1")
        assert record is not None and record.status == OP_FINISHED

        await server.shutdown()
        await client.close()
        await server_task
        lifecycle.stop()

    asyncio.run(scenario())


def test_hello_nonce_replay_is_rejected(tmp_path) -> None:
    async def scenario() -> None:
        client_t, server_t = memory_transport_pair()
        server = HostBrokerServer(server_t, broker_id="b-1")
        server_task = asyncio.create_task(server.run())
        hello = make_hello("c-1", a2l_id="b-1", nonce="nonce-1")
        await client_t.send(encode_frame(hello))
        first = decode_frame(await asyncio.wait_for(client_t.recv(), 2))
        assert first.type == "hello_ok"

        await client_t.send(encode_frame(hello))
        second = decode_frame(await asyncio.wait_for(client_t.recv(), 2))
        assert second.error_code == UNAUTHENTICATED
        assert "重放" in second.error

        await server.shutdown()
        await server_task

    asyncio.run(scenario())


def test_hello_requires_encryption_and_matching_identity(tmp_path) -> None:
    async def scenario() -> None:
        client_t, server_t = memory_transport_pair(
            client_peer={"encrypted": "1", "fingerprint": "a2l-fp"},
            server_peer={"encrypted": "1", "fingerprint": "a3-fp"},
        )
        server = HostBrokerServer(
            server_t,
            broker_id="b-1",
            device_fingerprint="a2l-fp",
            require_encrypted=True,
        )
        server_task = asyncio.create_task(server.run())
        client = HostBrokerClient(
            client_t,
            client_id="c-1",
            a2l_id="b-1",
            server_fingerprint="a3-fp",
            device_fingerprint="a2l-fp",
            require_encrypted=True,
            heartbeat_s=0.1,
        )
        hello = await client.connect(timeout=2)
        assert hello.meta["a2l_id"] == "b-1"
        assert hello.meta["device_fingerprint"] == "a2l-fp"

        await client.close()
        await server.shutdown()
        await server_task

        plain_client, plain_server = memory_transport_pair()
        strict = HostBrokerServer(
            plain_server, broker_id="b-1", require_encrypted=True
        )
        strict_task = asyncio.create_task(strict.run())
        await plain_client.send(
            encode_frame(make_hello("c-1", a2l_id="b-1", nonce="n-2"))
        )
        rejected = decode_frame(await asyncio.wait_for(plain_client.recv(), 2))
        assert rejected.error_code == UNAUTHENTICATED
        assert "加密" in rejected.error
        await strict.shutdown()
        await strict_task

    asyncio.run(scenario())


def test_grant_one_shot_rate_limit_and_disconnect_revoke(tmp_path) -> None:
    async def scenario() -> None:
        client_t, server_t = memory_transport_pair()
        grants = GrantStore()
        server = HostBrokerServer(
            server_t,
            broker_id="b-1",
            grants=grants,
            consent=AutoConsent(True),
            grant_rate_limit_s=60,
        )
        server_task = asyncio.create_task(server.run())
        client = HostBrokerClient(
            client_t, client_id="c-1", heartbeat_s=0.1, request_timeout_s=2
        )
        await client.connect(timeout=2)

        granted = await client.request_grant(
            scope=[str(tmp_path)],
            verbs=["read"],
            reason="one shot",
            one_shot=True,
        )
        assert granted.type == "grant.granted"
        assert granted.payload["one_shot"] is True
        grant_id = granted.grant_id

        first = await client.request(
            "host.health",
            workspace=_workspace(),
            op_id="op-one-1",
            grant_id=grant_id,
        )
        assert first.status == "ok"
        second = await client.request(
            "host.health",
            workspace=_workspace(),
            op_id="op-one-2",
            grant_id=grant_id,
        )
        assert second.error_code == GRANT_USED

        limited = await client.request_grant(
            scope=[str(tmp_path)], verbs=["read"], reason="too fast"
        )
        assert limited.error_code == GRANT_RATE_LIMITED

        await server.shutdown()
        assert grants.get(grant_id) is None
        await client.close()
        await server_task

    asyncio.run(scenario())


def test_identity_mismatch_fails_closed(tmp_path) -> None:
    async def scenario() -> None:
        client_t, server_t = memory_transport_pair(
            client_peer={"encrypted": "1", "fingerprint": "a2l-fp"},
            server_peer={"encrypted": "1", "fingerprint": "a3-fp"},
        )
        server = HostBrokerServer(
            server_t,
            broker_id="b-1",
            device_fingerprint="a2l-fp",
            require_encrypted=True,
        )
        server_task = asyncio.create_task(server.run())
        client = HostBrokerClient(
            client_t,
            client_id="c-1",
            a2l_id="b-1",
            server_fingerprint="wrong-a3-fp",
            device_fingerprint="a2l-fp",
            require_encrypted=True,
            heartbeat_s=0.1,
        )
        with pytest.raises(HostBrokerError):
            await client.connect(timeout=2)
        await client.close()
        await server.shutdown()
        await server_task

    asyncio.run(scenario())


def test_unauthenticated_request_is_rejected(tmp_path) -> None:
    async def scenario() -> None:
        client_t, server_t = memory_transport_pair()
        server = HostBrokerServer(server_t, broker_id="b-1")
        server_task = asyncio.create_task(server.run())
        request = make_request(
            "host.health", op_id="op-1", workspace=_workspace()
        )
        await client_t.send(encode_frame(request))
        raw = await asyncio.wait_for(client_t.recv(), timeout=2)
        assert raw is not None
        response = decode_frame(raw)
        assert response.error_code == UNAUTHENTICATED
        await server.shutdown()
        await server_task

    asyncio.run(scenario())


def test_paused_server_rejects_requests(tmp_path) -> None:
    async def scenario() -> None:
        client_t, server_t = memory_transport_pair()
        lifecycle = BrokerLifecycle(tmp_path / "a2l.lock")
        lifecycle.start()
        server = HostBrokerServer(
            server_t, broker_id="b-1", lifecycle=lifecycle
        )
        server_task = asyncio.create_task(server.run())
        client = HostBrokerClient(client_t, client_id="c-1", heartbeat_s=0.1)
        await client.connect(timeout=2)

        lifecycle.pause()
        response = await client.request(
            "host.health", workspace=_workspace(), op_id="op-paused"
        )
        assert response.error_code == A2L_PAUSED

        lifecycle.resume()
        response = await client.request(
            "host.health", workspace=_workspace(), op_id="op-running"
        )
        assert response.status == "ok"

        await server.shutdown()
        await client.close()
        await server_task
        lifecycle.stop()

    asyncio.run(scenario())


def test_unknown_or_write_op_is_not_enabled(tmp_path) -> None:
    async def scenario() -> None:
        client_t, server_t = memory_transport_pair()
        server = HostBrokerServer(server_t, broker_id="b-1")
        server_task = asyncio.create_task(server.run())
        client = HostBrokerClient(client_t, client_id="c-1", heartbeat_s=0.1)
        await client.connect(timeout=2)

        response = await client.request(
            "host.write_bytes", workspace=_workspace(), op_id="op-write"
        )
        assert response.error_code == OP_NOT_ENABLED_IN_PHASE1

        await server.shutdown()
        await client.close()
        await server_task

    asyncio.run(scenario())


def test_cancel_produces_cancelled_response(tmp_path) -> None:
    async def scenario() -> None:
        client_t, server_t = memory_transport_pair()
        started = asyncio.Event()

        async def handler(frame):
            started.set()
            await asyncio.sleep(30)
            return make_response(frame)

        server = HostBrokerServer(
            server_t, broker_id="b-1", handler=handler
        )
        server_task = asyncio.create_task(server.run())
        client = HostBrokerClient(
            client_t, client_id="c-1", heartbeat_s=0.1, request_timeout_s=5
        )
        await client.connect(timeout=2)

        task = asyncio.create_task(
            client.request(
                "host.read_text",
                workspace=_workspace(),
                payload={"path": "a.py"},
                op_id="op-cancel",
            )
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        await client.cancel("op-cancel")
        response = await asyncio.wait_for(task, timeout=2)
        assert response.status == "cancelled"
        assert response.error_code == CANCELLED

        await server.shutdown()
        await client.close()
        await server_task

    asyncio.run(scenario())


def test_grant_request_uses_local_consent(tmp_path) -> None:
    async def scenario() -> None:
        client_t, server_t = memory_transport_pair()
        grants = GrantStore()
        server = HostBrokerServer(
            server_t,
            broker_id="b-1",
            grants=grants,
            consent=AutoConsent(True),
        )
        server_task = asyncio.create_task(server.run())
        client = HostBrokerClient(client_t, client_id="c-1", heartbeat_s=0.1)
        await client.connect(timeout=2)

        frame = await client.request_grant(
            scope=[str(tmp_path)], verbs=["read"], reason="test"
        )
        assert frame.type == "grant.granted"
        assert frame.grant_id
        assert grants.get(frame.grant_id) is not None

        await server.shutdown()
        await client.close()
        await server_task

    asyncio.run(scenario())


def test_grant_request_can_be_denied(tmp_path) -> None:
    async def scenario() -> None:
        client_t, server_t = memory_transport_pair()
        grants = GrantStore()
        server = HostBrokerServer(
            server_t,
            broker_id="b-1",
            grants=grants,
            consent=AutoConsent(False),
        )
        server_task = asyncio.create_task(server.run())
        client = HostBrokerClient(client_t, client_id="c-1", heartbeat_s=0.1)
        await client.connect(timeout=2)

        frame = await client.request_grant(
            scope=[str(tmp_path)], verbs=["read"], reason="test"
        )
        assert frame.type == "grant.denied"
        assert grants.active() == []

        await server.shutdown()
        await client.close()
        await server_task

    asyncio.run(scenario())


def test_server_shutdown_marks_client_offline(tmp_path) -> None:
    async def scenario() -> None:
        client_t, server_t = memory_transport_pair()
        server = HostBrokerServer(server_t, broker_id="b-1")
        server_task = asyncio.create_task(server.run())
        client = HostBrokerClient(client_t, client_id="c-1", heartbeat_s=0.1)
        await client.connect(timeout=2)
        assert client.is_online is True

        await server.shutdown()
        await server_task
        for _ in range(20):
            if not client.is_online:
                break
            await asyncio.sleep(0.01)
        assert client.is_online is False
        with pytest.raises(HostBrokerOffline):
            await client.request("host.health", op_id="op-offline")
        await client.close()

    asyncio.run(scenario())
