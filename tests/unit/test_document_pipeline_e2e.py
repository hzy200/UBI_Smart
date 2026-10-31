"""多文档全链路：document_read → 汇总上下文 → author → report_check。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from ubismart.config import Settings
from ubismart.orchestrator.document_workers import (
    DocumentAnalysis,
    DocumentPoint,
    make_author_runner,
    make_document_runner,
    make_report_review_runner,
)
from ubismart.orchestrator.plan import _default_document_tasks
from ubismart.orchestrator.scheduler_run import SchedulerRun
from ubismart.tools.documents import build_document_tools
from ubismart.tools.report import build_report_tools


class _Structured:
    def __init__(self, analysis: DocumentAnalysis) -> None:
        self.analysis = analysis

    async def ainvoke(self, messages: Any, config: Any = None) -> DocumentAnalysis:
        return self.analysis


class _PipelineLLM:
    """文档抽取与报告撰写共用；不访问网络。"""

    def __init__(self) -> None:
        self.analysis = DocumentAnalysis(
            summary="两份文档的汇总",
            points=[
                DocumentPoint(
                    claim="要点 A",
                    evidence_path="a.md",
                    anchor="heading=1",
                    quote="A",
                    confidence="high",
                ),
                DocumentPoint(
                    claim="要点 B",
                    evidence_path="b.docx",
                    anchor="paragraph=1",
                    quote="B",
                    confidence="high",
                ),
            ],
        )

    def with_structured_output(self, schema: Any, **kwargs: Any) -> _Structured:
        return _Structured(self.analysis)

    async def ainvoke(self, messages: Any, config: Any = None) -> Any:
        class _Response:
            content = "# 综合报告\n\n## 结论\n\n- 要点 A\n- 要点 B"
            usage_metadata = {"input_tokens": 20, "output_tokens": 10}

        return _Response()


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        report_output_dir="reports",
        document_cache_enabled=False,
    )


def test_scheduler_runs_multi_document_pipeline(tmp_path: Path) -> None:
    from docx import Document

    (tmp_path / "a.md").write_text("# A\n\n正文 A", encoding="utf-8")
    word = Document()
    word.add_heading("B", level=1)
    word.add_paragraph("正文 B")
    word.save(tmp_path / "b.docx")

    settings = _settings().model_copy(
        update={
            "orchestrator_document_checkpoint": False,
            "orchestrator_checkpoint_every": 99,
            "orchestrator_run_deadline_s": 0,
        }
    )
    doc_tool = build_document_tools(settings, root=str(tmp_path))[0]
    report_tools = {
        tool.name: tool
        for tool in build_report_tools(
            settings, root=str(tmp_path), allow_write=True
        )
    }
    llm = _PipelineLLM()
    document_runner = make_document_runner(
        llm=llm, tools=[doc_tool], settings=settings
    )
    author_runner = make_author_runner(
        llm=llm, tools=list(report_tools.values()), settings=settings
    )
    review_runner = make_report_review_runner(
        tools=list(report_tools.values())
    )

    async def runner(task: dict[str, Any], **kwargs: Any) -> Any:
        role = task.get("role")
        if role == "document":
            return await document_runner(
                task,
                parent_request=kwargs.get("parent_request", ""),
                dependency_context=kwargs.get("dependency_context") or {},
            )
        if role == "author":
            return await author_runner(
                task,
                parent_request=kwargs.get("parent_request", ""),
                dependency_context=kwargs.get("dependency_context") or {},
            )
        return await review_runner(
            task,
            parent_request=kwargs.get("parent_request", ""),
            dependency_context=kwargs.get("dependency_context") or {},
        )

    tasks = _default_document_tasks(
        "写综合报告",
        {"uploads": ["a.md", "b.docx"], "images": []},
        settings,
        allow_author=True,
    )
    assert tasks is not None
    state = {
        "request": "写综合报告",
        "tasks": tasks,
        "task_status": {task["id"]: "pending" for task in tasks},
        "task_attempts": {},
        "worker_results": [],
        "orchestrator_round": 0,
        "scheduler_deadline": 0,
        "token_budget_limit": 0,
        "token_budget_spent": 0,
        "token_budget_reserved": 0,
    }
    run = SchedulerRun(
        state,
        {},
        worker_runner=runner,
        settings=settings,
        sandbox=None,
        root=str(tmp_path),
        steering=None,
        governor=None,
    )
    result = asyncio.run(run.run())
    statuses = result["task_status"]
    assert all(value == "success" for value in statuses.values()), statuses
    assert (tmp_path / "reports" / "report.md").is_file()


def test_scheduler_runs_explicit_multi_format_authors(tmp_path: Path) -> None:
    settings = _settings().model_copy(
        update={
            "orchestrator_document_checkpoint": False,
            "orchestrator_checkpoint_every": 99,
            "orchestrator_run_deadline_s": 0,
        }
    )
    tasks = _default_document_tasks(
        "输出 md 和 PDF 报告",
        {"uploads": ["a.md"], "images": []},
        settings,
        allow_author=True,
    )
    assert tasks is not None

    async def runner(task: dict[str, Any], **kwargs: Any) -> Any:
        from ubismart.orchestrator.contracts import WorkerResult

        if task.get("role") == "author":
            target = tmp_path / str(task["output_path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("report", encoding="utf-8")
            return WorkerResult(
                task_id=task["id"], role="author", status="success",
                summary="ok", files=[str(task["output_path"])],
            )
        return WorkerResult(
            task_id=task["id"], role=str(task.get("role")), status="success",
            summary="ok", files=["a.md"],
        )

    state = {
        "request": "输出 md 和 PDF 报告",
        "tasks": tasks,
        "task_status": {task["id"]: "pending" for task in tasks},
        "task_attempts": {},
        "worker_results": [],
        "orchestrator_round": 0,
        "scheduler_deadline": 0,
        "token_budget_limit": 0,
    }
    run = SchedulerRun(
        state, {}, worker_runner=runner, settings=settings,
        sandbox=None, root=str(tmp_path), steering=None, governor=None,
    )
    result = asyncio.run(run.run())
    assert all(
        value == "success" for value in result["task_status"].values()
    )
    files = sorted((tmp_path / "reports").glob("report-*.*"))
    assert {path.suffix for path in files} == {".md", ".pdf"}
    assert len(files) == 2


def test_scheduler_cancel_stops_document_pipeline(tmp_path: Path) -> None:
    settings = _settings().model_copy(
        update={"orchestrator_run_deadline_s": 0}
    )
    tasks = _default_document_tasks(
        "写报告",
        {"uploads": ["a.md"], "images": []},
        settings,
        allow_author=True,
    )
    assert tasks is not None
    state = {
        "request": "写报告",
        "tasks": tasks,
        "task_status": {task["id"]: "pending" for task in tasks},
        "task_attempts": {},
        "worker_results": [],
        "orchestrator_round": 0,
        "scheduler_deadline": 0,
        "token_budget_limit": 0,
        "steering_cancelled": True,
    }
    run = SchedulerRun(
        state,
        {},
        worker_runner=None,
        settings=settings,
        sandbox=None,
        root=str(tmp_path),
        steering=None,
        governor=None,
    )
    result = asyncio.run(run.run())
    assert all(
        value == "cancelled" for value in result["task_status"].values()
    )


def test_multi_document_pipeline_end_to_end(tmp_path: Path) -> None:
    from docx import Document

    (tmp_path / "a.md").write_text("# A\n\n正文 A", encoding="utf-8")
    word = Document()
    word.add_heading("B", level=1)
    word.add_paragraph("正文 B")
    word.save(tmp_path / "b.docx")

    settings = _settings()
    doc_tool = build_document_tools(settings, root=str(tmp_path))[0]
    report_tools = {
        tool.name: tool
        for tool in build_report_tools(
            settings, root=str(tmp_path), allow_write=True
        )
    }
    llm = _PipelineLLM()
    document_runner = make_document_runner(
        llm=llm, tools=[doc_tool], settings=settings
    )
    author_runner = make_author_runner(
        llm=llm, tools=list(report_tools.values()), settings=settings
    )
    review_runner = make_report_review_runner(
        tools=list(report_tools.values())
    )

    async def run() -> tuple[list[Any], Any, Any]:
        documents = await asyncio.gather(
            document_runner(
                {
                    "id": "doc-a",
                    "role": "document",
                    "goal": "提炼 a.md",
                    "inputs": ["a.md"],
                },
                parent_request="写综合报告",
            ),
            document_runner(
                {
                    "id": "doc-b",
                    "role": "document",
                    "goal": "提炼 b.docx",
                    "inputs": ["b.docx"],
                },
                parent_request="写综合报告",
            ),
        )
        context = {"tasks": [result.model_dump() for result in documents]}
        author = await author_runner(
            {
                "id": "author",
                "role": "author",
                "goal": "写综合报告",
                "output_format": "md",
                "output_path": "reports/report.md",
                "write_scope": ["reports"],
            },
            parent_request="写综合报告",
            dependency_context=context,
        )
        review = await review_runner(
            {
                "id": "review",
                "role": "review",
                "goal": "校验报告",
                "inputs": ["reports/report.md"],
            },
            parent_request="写综合报告",
            dependency_context=context,
        )
        return documents, author, review

    documents, author, review = asyncio.run(run())
    assert all(result.status == "success" for result in documents)
    assert author.status == "success", author.error
    report = (tmp_path / "reports" / "report.md").read_text(encoding="utf-8")
    assert "要点 A" in report and "要点 B" in report
    assert "a.md#heading=1" in report
    assert "b.docx#paragraph=1" in report
    assert review.status == "success"
    assert review.findings and "校验通过" in review.findings[0]
