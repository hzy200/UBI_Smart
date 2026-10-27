"""request id 上下文与 A2→A3 透传。"""

from __future__ import annotations

from coding_agent.agent_protocol import AgentContext
from coding_agent.control.agent_client import HttpAgentClient
from coding_agent.request_context import (
    current_request_id,
    reset_request_id,
    set_request_id,
)


def test_agent_client_propagates_request_id() -> None:
    client = HttpAgentClient("http://127.0.0.1:1", "internal-token")
    token = set_request_id("client-request-123456")
    try:
        headers = client._headers(AgentContext())
        assert headers["X-Request-ID"] == "client-request-123456"
    finally:
        reset_request_id(token)
    assert current_request_id() == ""
