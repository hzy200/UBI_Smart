"""通知面卡片投影：首卡、审批、故障、节流与 heartbeat。"""

from __future__ import annotations

from dataclasses import dataclass, field

from ubismart.events import (
    ApprovalRequested,
    BudgetWarning,
    EstimationReady,
    RunFinished,
    RunStarted,
    Verification,
    WorkerFinished,
)
from ubismart.runtime.cards import CardProjector


@dataclass
class _Ctx:
    plan: list[str] = field(default_factory=list)
    step_idx: int = 0
    progress: dict = field(default_factory=dict)


def _projector(**overrides) -> CardProjector:
    return CardProjector(
        thread_id="t1",
        ctx=_Ctx(plan=["a", "b"], step_idx=1, progress={"plan_version": 2}),
        max_silence_s=60,
        min_interval_s=10,
        approval_ack_timeout_s=300,
        approval_default_action="pause",
        **overrides,
    )


def test_first_card_is_immediate_and_estimate_upserts_same_id() -> None:
    projector = _projector()
    first = projector.observe(RunStarted(thread_id="t1"))
    assert first[0].kind == "progress"
    assert first[0].card_id == "estimate:t1"

    estimate = projector.observe(
        EstimationReady(
            plan_version=2,
            card={
                "p50_wall_ms": 30_000,
                "p90_wall_ms": 90_000,
                "p50_tokens": 1_000,
                "p90_tokens": 3_000,
                "confidence": "med",
                "recommended": "confirm",
            },
        )
    )
    assert estimate[0].kind == "estimate"
    assert estimate[0].card_id == "estimate:t1"
    assert estimate[0].severity == "warn"
    assert estimate[0].plan_version == 2


def test_approval_card_carries_ack_timeout_and_default_action() -> None:
    projector = _projector()
    cards = projector.observe(
        ApprovalRequested(
            request_id="c1",
            tool="shell_exec",
            command="rm -rf build",
            level="L3",
            plan_version=2,
        )
    )

    card = cards[0]
    assert card.kind == "approval"
    assert card.requires_ack is True
    assert card.ack_timeout_ms == 300_000
    assert card.default_action == "pause"
    assert card.payload["pending_count"] == 1


def test_fault_cards_are_not_throttled() -> None:
    projector = _projector()
    first = projector.observe(
        Verification(status="failed", command="pytest", summary="1 failed")
    )
    second = projector.observe(
        BudgetWarning(
            level="exhausted", dimension="tokens", message="tokens 预算已耗尽"
        )
    )

    assert first[0].kind == "fault"
    assert second[0].kind == "fault"
    assert second[0].severity == "error"


def test_progress_cards_are_throttled_but_faults_are_not() -> None:
    projector = _projector()
    projector.observe(RunStarted(thread_id="t1"))
    # 首卡刚刚发过；把进度窗口拨到 10s 前，模拟下一次允许推送。
    projector._last_progress_at -= 11
    first = projector.observe(
        WorkerFinished(task_id="a", role="explore", status="success")
    )
    second = projector.observe(
        WorkerFinished(task_id="b", role="explore", status="success")
    )

    assert any(card.kind == "progress" for card in first)
    assert not any(card.kind == "progress" for card in second)

    fault = projector.observe(
        WorkerFinished(task_id="c", role="explore", status="failed")
    )
    assert any(card.kind == "fault" for card in fault)


def test_heartbeat_only_after_max_silence() -> None:
    projector = _projector()
    assert projector.heartbeat() == []

    projector.last_event_at -= 61
    cards = projector.heartbeat()

    assert cards[0].kind == "heartbeat"
    assert "当前步骤 2/2" in cards[0].body
    assert cards[0].plan_version == 2


def test_done_card_on_run_finished() -> None:
    card = _projector().observe(RunFinished(answer="ok"))[0]

    assert card.kind == "done"
    assert card.card_id == "done:t1"
