"""A2L 红队场景：**服务端失陷**方向。

`tests/unit/test_host_broker_channel.py` 等用例是"按功能验证"——握手能不能成、
grant 能不能用。这里换攻击者视角：假设 A2 控制面已被攻破，它能在已建立的
mTLS 通道里**主动投递任意帧**，能骗到 A2L 做什么。

结论口径见 `docs/THREAT_MODEL.md` §5.12 的"红队矩阵"：这里覆盖本机可跑的那几条；
需要真实 TLS / 台账热重载 / TPM 的留在矩阵里标为缺口。
"""

from __future__ import annotations

import asyncio

from coding_agent.host_broker.client import HostBrokerClient, HostBrokerOffline
from coding_agent.host_broker.protocol import (
    encode_frame,
    make_request,
    make_response,
)
from coding_agent.host_broker.server import HostBrokerServer
from coding_agent.host_broker.transport import memory_transport_pair


def _workspace() -> dict[str, str]:
    return {"workspace_id": "ws-1", "kind": "host", "root_handle": "h-1"}


def test_forged_response_cannot_hijack_an_in_flight_request() -> None:
    """服务端插一条 id 不匹配的 response，不能改写客户端拿到的结果。

    客户端的 future 按**帧 id**匹配，不是按 op_id。这条用例钉住"伪造帧被丢弃"
    这个行为 —— 一旦有人改成"取最近一个 pending"，服务端就能用一帧伪造结果
    覆盖掉真实执行结果。
    """

    async def scenario() -> None:
        client_t, server_t = memory_transport_pair()

        async def handler(frame):
            return make_response(frame, data={"served": frame.op_id})

        server = HostBrokerServer(server_t, broker_id="b-1", handler=handler)
        server_task = asyncio.create_task(server.run())
        client = HostBrokerClient(
            client_t, client_id="c-1", a2l_id="b-1", heartbeat_s=0.1
        )
        await client.connect(timeout=2)

        pending = asyncio.create_task(
            client.request(
                "host.read_text",
                workspace=_workspace(),
                payload={"path": "a.py"},
                op_id="op-real",
                timeout=2,
            )
        )
        await asyncio.sleep(0.05)  # 让真实请求先进入 pending

        forged_request = make_request("host.read_text", op_id="op-forged")
        await server_t.send(
            encode_frame(make_response(forged_request, data={"served": "forged"}))
        )

        response = await pending
        assert response.data["served"] == "op-real"
        assert client.is_online  # 伪造帧不该把连接搞坏

        await client.close()
        await server.shutdown()
        await server_task

    asyncio.run(scenario())


async def _error_of(task: asyncio.Task) -> BaseException | None:
    try:
        await task
    except Exception as exc:  # noqa: BLE001 - 这里就是要拿异常本身
        return exc
    return None


def test_client_fails_closed_when_link_drops() -> None:
    """断线后 pending 请求必须立刻失败，而不是挂在超时上。

    "掉线但界面显示在跑"是 A2L 最危险的状态：用户以为命令没执行，实际可能
    已经执行了一半。`_fail_pending` 要显式失败。
    """

    async def scenario() -> None:
        client_t, server_t = memory_transport_pair()
        stalled = asyncio.Event()

        async def handler(frame):
            # 模拟"命令正在执行、还没返回"——此时服务端断开。
            await stalled.wait()
            return make_response(frame, data={})

        server = HostBrokerServer(server_t, broker_id="b-1", handler=handler)
        server_task = asyncio.create_task(server.run())
        client = HostBrokerClient(
            client_t,
            client_id="c-1",
            a2l_id="b-1",
            heartbeat_s=0.1,
            request_timeout_s=30,
        )
        await client.connect(timeout=2)

        pending = asyncio.create_task(
            client.request(
                "host.read_text",
                workspace=_workspace(),
                payload={"path": "a.py"},
                op_id="op-1",
                timeout=30,
            )
        )
        await asyncio.sleep(0.05)
        # 直接掐断链路（不是 graceful shutdown）：模拟服务端进程被杀。
        await server_t.close()
        stalled.set()

        error = await asyncio.wait_for(_error_of(pending), 3)
        assert error is not None, "断线后 pending 请求应该失败"
        assert not client.is_online
        assert isinstance(error, HostBrokerOffline) or "断开" in str(error)

        await client.close()
        await server.shutdown()
        await server_task

    asyncio.run(scenario())


def test_handshake_is_required_before_any_request() -> None:
    """没握手就发请求：本地直接拒，不发往服务端。"""

    async def scenario() -> None:
        client_t, _server_t = memory_transport_pair()
        client = HostBrokerClient(client_t, client_id="c-1", a2l_id="b-1")
        try:
            await client.request(
                "host.read_text", workspace=_workspace(), payload={}, op_id="op-1"
            )
        except HostBrokerOffline:
            return
        raise AssertionError("未握手时请求应当被拒")

    asyncio.run(scenario())
