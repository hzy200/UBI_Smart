"""M4：恢复中心返回真实待审批数量。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from coding_agent.agent_protocol import AgentContext
from coding_agent.audit import AuditLogger, AuditRecord
from coding_agent.audit.logger import now_iso
from coding_agent.servers.agent_server_routes.state import (
    StateDeps,
    register_state_routes,
)


class _Runtime:
    async def pending_approvals(self, thread_id: str):
        return [{"call_id": "c1"}, {"call_id": "c2"}]


async def _close(_runtime) -> None:
    return None


def _app(tmp_path) -> TestClient:
    audit_dir = tmp_path / "audit"
    logger = AuditLogger(audit_dir)
    logger.write(
        AuditRecord(
            ts=now_iso(),
            kind="run_start",
            thread_id="t1",
            workspace="/tmp/ws",
            detail="interrupted task",
        )
    )
    app = FastAPI()
    register_state_routes(
        app,
        StateDeps(
            context=lambda _request: AgentContext(
                tenant="local", workspace="/tmp/ws"
            ),
            new_runtime=lambda _context: _Runtime(),
            close_runtime=_close,
            reject_active=lambda *_a: None,
            audit_dir=lambda _context: audit_dir,
            states={},
        ),
    )
    return TestClient(app)


def test_recovery_counts_pending_approvals(tmp_path) -> None:
    response = _app(tmp_path).get("/v1/recovery")
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["thread_id"] == "t1"
    assert items[0]["status"] == "interrupted"
    assert items[0]["recoverable"] is True
    assert items[0]["approvals"] == 2
