"""合并前高风险补丁审查：判定与调度接线。"""

from __future__ import annotations

import asyncio
from typing import Any

from ubismart.config import Settings
from ubismart.orchestrator import scheduler_run as scheduler_runtime
from ubismart.orchestrator.contracts import WorkerResult
from ubismart.orchestrator.patch_review import (
    diff_line_count,
    is_high_risk_patch,
    patch_line_map,
    validate_review_issues,
)
from ubismart.orchestrator.scheduler import make_scheduler_node
from ubismart.session.steering import SteeringController


def _settings(**overrides: Any) -> Settings:
    base = {
        "orchestrator_max_concurrent_workers": 1,
        "orchestrator_task_retries": 0,
        "orchestrator_checkpoint_every": 20,
        "orchestrator_run_deadline_s": 0,
        "orchestrator_incremental_merge": False,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def _task(task_id: str = "a") -> dict[str, Any]:
    return {
        "id": task_id,
        "role": "implement",
        "goal": "改一处",
        "inputs": [],
        "acceptance": [],
        "depends_on": [],
        "depends_mode": "full",
        "write_scope": ["src/"],
    }


def _state(ids: tuple[str, ...] = ("a",)) -> dict[str, Any]:
    tasks = [_task(task_id) for task_id in ids]
    return {
        "request": "做事",
        "cwd": "/ws",
        "tasks": tasks,
        "task_status": {task_id: "pending" for task_id in ids},
        "task_attempts": {},
        "worker_results": [],
        "orchestrator_round": 0,
        "task_extensions_seen": 0,
    }


def _result(task: dict[str, Any]) -> WorkerResult:
    return WorkerResult(
        task_id=str(task["id"]),
        role=str(task["role"]),
        worker_id=f"{task['role']}:{task['id']}",
        goal=str(task["goal"]),
        status="success",
        summary="ok",
        files=["src/a.py"],
    )


def test_high_risk_detection() -> None:
    settings = Settings(
        _env_file=None,
        orchestrator_patch_review=True,
        orchestrator_patch_review_max_lines=2,
    )

    assert is_high_risk_patch({"files": ["tests/test_a.py"]}, "", settings)
    assert is_high_risk_patch({"files": ["sandbox/policy.py"]}, "", settings)
    assert is_high_risk_patch({"files": [".github/workflows/ci.yml"]}, "", settings)
    assert not is_high_risk_patch({"files": ["src/a.py"]}, "+one\n", settings)
    assert is_high_risk_patch(
        {"files": ["src/a.py"]}, "+one\n+two\n+three\n", settings
    )
    assert diff_line_count("--- a\n+++ b\n-old\n+new\n") == 2


def test_disabled_review_is_not_high_risk() -> None:
    settings = Settings(
        _env_file=None, orchestrator_patch_review=False
    )
    assert not is_high_risk_patch(
        {"files": ["tests/test_a.py"]}, "+one\n+two\n", settings
    )


_PATCH = (
    "--- a/src/a.py\n"
    "+++ b/src/a.py\n"
    "@@ -1,2 +1,3 @@\n"
    " x\n"
    "+y\n"
    " z\n"
)


def test_issue_validation_drops_hallucinated_paths_and_lines() -> None:
    line_map = patch_line_map(_PATCH)
    assert line_map["src/a.py"] == 3

    kept, dropped = validate_review_issues(
        [
            {"path": "src/a.py", "line": 2, "message": "ok"},
            {"path": "ghost.py", "line": 1, "message": "不存在"},
            {"path": "src/a.py", "line": 99, "message": "越界"},
        ],
        {"files": ["src/a.py"]},
        _PATCH,
    )

    assert [(item["path"], item["line"]) for item in kept] == [
        ("src/a.py", 2)
    ]
    assert dropped == 2


def test_hallucinated_blocking_issue_does_not_block_merge(monkeypatch) -> None:
    merged: list[dict[str, Any]] = []

    async def reviewer(_record, _text, _config):  # noqa: ANN001
        return {
            "approve": False,
            "issues": [
                {
                    "path": "ghost.py",
                    "line": 1,
                    "message": "编造的问题",
                    "severity": "blocking",
                }
            ],
        }

    node = _install_runner(monkeypatch, reviewer, merged)
    out = asyncio.run(
        node(_state(), {"configurable": {"thread_id": "t"}})
    )

    assert out["task_status"]["a"] == "success"
    assert [record["task_id"] for record in merged] == ["a"]


def test_patch_reviews_run_concurrently(monkeypatch) -> None:
    async def fake_merge(sandbox, root, records, *, allow_overlap=False):  # noqa: ANN001
        return {record["task_id"]: (True, "", ["snap"]) for record in records}

    monkeypatch.setattr(
        "ubismart.orchestrator.merge_queue.merge_implement_patches_atomic",
        fake_merge,
    )

    async def fake_read(_self, _record):  # noqa: ANN001
        return _PATCH

    monkeypatch.setattr(
        scheduler_runtime.SchedulerRun, "_read_patch_text", fake_read
    )

    active = 0
    max_active = 0

    async def reviewer(_record, _text, _config):  # noqa: ANN001
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        return {"approve": True, "issues": []}

    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        return _result(task)

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(
            orchestrator_max_concurrent_workers=3,
            orchestrator_patch_review_concurrency=2,
        ),
        sandbox=object(),  # type: ignore[arg-type]
        root="/ws",
        steering=SteeringController(),
        patch_reviewer=reviewer,
    )
    out = asyncio.run(
        node(_state(("a", "b", "c")), {"configurable": {"thread_id": "t"}})
    )

    assert out["task_status"] == {"a": "success", "b": "success", "c": "success"}
    assert max_active >= 2


def _install_runner(monkeypatch: Any, reviewer: Any, calls: list[dict[str, Any]]):
    async def fake_merge(sandbox, root, records, *, allow_overlap=False):  # noqa: ANN001
        calls.extend(records)
        return {record["task_id"]: (True, "", ["snap"]) for record in records}

    monkeypatch.setattr(
        "ubismart.orchestrator.merge_queue.merge_implement_patches_atomic",
        fake_merge,
    )

    async def fake_read(_self, _record):  # noqa: ANN001
        return "--- a\n+++ b\n+change\n"

    monkeypatch.setattr(
        scheduler_runtime.SchedulerRun, "_read_patch_text", fake_read
    )

    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        return _result(task)

    return make_scheduler_node(
        worker_runner=runner,
        settings=_settings(),
        sandbox=object(),  # type: ignore[arg-type]
        root="/ws",
        steering=SteeringController(),
        patch_reviewer=reviewer,
    )


def test_rejected_patch_is_not_merged_and_feeds_findings(monkeypatch) -> None:
    merged: list[dict[str, Any]] = []

    async def reviewer(_record, _text, _config):  # noqa: ANN001
        return {
            "approve": False,
            "issues": [
                {
                    "path": "src/a.py",
                    "line": 3,
                    "message": "破坏了边界条件",
                    "severity": "blocking",
                }
            ],
        }

    node = _install_runner(monkeypatch, reviewer, merged)
    out = asyncio.run(
        node(_state(), {"configurable": {"thread_id": "t"}})
    )

    assert out["task_status"]["a"] == "failed"
    assert merged == []
    result = out["worker_results"][0]
    assert result["merged"] is False
    assert any("[patch-review]" in item for item in result["findings"])


def test_approved_patch_is_merged(monkeypatch) -> None:
    merged: list[dict[str, Any]] = []

    async def reviewer(_record, _text, _config):  # noqa: ANN001
        return {"approve": True, "issues": []}

    node = _install_runner(monkeypatch, reviewer, merged)
    out = asyncio.run(
        node(_state(), {"configurable": {"thread_id": "t"}})
    )

    assert out["task_status"]["a"] == "success"
    assert [record["task_id"] for record in merged] == ["a"]
    assert out["worker_results"][0]["merged"] is True
