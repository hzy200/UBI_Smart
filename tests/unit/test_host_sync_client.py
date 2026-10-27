"""A2L RPC 超时：目录选择是人工操作，不能用普通 30 秒超时。"""

from __future__ import annotations

from coding_agent.host_broker.protocol import GRANT_REQUIRED, Frame
from coding_agent.host_broker.sync_client import (
    INTERACTIVE_TIMEOUT_S,
    GrantCache,
    call_with_grant,
)


class _Client:
    def __init__(self) -> None:
        self.timeouts: list[float] = []

    def request(self, _kind, **kwargs):
        self.timeouts.append(float(kwargs["timeout"]))
        return Frame(type="response", data={"ok": True})


def _call(kind: str) -> tuple[list[float], list[float], dict]:
    client = _Client()
    run_timeouts: list[float] = []

    def run(coro, *, timeout=None):
        run_timeouts.append(float(timeout or 0))
        return coro

    result = call_with_grant(
        run,
        client,  # type: ignore[arg-type]
        kind=kind,
        workspace={},
        payload={},
        grant_id="",
        client_id="a2l",
        request_timeout_s=30.0,
    )
    return client.timeouts, run_timeouts, result


def test_workspace_register_has_interactive_timeout() -> None:
    request_timeouts, run_timeouts, result = _call("host.workspace.register")
    assert result == {"ok": True}
    assert request_timeouts[0] >= INTERACTIVE_TIMEOUT_S
    assert run_timeouts[0] > request_timeouts[0]


def test_normal_ops_keep_short_timeout() -> None:
    request_timeouts, run_timeouts, _ = _call("host.read_text")
    assert request_timeouts == [30.0]
    assert run_timeouts == [35.0]


class _GrantClient:
    def __init__(self) -> None:
        self.grant_ids: list[str] = []
        self.grant_requests = 0

    def request(self, _kind, **kwargs):
        grant_id = str(kwargs.get("grant_id") or "")
        self.grant_ids.append(grant_id)
        if grant_id == "g1":
            return Frame(type="response", data={"ok": True})
        return Frame(
            type="response",
            error_code=GRANT_REQUIRED,
            data={
                "scope": [r"C:\ws"],
                "verbs": ["read"],
                "ttl_s": 1800,
            },
        )

    def request_grant(self, **_kwargs):
        self.grant_requests += 1
        return Frame(type="grant.granted", grant_id="g1", payload={})


def test_grant_is_cached_for_workspace() -> None:
    client = _GrantClient()
    grants = GrantCache()

    def run(value, *, timeout=None):
        return value

    def call() -> dict:
        return call_with_grant(
            run,
            client,  # type: ignore[arg-type]
            kind="host.read_text",
            workspace={"workspace_id": "ws-1"},
            payload={"path": "a.txt"},
            grant_id="",
            client_id="a2l",
            request_timeout_s=30.0,
            grants=grants,
        )

    assert call() == {"ok": True}
    assert client.grant_requests == 1
    assert call() == {"ok": True}
    # 第二次直接用缓存 grant，不再请求本地授权。
    assert client.grant_requests == 1
    assert client.grant_ids == ["", "g1", "g1"]
