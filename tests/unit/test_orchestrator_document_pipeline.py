"""document_pipeline 的任务图、阶段屏障与依赖上下文。"""

from __future__ import annotations

from ubismart.config import Settings
from ubismart.orchestrator.graph import validate_tasks
from ubismart.orchestrator.plan import _default_document_tasks
from ubismart.orchestrator.scheduler_run import SchedulerRun
from ubismart.orchestrator.scheduling import task_priority
from ubismart.orchestrator.workflow import rule_workflow


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        orchestrator_enabled=True,
        orchestrator_max_tasks=8,
        report_output_dir="reports",
    )


def test_rule_selects_document_pipeline_for_uploads() -> None:
    decision = rule_workflow(
        "提炼要点并写报告",
        {"uploads": [".agent/uploads/t/a.pdf"], "file_count": 2},
        _settings(),
    )
    assert decision is not None
    assert decision.workflow == "document_pipeline"


def test_rule_selects_document_pipeline_for_document_hint() -> None:
    decision = rule_workflow(
        "读一下这个 PDF 并总结",
        {"file_count": 2},
        _settings(),
    )
    assert decision is not None
    assert decision.workflow == "document_pipeline"


def test_pipeline_switch_disables_document_workflow() -> None:
    settings = _settings().model_copy(
        update={"document_pipeline_enabled": False}
    )
    decision = rule_workflow(
        "读一下这个 PDF 并总结",
        {"file_count": 2},
        settings,
    )
    assert decision is None or decision.workflow != "document_pipeline"


def test_default_document_tasks_are_a_staged_dag() -> None:
    tasks = _default_document_tasks(
        "总结并写 Word 报告",
        {
            "uploads": [".agent/uploads/t/a.pdf"],
            "images": [".agent/uploads/t/b.png"],
        },
        _settings(),
        allow_author=True,
    )
    assert tasks is not None
    by_id = {task["id"]: task for task in tasks}
    assert by_id["doc-1"]["stage"] == 0
    assert by_id["img-1"]["stage"] == 0
    assert by_id["summary"]["stage"] == 1
    assert by_id["summary"]["depends_mode"] == "any"
    assert by_id["author"]["stage"] == 2
    assert by_id["author"]["output_format"] == "docx"
    assert by_id["review"]["stage"] == 3
    valid, degraded, errors = validate_tasks(
        tasks,
        request="x",
        max_tasks=8,
        allow_write=False,
        allow_author=True,
    )
    assert not degraded, errors
    assert {task["role"] for task in valid} >= {
        "document",
        "vision",
        "author",
        "review",
    }
    valid_by_id = {task["id"]: task for task in valid}
    assert valid_by_id["author"]["template"] == ""


def test_author_requires_write_permission() -> None:
    tasks = _default_document_tasks(
        "写报告",
        {"uploads": [".agent/uploads/t/a.pdf"]},
        _settings(),
        allow_author=False,
    )
    assert tasks is not None
    assert all(task["role"] != "author" for task in tasks)
    author_tasks = [
        {
            "id": "author",
            "role": "author",
            "goal": "写报告",
            "write_scope": ["reports"],
            "stage": 2,
        }
    ]
    _, degraded, errors = validate_tasks(
        author_tasks,
        request="x",
        max_tasks=4,
        allow_write=False,
        allow_author=False,
    )
    assert degraded
    assert any("没有写权限" in error for error in errors)


def test_scheduler_gate_routes_cancel_and_replan() -> None:
    from ubismart.orchestrator.subgraph import _route_after_scheduler_gate

    assert _route_after_scheduler_gate({"steering_cancelled": True}) == "synthesize"
    assert _route_after_scheduler_gate({"steering_replan": True}) == "plan_tasks"
    assert _route_after_scheduler_gate(
        {"task_status": {"t1": "pending"}}
    ) == "scheduler"
    assert _route_after_scheduler_gate(
        {"task_status": {"t1": "success"}}
    ) == "synthesize"


def test_pipeline_caps_documents_and_images() -> None:
    settings = _settings().model_copy(
        update={
            "orchestrator_max_tasks": 20,
            "orchestrator_document_max_docs": 2,
            "orchestrator_document_max_images": 1,
        }
    )
    tasks = _default_document_tasks(
        "总结",
        {
            "uploads": [f"doc-{index}.pdf" for index in range(5)],
            "images": [f"img-{index}.png" for index in range(5)],
        },
        settings,
        allow_author=False,
    )
    assert tasks is not None
    assert sum(1 for task in tasks if task["role"] == "document") <= 3
    assert sum(1 for task in tasks if task["role"] == "vision") == 1


def test_validate_rejects_too_many_documents() -> None:
    tasks = [
        {"id": f"doc-{index}", "role": "document", "goal": "读", "inputs": []}
        for index in range(3)
    ]
    _, degraded, errors = validate_tasks(
        tasks,
        request="x",
        max_tasks=10,
        max_documents=2,
        max_images=2,
    )
    assert degraded
    assert any("超过上限" in error for error in errors)


def test_document_checkpoint_boundary() -> None:
    run = object.__new__(SchedulerRun)
    run.settings = _settings()
    assert run.is_document_boundary(
        {"role": "document", "status": "success"}
    ) is True
    assert run.is_document_boundary(
        {"role": "vision", "status": "success"}
    ) is False
    assert run.is_document_boundary(
        {"role": "document", "status": "failed"}
    ) is False


def test_author_tasks_allow_explicit_multi_format_but_cap_at_three() -> None:
    tasks = [
        {
            "id": f"author-{index}",
            "role": "author",
            "goal": "写报告",
            "write_scope": ["reports"],
            "output_path": f"reports/report-{index}.md",
            "stage": 2,
        }
        for index in range(4)
    ]
    _, degraded, errors = validate_tasks(
        tasks,
        request="x",
        max_tasks=8,
        allow_write=False,
        allow_author=True,
    )
    assert degraded
    assert any("author 任务最多 3 个" in error for error in errors)


def test_explicit_multi_format_creates_multiple_authors() -> None:
    tasks = _default_document_tasks(
        "输出 md 和 PDF 报告",
        {"uploads": ["a.md"]},
        _settings(),
        allow_author=True,
    )
    assert tasks is not None
    authors = [task for task in tasks if task["role"] == "author"]
    assert len(authors) == 2
    assert {task["output_format"] for task in authors} == {"md", "pdf"}
    review = next(task for task in tasks if task["role"] == "review")
    assert set(review["depends_on"]) == {task["id"] for task in authors}
    valid, degraded, errors = validate_tasks(
        tasks,
        request="x",
        max_tasks=8,
        allow_write=False,
        allow_author=True,
    )
    assert not degraded, errors
    assert len([task for task in valid if task["role"] == "author"]) == 2


def test_role_priority_orders_document_pipeline() -> None:
    assert task_priority({"role": "document"}) > task_priority({"role": "vision"})
    assert task_priority({"role": "vision"}) > task_priority({"role": "author"})
    assert task_priority({"role": "author"}) > task_priority({"role": "review"})


def test_role_concurrency_limits() -> None:
    run = object.__new__(SchedulerRun)
    run.settings = _settings().model_copy(
        update={
            "orchestrator_document_concurrency": 1,
            "orchestrator_vision_concurrency": 2,
            "orchestrator_author_concurrency": 1,
        }
    )
    run.by_id = {
        "doc-1": {"id": "doc-1", "role": "document"},
        "img-1": {"id": "img-1", "role": "vision"},
    }
    run.in_flight = {"doc-1": object()}
    assert run.role_available({"role": "document"}) is False
    assert run.role_available({"role": "vision"}) is True

    run.in_flight = {"author-1": object()}
    run.by_id["author-1"] = {"id": "author-1", "role": "author"}
    assert run.role_available({"role": "author"}) is False


def test_author_is_hard_single_instance() -> None:
    run = object.__new__(SchedulerRun)
    run.settings = _settings().model_copy(
        update={"orchestrator_author_concurrency": 5}
    )
    assert run.role_limit("author") == 1


def test_role_token_budget_exhaustion() -> None:
    run = object.__new__(SchedulerRun)
    run.settings = _settings().model_copy(
        update={"orchestrator_document_token_budget": 100}
    )
    run.results = [
        {"task_id": "doc-1", "role": "document",
         "input_tokens": 80, "output_tokens": 40}
    ]
    assert run.role_token_spent("document") == 120
    assert run.role_budget_exhausted({"role": "document"}) is True
    assert run.role_budget_exhausted({"role": "vision"}) is False


def test_stage_barrier_and_dependency_context() -> None:
    run = object.__new__(SchedulerRun)
    run.tasks = [
        {"id": "doc-1", "stage": 0},
        {"id": "summary", "stage": 1},
    ]
    run.status = {"doc-1": "pending", "summary": "pending"}
    run.in_flight = {}
    run.results = []
    assert run.stage_ready(run.tasks[0]) is True
    assert run.stage_ready(run.tasks[1]) is False

    run.status["doc-1"] = "success"
    assert run.stage_ready(run.tasks[1]) is True
    run.results = [
        {
            "task_id": "doc-1",
            "role": "document",
            "status": "success",
            "summary": "文档摘要",
            "findings": ["要点一"],
            "sources": [{"path": "a.pdf", "anchor": "page=1"}],
            "attempt": 1,
        }
    ]
    context = run.dependency_context({"depends_on": ["doc-1"]})
    assert context["tasks"][0]["summary"] == "文档摘要"
    assert context["tasks"][0]["sources"][0]["anchor"] == "page=1"
