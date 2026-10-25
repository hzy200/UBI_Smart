"""LangSmith 全功能套件的结构守卫。

套件本身是“全量测试”的尺子：case 数量、ID 唯一性、机制探针是否真的通过，
都必须在不调用模型、不依赖 WSL 的前提下验证。否则某次重构把探针改坏了，
要等到烧完一轮全量额度才会发现。
"""

from __future__ import annotations

import sys
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parents[1] / "eval"
if str(EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(EVAL_DIR))

import langsmith_suite as suite  # noqa: E402


def test_case_count_exceeds_hundred() -> None:
    cases = suite._case_ids("all")
    assert len(cases) > 100, f"全量用例只有 {len(cases)} 个"
    assert len(set(cases)) == len(cases), "case id 有重复"


def test_every_kind_is_represented() -> None:
    assert suite.MECHANISMS
    assert suite.AGENT_CASES
    assert len(suite.CAPABILITY_IDS) >= 100
    assert len(suite.HARD_IDS) >= 12


def test_hard_suite_is_separate_from_the_full_dataset() -> None:
    """hard 任务有独立数据集，不能默认混进 `--kinds all` 的高成本全量。"""
    hard_ids = suite._case_ids("hard")
    assert hard_ids == list(suite.HARD_IDS)
    assert set(hard_ids).isdisjoint(suite._case_ids("all"))
    tasks = suite._tasks_by_id()
    assert all(task_id in tasks for task_id in hard_ids)


def test_real_suite_is_separate_from_the_full_dataset() -> None:
    real_ids = suite._case_ids("real")
    assert len(real_ids) >= 5
    assert real_ids == list(suite.REAL_IDS)
    assert set(real_ids).isdisjoint(suite._case_ids("all"))
    tasks = suite._tasks_by_id()
    assert all(task_id in tasks for task_id in real_ids)


def test_real_advanced_suite_is_separate() -> None:
    ids = suite._case_ids("real_advanced")
    assert len(ids) >= 6
    assert ids == list(suite.REAL_ADVANCED_IDS)
    assert set(ids).isdisjoint(suite._case_ids("all"))
    tasks = suite._tasks_by_id()
    assert all(task_id in tasks for task_id in ids)


def test_blind_suite_is_separate() -> None:
    ids = suite._case_ids("blind")
    assert len(ids) >= 5
    assert ids == list(suite.BLIND_IDS)
    assert set(ids).isdisjoint(suite._case_ids("all"))
    tasks = suite._tasks_by_id()
    assert all(task_id in tasks for task_id in ids)


def test_agent_cases_point_to_existing_tasks() -> None:
    tasks = suite._tasks_by_id()
    for case_id, spec in suite.AGENT_CASES.items():
        assert spec["task_id"] in tasks, f"{case_id} 指向不存在的任务"
        assert suite._case_input(case_id)["kind"] == "agent"


def test_capability_checks_point_to_existing_tasks() -> None:
    tasks = suite._tasks_by_id()
    for case_id, spec in suite.CAPABILITY_CHECKS.items():
        assert case_id in tasks, f"{case_id} 不是 capability 任务"
        assert spec.get("expected_workflow"), f"{case_id} 没有声明 workflow"
        assert suite._case_input(case_id)["kind"] == "capability"


def test_capability_case_fails_when_workflow_does_not_match(monkeypatch) -> None:
    """任务判定通过、但机制不对，整条 case 仍算失败。"""
    from harness import TaskResult

    async def fake_run_task(*args, **kwargs):
        return TaskResult(
            task_id="long/parallel_modules",
            category="cross_file",
            verdict="passed",
            workflow="direct",
            workers=1,
        )

    monkeypatch.setattr(suite, "run_task", fake_run_task)
    monkeypatch.setattr(suite, "task_root", lambda *a, **k: "/tmp/ws")

    out = suite._run_capability(
        "long/parallel_modules", None, None, None, ""
    )

    assert out["verdict"] == "passed"
    assert out["checks"] == {"workflow": False, "workers": False}
    assert out["passed"] is False


def test_capability_case_passes_when_checks_hold(monkeypatch) -> None:
    from harness import TaskResult

    async def fake_run_task(*args, **kwargs):
        return TaskResult(
            task_id="long/parallel_modules",
            category="cross_file",
            verdict="passed",
            workflow="parallel_readonly",
            workers=5,
            worker_statuses_by_role={"test": {"failed": 2}},
        )

    monkeypatch.setattr(suite, "run_task", fake_run_task)
    monkeypatch.setattr(suite, "task_root", lambda *a, **k: "/tmp/ws")

    out = suite._run_capability(
        "long/parallel_modules", None, None, None, ""
    )

    assert out["checks"] == {"workflow": True, "workers": True}
    assert out["passed"] is True


def test_subagent_case_disables_orchestration_and_requires_delegation() -> None:
    spec = suite.AGENT_CASES["agent/subagent_explore"]
    assert spec["settings"]["orchestrator_enabled"] is False
    assert spec["settings"]["explore_enabled"] is True
    assert spec["settings"]["delegate_min_files"] == 0
    assert spec["min_subagents"] >= 1
    assert spec["min_delegate_batch"] >= 2
    assert spec["prompt_prefix"].startswith("动手前先用")


def test_agent_case_applies_prompt_prefix(monkeypatch) -> None:
    """prompt 覆盖只能加前缀，任务原有的需求与测试必须保留。"""
    from harness import TaskResult

    seen: dict[str, str] = {}

    async def fake_run_task(task, *args, **kwargs):
        seen["prompt"] = task.prompt
        return TaskResult(
            task_id=task.id,
            category=task.category,
            verdict="passed",
            subagents=2,
            max_delegate_batch=2,
        )

    monkeypatch.setattr(suite, "run_task", fake_run_task)
    monkeypatch.setattr(suite, "task_root", lambda *a, **k: "/tmp/ws")

    out = suite._run_agent_case(
        "agent/subagent_explore",
        suite.Settings(_env_file=None),
        None,
        None,
        "",
    )

    assert seen["prompt"].startswith(
        suite.AGENT_CASES["agent/subagent_explore"]["prompt_prefix"]
    )
    assert "整单折扣" in seen["prompt"]
    assert out["checks"] == {"subagents": True, "delegate_batch": True}
    assert out["passed"] is True


def test_mechanism_checks_combine_workflow_and_worker_assertions() -> None:
    class _Result:
        workflow = "direct"
        workers = 1
        worker_merged = 0

    checks = suite._mechanism_checks(
        {
            "expected_workflow": "parallel_readonly",
            "min_workers": 2,
            "min_merged": 1,
        },
        _Result(),
    )

    assert checks == {"workflow": False, "workers": False, "merged": False}


def test_pure_mechanism_probes_pass() -> None:
    """机制探针必须确定性通过。

    `shell/persistent` 真实启动 WSL 常驻 shell，由带 `wsl` 标记的集成用例覆盖，
    这里跳过，避免在无 WSL 的 CI 上误报。
    """
    skipped = {"shell/persistent"}
    failures = {
        name: result
        for name, probe in suite.MECHANISMS.items()
        if name not in skipped
        for result in [probe()]
        if not result.get("passed")
    }
    assert not failures, failures
