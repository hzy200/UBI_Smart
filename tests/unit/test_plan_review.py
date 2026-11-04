"""契约归一化与独立计划评审（S2/S4）。"""

from __future__ import annotations

from ubismart.orchestrator.contracts import TaskContract, TaskSpec
from ubismart.orchestrator.plan_review import (
    normalize_contract,
    review_plan,
)


def _task(**overrides):
    base = {
        "id": "t1",
        "role": "explore",
        "goal": "定位定价逻辑",
        "inputs": ["pricing.py"],
        "acceptance": ["列出至少一个 file:line 证据"],
        "depends_on": [],
    }
    return {**base, **overrides}


def test_normalize_contract_derives_acceptance_and_side_effects() -> None:
    tasks = [
        _task(
            id="impl",
            role="implement",
            goal="实现定价",
            write_scope=["pricing.py"],
            acceptance=[],
        )
    ]
    contract = normalize_contract({}, request="实现定价", tasks=tasks)

    assert contract["goal"] == "实现定价"
    assert contract["acceptance"]
    assert any(rule["auto"] for rule in contract["acceptance"])
    assert tasks[0]["acceptance"], "缺失 acceptance 应被宿主补齐"
    assert contract["side_effects"][0]["kind"] == "file"
    assert contract["side_effects"][0]["target"] == "pricing.py"


def test_contract_unknowns_are_merged_from_tasks() -> None:
    tasks = [_task(unknown=["币种未指定"])]
    contract = normalize_contract(
        {"goal": "对比价格", "acceptance": [{"rule": "测试通过", "auto": True}]},
        request="对比价格",
        tasks=tasks,
    )

    assert contract["unknown"] == ["t1: 币种未指定"]


def test_review_rejects_unverifiable_and_unbounded_steps() -> None:
    contract = {
        "goal": "实现定价",
        "acceptance": [{"rule": "测试通过", "auto": True}],
        "constraints": [],
        "known": {},
        "unknown": [],
        "side_effects": [],
    }
    review = review_plan(
        contract,
        [
            _task(id="t1", verifiable=False),
            _task(id="t2", branching="unbounded"),
        ],
    )

    assert review.ok is False
    assert any("不可验证" in reason for reason in review.reasons)
    assert any("无界分支" in reason for reason in review.reasons)


def test_review_rejects_unmapped_and_duplicate_tasks() -> None:
    contract = {
        "goal": "实现定价",
        "acceptance": [{"rule": "测试通过", "auto": True}],
        "constraints": [],
        "known": {},
        "unknown": [],
        "side_effects": [],
    }
    review = review_plan(
        contract,
        [
            _task(id="t1", acceptance=[], acceptance_refs=[]),
            _task(id="t2", goal="定位定价逻辑"),
        ],
    )

    assert review.ok is False
    assert any("没有映射" in reason for reason in review.reasons)
    assert any("重复" in reason for reason in review.reasons)


def test_models_tolerate_string_acceptance_and_side_effects() -> None:
    contract = TaskContract(
        goal="实现定价",
        acceptance=["运行测试并确认退出码为 0"],
        side_effects=["pricing.py"],
        unknown="币种未指定",
    )
    task = TaskSpec(
        id="t1",
        role="explore",
        goal="定位",
        inputs="pricing.py",
        acceptance="列出 file:line 证据",
        depends_on="t0",
    )

    assert contract.acceptance[0].auto is True
    assert contract.side_effects[0].kind == "external"
    assert contract.unknown == ["币种未指定"]
    assert task.inputs == ["pricing.py"]
    assert task.depends_on == ["t0"]
    assert task.acceptance == ["列出 file:line 证据"]


def test_review_accepts_a_well_formed_plan() -> None:
    contract = {
        "goal": "实现定价",
        "acceptance": [{"rule": "测试通过", "auto": True}],
        "constraints": [],
        "known": {},
        "unknown": [],
        "side_effects": [],
    }
    review = review_plan(contract, [_task(acceptance_refs=[0])])

    assert review.ok is True
    assert review.reasons == []
