"""只读 worker runner（Phase 3A）。

- `explore`：复用现有 Explore 子智能体（独立上下文 + 只读工具）；
- `test`：复用宿主测试 runner（确定性，不调 LLM）。

两者都返回结构化 `WorkerResult`，由 dispatch 统一汇总。Phase 3A 不允许写。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.runnables import RunnableConfig

from ubismart.agents.explore import DelegateRunner
from ubismart.config import Settings
from ubismart.identity.instructions import load_project_instructions
from ubismart.memory.longterm import (
    LayeredMemory,
    LongTermMemory,
    resolve_global_memory_root,
    resolve_memory_root,
)
from ubismart.memory.semantic import wrap_memory
from ubismart.orchestrator.contracts import WorkerResult
from ubismart.orchestrator.implement import ImplementRunner
from ubismart.orchestrator.retry import format_retry_context
from ubismart.platform.read_cache import ReadCache
from ubismart.sandbox.fs import SandboxFs
from ubismart.sandbox.wsl_exec import WslSandbox
from ubismart.tools.testrun import run_verification

WorkerRunner = Callable[..., Awaitable[WorkerResult]]


def _make_instructions_loader(
    sandbox: WslSandbox, root: str, trusted: bool
) -> Callable[[], str]:
    # 指令加载要起一次 WSL `find`：延迟到第一次真正跑 worker 时再做，
    # 纯 direct 会话不为编排功能付启动成本。
    cache: list[str] = []

    def load() -> str:
        if not cache:
            try:
                text = load_project_instructions(
                    sandbox,
                    root,
                    trusted=trusted,
                )
            except Exception:  # noqa: BLE001 - 指令读取失败不阻断 worker
                text = ""
            cache.append(text)
        return cache[0]

    return load


def _search_memories(
    memory: Any,
    task: dict[str, Any],
    parent_request: str,
    top_k: int,
) -> list[str]:
    """按任务目标筛 top-K；记忆读取失败不能阻断 worker。"""
    query = f"{task.get('goal', '')}\n{parent_request}"
    try:
        return memory.search(query, top_k)
    except Exception:  # noqa: BLE001 - 记忆是增强信息，不是执行前提
        return []


def _select_readonly_runner(
    role: str,
    escalate: bool,
    explore_runner: DelegateRunner | None,
    review_runner: DelegateRunner | None,
    explore_fallback_runner: DelegateRunner | None,
    review_fallback_runner: DelegateRunner | None,
) -> DelegateRunner | None:
    runner = explore_runner if role == "explore" else review_runner
    fallback = (
        explore_fallback_runner
        if role == "explore"
        else review_fallback_runner
    )
    if escalate and fallback is not None:
        runner = fallback
    return runner


async def _run_readonly(
    task: dict[str, Any],
    *,
    task_id: str,
    role: str,
    goal: str,
    worker_id: str,
    retry_note: str,
    parent_request: str,
    session_brief: str,
    cwd: str,
    config: RunnableConfig | None,
    cancel: Callable[[], bool] | None,
    dependency_context: dict[str, Any] | None,
    memories: list[str],
    escalate: bool,
    explore_runner: DelegateRunner | None,
    review_runner: DelegateRunner | None,
    explore_fallback_runner: DelegateRunner | None,
    review_fallback_runner: DelegateRunner | None,
    report_review_runner: Callable[..., Awaitable[WorkerResult]] | None,
    instructions: Callable[[], str],
) -> WorkerResult:
    report_inputs = any(
        str(item).lower().endswith((".md", ".docx", ".pdf"))
        for item in (task.get("inputs") or [])
    )
    if role == "review" and report_inputs and report_review_runner is not None:
        return await report_review_runner(
            task,
            parent_request=parent_request,
            session_brief=session_brief,
            cwd=cwd,
            config=config,
            cancel=cancel,
            dependency_context=dependency_context or {},
        )
    runner = _select_readonly_runner(
        role,
        escalate,
        explore_runner,
        review_runner,
        explore_fallback_runner,
        review_fallback_runner,
    )
    if runner is None:
        return WorkerResult(
            task_id=task_id,
            role=role,
            worker_id=worker_id,
            goal=goal,
            status="failed",
            summary=f"{role} worker 未配置（orchestrator 缺少 runner）",
        )
    artifact = await runner(
        args={
            "goal": f"{goal}\n{retry_note}" if retry_note else goal,
            "paths": list(task.get("inputs") or []),
            "repo_map": str(task.get("repo_map") or ""),
            "session_brief": session_brief,
            "memories": memories,
            "instructions": instructions(),
            "reason": f"编排器派发的只读 {role} 任务",
        },
        call_id=task_id,
        parent_request=parent_request,
        cwd=cwd,
        config=config,
        cancel=cancel,
    )
    return WorkerResult(
        task_id=task_id,
        role=role,
        worker_id=worker_id,
        goal=goal,
        status=artifact.status,
        summary=artifact.summary,
        files=list(artifact.relevant_files),
        findings=list(artifact.findings),
        citations=list(artifact.citations),
        proposed_tasks=list(artifact.proposed_tasks),
        input_tokens=artifact.input_tokens,
        output_tokens=artifact.output_tokens,
        duration_ms=int(artifact.duration_ms or 0),
        # 只读 worker 的工具调用也要透传：runtime 用
        # `len(result.tool_calls)` 统计 WorkerFinished.tool_calls。
        # 漏掉这一项会让所有 explore/review worker 看起来一次工具都没用。
        tool_calls=list(artifact.tool_calls),
        llm_ms=artifact.llm_ms,
        tool_ms=artifact.tool_ms,
        timed_out=artifact.timed_out,
        budget_used={
            "input_tokens": artifact.input_tokens,
            "output_tokens": artifact.output_tokens,
        },
    )


async def _run_implement(
    task: dict[str, Any],
    *,
    task_id: str,
    role: str,
    goal: str,
    worker_id: str,
    parent_request: str,
    session_brief: str,
    cwd: str,
    config: RunnableConfig | None,
    cancel: Callable[[], bool] | None,
    memories: list[str],
    escalate: bool,
    implement_runner: ImplementRunner | None,
    implement_fallback_runner: ImplementRunner | None,
    instructions: Callable[[], str],
) -> WorkerResult:
    active_runner = implement_runner
    if escalate and implement_fallback_runner is not None:
        active_runner = implement_fallback_runner
    if active_runner is None:
        return WorkerResult(
            task_id=task_id,
            role=role,
            worker_id=worker_id,
            goal=goal,
            status="failed",
            summary="实现 worker 未配置（需要开启写编排 + --write + 干净 git 仓库）",
        )
    return await active_runner(
        task,
        parent_request=parent_request,
        cwd=cwd,
        config=config,
        cancel=cancel,
        session_brief=session_brief,
        memories=memories,
        instructions=instructions(),
    )


async def _run_document(
    task: dict[str, Any],
    *,
    task_id: str,
    role: str,
    goal: str,
    worker_id: str,
    parent_request: str,
    session_brief: str,
    cwd: str,
    config: RunnableConfig | None,
    cancel: Callable[[], bool] | None,
    dependency_context: dict[str, Any] | None,
    memories: list[str],
    document_runner: Callable[..., Awaitable[WorkerResult]] | None,
    vision_runner: Callable[..., Awaitable[WorkerResult]] | None,
    author_runner: Callable[..., Awaitable[WorkerResult]] | None,
    instructions: Callable[[], str],
) -> WorkerResult:
    runner = {
        "document": document_runner,
        "vision": vision_runner,
        "author": author_runner,
    }.get(role)
    if runner is None:
        return WorkerResult(
            task_id=task_id,
            role=role,
            worker_id=worker_id,
            goal=goal,
            status="failed",
            summary=f"{role} worker 未配置",
        )
    return await runner(
        task,
        parent_request=parent_request,
        session_brief=session_brief,
        cwd=cwd,
        config=config,
        cancel=cancel,
        dependency_context=dependency_context or {},
        memories=memories,
        instructions=instructions(),
    )


async def _run_test(
    *,
    task_id: str,
    role: str,
    goal: str,
    worker_id: str,
    sandbox: WslSandbox,
    root: str,
    cancel: Callable[[], bool] | None,
) -> WorkerResult:
    if cancel is not None and cancel():
        return WorkerResult(
            task_id=task_id,
            role=role,
            worker_id=worker_id,
            goal=goal,
            status="cancelled",
            summary="已按用户要求取消。",
        )
    result = await asyncio.to_thread(
        run_verification, sandbox, root, allow_manifest=False
    )
    if result.status == "ok":
        status = "success"
    elif result.status == "failed":
        status = "failed"
    else:
        # not_configured / skipped：没验证，不等于通过
        status = "partial"
    findings = [
        f"{issue.location} {issue.message}".strip()
        for issue in result.issues
    ]
    if result.failed_tests:
        findings.append("失败测试：" + "、".join(result.failed_tests))
    return WorkerResult(
        task_id=task_id,
        role=role,
        worker_id=worker_id,
        goal=goal,
        status=status,
        summary=result.summary or result.render(),
        findings=findings,
        verification=result.model_dump(),
        duration_ms=result.duration_ms or 0,
    )


async def _run_worker(
    task: dict[str, Any],
    *,
    parent_request: str,
    session_brief: str,
    cwd: str,
    config: RunnableConfig | None,
    cancel: Callable[[], bool] | None,
    dependency_context: dict[str, Any] | None,
    explore_runner: DelegateRunner | None,
    review_runner: DelegateRunner | None,
    implement_runner: ImplementRunner | None,
    explore_fallback_runner: DelegateRunner | None,
    review_fallback_runner: DelegateRunner | None,
    implement_fallback_runner: ImplementRunner | None,
    document_runner: Callable[..., Awaitable[WorkerResult]] | None,
    vision_runner: Callable[..., Awaitable[WorkerResult]] | None,
    author_runner: Callable[..., Awaitable[WorkerResult]] | None,
    report_review_runner: Callable[..., Awaitable[WorkerResult]] | None,
    sandbox: WslSandbox,
    root: str,
    memory: Any,
    memory_top_k: int,
    instructions: Callable[[], str],
) -> WorkerResult:
    task_id = str(task.get("id", ""))
    role = str(task.get("role", "explore"))
    goal = str(task.get("goal", ""))
    retry_note = format_retry_context(task)
    worker_id = f"{role}:{task_id}"
    memories = _search_memories(memory, task, parent_request, memory_top_k)
    escalate = bool(task.get("escalate"))

    if role in {"explore", "review"}:
        return await _run_readonly(
            task,
            task_id=task_id,
            role=role,
            goal=goal,
            worker_id=worker_id,
            retry_note=retry_note,
            parent_request=parent_request,
            session_brief=session_brief,
            cwd=cwd,
            config=config,
            cancel=cancel,
            dependency_context=dependency_context,
            memories=memories,
            escalate=escalate,
            explore_runner=explore_runner,
            review_runner=review_runner,
            explore_fallback_runner=explore_fallback_runner,
            review_fallback_runner=review_fallback_runner,
            report_review_runner=report_review_runner,
            instructions=instructions,
        )

    if role == "implement":
        return await _run_implement(
            task,
            task_id=task_id,
            role=role,
            goal=goal,
            worker_id=worker_id,
            parent_request=parent_request,
            session_brief=session_brief,
            cwd=cwd,
            config=config,
            cancel=cancel,
            memories=memories,
            escalate=escalate,
            implement_runner=implement_runner,
            implement_fallback_runner=implement_fallback_runner,
            instructions=instructions,
        )

    if role in {"document", "vision", "author"}:
        return await _run_document(
            task,
            task_id=task_id,
            role=role,
            goal=goal,
            worker_id=worker_id,
            parent_request=parent_request,
            session_brief=session_brief,
            cwd=cwd,
            config=config,
            cancel=cancel,
            dependency_context=dependency_context,
            memories=memories,
            document_runner=document_runner,
            vision_runner=vision_runner,
            author_runner=author_runner,
            instructions=instructions,
        )

    if role == "test":
        return await _run_test(
            task_id=task_id,
            role=role,
            goal=goal,
            worker_id=worker_id,
            sandbox=sandbox,
            root=root,
            cancel=cancel,
        )

    return WorkerResult(
        task_id=task_id,
        role=role,
        worker_id=worker_id,
        goal=goal,
        status="failed",
        summary=f"Phase 3A 不支持 worker 角色：{role}",
    )


def make_worker_runner(
    *,
    explore_runner: DelegateRunner | None,
    review_runner: DelegateRunner | None = None,
    implement_runner: ImplementRunner | None = None,
    explore_fallback_runner: DelegateRunner | None = None,
    review_fallback_runner: DelegateRunner | None = None,
    implement_fallback_runner: ImplementRunner | None = None,
    document_runner: Callable[..., Awaitable[WorkerResult]] | None = None,
    vision_runner: Callable[..., Awaitable[WorkerResult]] | None = None,
    author_runner: Callable[..., Awaitable[WorkerResult]] | None = None,
    report_review_runner: Callable[..., Awaitable[WorkerResult]] | None = None,
    sandbox: WslSandbox,
    root: str,
    memory_top_k: int = 8,
    settings: Settings | None = None,
) -> WorkerRunner:
    # 并发 worker 共享同一份记忆读取；只读文件，不持有业务状态。
    # 这里用**独立的** ReadCache：工作区与记忆根目录的相对路径可能同名
    # （例如都有一个 AGENTS.md），与主 agent 的 workspace cache 混用会串内容。
    # implement worker 则在独立 worktree 中，内容可能与主工作区不同，保留各自缓存。
    effective = settings or Settings(_env_file=None)
    memory_cache = ReadCache(effective.read_cache_max_bytes)
    memory_root = resolve_memory_root(effective, sandbox, root)
    project_memory = LongTermMemory(
        SandboxFs(sandbox, memory_root, cache=memory_cache),
        root,
        state_root=memory_root,
    )
    global_root = resolve_global_memory_root(effective, sandbox)
    global_memory = (
        LongTermMemory(
            SandboxFs(sandbox, global_root, cache=memory_cache),
            global_root,
            state_root=global_root,
        )
        if global_root
        else None
    )
    memory = wrap_memory(
        LayeredMemory(project_memory, global_memory), effective
    )
    instructions = _make_instructions_loader(
        sandbox, root, effective.trust_repo_instructions
    )

    async def run(
        task: dict[str, Any],
        *,
        parent_request: str,
        session_brief: str = "",
        cwd: str,
        config: RunnableConfig | None = None,
        cancel: Callable[[], bool] | None = None,
        dependency_context: dict[str, Any] | None = None,
    ) -> WorkerResult:
        return await _run_worker(
            task,
            parent_request=parent_request,
            session_brief=session_brief,
            cwd=cwd,
            config=config,
            cancel=cancel,
            dependency_context=dependency_context,
            explore_runner=explore_runner,
            review_runner=review_runner,
            implement_runner=implement_runner,
            explore_fallback_runner=explore_fallback_runner,
            review_fallback_runner=review_fallback_runner,
            implement_fallback_runner=implement_fallback_runner,
            document_runner=document_runner,
            vision_runner=vision_runner,
            author_runner=author_runner,
            report_review_runner=report_review_runner,
            sandbox=sandbox,
            root=root,
            memory=memory,
            memory_top_k=memory_top_k,
            instructions=instructions,
        )

    return run
