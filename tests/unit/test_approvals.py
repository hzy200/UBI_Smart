"""RFC-0004：四眼审批存储、合并与 SSE 事件解析。"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from ubismart.control.approvals import ApprovalStore, SelfApprovalError
from ubismart.control.store import ControlStore
from ubismart.web.services import AgentGateway


def _store(tmp_path: Path) -> ControlStore:
    return ControlStore(f"sqlite:///{tmp_path / 'control.db'}")


def _record(store: ApprovalStore, *, level: int = 3, call: str = "c1") -> None:
    store.record_pending(
        tenant="default",
        thread_id="t1",
        call_id=call,
        tool="shell_exec",
        command="rm -rf build",
        level=f"L{level}",
        level_int=level,
        requested_by="alice",
    )


def test_history_filters_by_epoch_range(tmp_path) -> None:
    control = _store(tmp_path)
    approvals = ApprovalStore(control.engine)
    _record(approvals)
    result = approvals.history(
        "default",
        from_ts="0",
        to_ts=str(int(time.time()) + 60),
    )
    assert result["total"] == 1
    assert result["records"][0]["call_id"] == "c1"
    control.close()


def test_self_approval_is_rejected(tmp_path) -> None:
    control = _store(tmp_path)
    approvals = ApprovalStore(control.engine)
    _record(approvals)
    pending = approvals.pending("default")
    assert pending[0]["call_id"] == "c1"
    with pytest.raises(SelfApprovalError):
        approvals.decide(
            tenant="default",
            approval_id=pending[0]["id"],
            approver="alice",
            approved=True,
        )
    control.close()


def test_admin_decision_wins_over_owner(tmp_path) -> None:
    control = _store(tmp_path)
    approvals = ApprovalStore(control.engine)
    _record(approvals)
    approval_id = approvals.pending("default")[0]["id"]
    row = approvals.decide(
        tenant="default",
        approval_id=approval_id,
        approver="root",
        approved=True,
    )
    assert row is not None and row["status"] == "approved"

    merged = approvals.merge_decisions(
        tenant="default",
        thread_id="t1",
        owner_decisions={"c1": False},
        min_level=3,
    )
    assert merged == {"c1": True}
    _record(approvals, call="c2")
    merged = approvals.merge_decisions(
        tenant="default",
        thread_id="t1",
        owner_decisions={"c1": False, "c2": True},
        min_level=3,
    )
    # c2 仍 pending：发起人的决定被忽略，A3 会按拒绝处理。
    assert merged == {"c1": True}
    control.close()


def test_low_level_owner_decision_is_kept(tmp_path) -> None:
    control = _store(tmp_path)
    approvals = ApprovalStore(control.engine)
    _record(approvals, level=2, call="c2")
    merged = approvals.merge_decisions(
        tenant="default",
        thread_id="t1",
        owner_decisions={"c2": True, "unknown": True},
        min_level=3,
    )
    assert merged == {"c2": True}
    control.close()


def test_unknown_thread_fails_closed(tmp_path) -> None:
    control = _store(tmp_path)
    approvals = ApprovalStore(control.engine)
    assert approvals.merge_decisions(
        tenant="default",
        thread_id="missing",
        owner_decisions={"c1": True},
        min_level=3,
    ) == {}
    control.close()


def test_proxy_parses_approval_events() -> None:
    seen: list[dict] = []

    class _Stream:
        status_code = 200

        async def aiter_bytes(self):  # noqa: ANN202
            yield (
                b'data: {"type":"approval_requested","request_id":"c1",'
                b'"level_int":3}\n\n'
            )
            yield b'data: {"type":"assistant_token","text":"x"}\n\n'

    async def on_event(payload: dict) -> None:
        seen.append(payload)

    async def scenario() -> None:
        gateway = object.__new__(AgentGateway)
        response = await gateway.proxy(
            _Stream(), public_thread="t1", on_event=on_event
        )
        async for _chunk in response.body_iterator:
            pass

    asyncio.run(scenario())
    assert [item["type"] for item in seen] == [
        "approval_requested",
        "assistant_token",
    ]
    assert seen[0]["request_id"] == "c1"


def test_record_pending_ignores_missing_call_id_and_low_level(tmp_path) -> None:
    control = _store(tmp_path)
    approvals = ApprovalStore(control.engine)
    approvals.record_pending(
        tenant="default",
        thread_id="t1",
        call_id="",
        tool="shell_exec",
        command="ls",
        level="L0",
        level_int=0,
        requested_by="alice",
    )
    approvals.record_pending(
        tenant="default",
        thread_id="t1",
        call_id="c0",
        tool="shell_exec",
        command="ls",
        level="L0",
        level_int=0,
        requested_by="alice",
    )
    assert approvals.pending("default") == []
    control.close()


def test_repeated_pending_updates_command_and_level(tmp_path) -> None:
    control = _store(tmp_path)
    approvals = ApprovalStore(control.engine)
    _record(approvals, level=2)
    _record(approvals, level=3)
    rows = approvals.pending("default")
    assert len(rows) == 1
    assert rows[0]["level_int"] == 3
    control.close()


def test_decide_unknown_or_other_tenant_returns_none(tmp_path) -> None:
    control = _store(tmp_path)
    approvals = ApprovalStore(control.engine)
    assert approvals.decide(
        tenant="default",
        approval_id="missing",
        approver="root",
        approved=True,
    ) is None
    _record(approvals)
    approval_id = approvals.pending("default")[0]["id"]
    assert approvals.decide(
        tenant="other",
        approval_id=approval_id,
        approver="root",
        approved=True,
    ) is None
    control.close()


def test_decide_is_idempotent_and_keeps_first_decision(tmp_path) -> None:
    control = _store(tmp_path)
    approvals = ApprovalStore(control.engine)
    _record(approvals)
    approval_id = approvals.pending("default")[0]["id"]
    first = approvals.decide(
        tenant="default",
        approval_id=approval_id,
        approver="root",
        approved=True,
    )
    again = approvals.decide(
        tenant="default",
        approval_id=approval_id,
        approver="root",
        approved=False,
    )
    assert first["status"] == "approved"
    assert again["status"] == "approved"
    control.close()


def test_merge_keeps_owner_decision_when_four_eyes_off(tmp_path) -> None:
    control = _store(tmp_path)
    approvals = ApprovalStore(control.engine)
    merged = approvals.merge_decisions(
        tenant="default",
        thread_id="missing",
        owner_decisions={"c1": True, "c2": False},
        min_level=0,
    )
    assert merged == {"c1": True, "c2": False}
    _record(approvals, level=2, call="c1")
    merged = approvals.merge_decisions(
        tenant="default",
        thread_id="t1",
        owner_decisions={"unknown": True},
        min_level=0,
    )
    assert merged == {"unknown": True}
    control.close()


def test_merge_includes_admin_decision_not_resubmitted(tmp_path) -> None:
    control = _store(tmp_path)
    approvals = ApprovalStore(control.engine)
    _record(approvals)
    approval_id = approvals.pending("default")[0]["id"]
    approvals.decide(
        tenant="default",
        approval_id=approval_id,
        approver="root",
        approved=True,
    )
    merged = approvals.merge_decisions(
        tenant="default",
        thread_id="t1",
        owner_decisions={},
        min_level=3,
    )
    assert merged == {"c1": True}
    control.close()


def test_purge_expired_only_removes_pending(tmp_path) -> None:
    from sqlalchemy.orm import Session

    from ubismart.control.schema import ControlApproval

    control = _store(tmp_path)
    approvals = ApprovalStore(control.engine)
    with Session(control.engine) as session, session.begin():
        session.add(
            ControlApproval(
                id="expired-1",
                tenant="default",
                thread_id="t1",
                call_id="old",
                tool="shell_exec",
                command="ls",
                level="L3",
                level_int=3,
                requested_by="alice",
                status="pending",
                decided_by="",
                decided_at="",
                created_at="1",
                expires_at="1",
            )
        )
    assert approvals.purge_expired() == 1
    assert approvals.purge_expired() == 0
    control.close()
