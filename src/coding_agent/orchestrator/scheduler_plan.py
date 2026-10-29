"""调度计划层的纯函数：任务图上的判断与改写。

这些函数不碰运行态（没有 in-flight、没有堆、不发事件），只吃 `tasks` 与
`status` 两个字典。抽出来的直接原因是**断开一个环**：

    scheduler.py        →  scheduler_run.py   （节点工厂里实例化运行态）
    scheduler_run.py    →  scheduler.py       （要下面这几个纯函数）

两者互相 import，只靠"某一边写在函数里"才没炸。把纯函数下沉之后，
`scheduler_run → scheduler_plan` 是普通依赖，`scheduler → scheduler_run`
只剩一条单向边，模块级图重新变成 DAG（见
`tests/unit/test_architecture.py::test_dependency_graph_has_no_new_cycles`）。
"""

from __future__ import annotations

from typing import Any

from coding_agent.orchestrator.contracts import WorkerResult


def deps_met(task: dict[str, Any], status: dict[str, str]) -> bool:
    for dep in task.get("depends_on") or []:
        value = status.get(str(dep))
        if value == "success":
            continue
        if value == "partial" and task.get("depends_mode") == "any":
            continue
        return False
    return True


def wire_implement_dependents(tasks: list[dict[str, Any]]) -> None:
    """把 test/review 自动挂到 implement 之后。

    否则它们会和写 worker 同批启动，看到的是合并前状态，产出天然是
    partial/blocked。`any` 允许消费 partial 的写产出。
    """
    implement_ids = [
        str(task["id"]) for task in tasks if task.get("role") == "implement"
    ]
    if not implement_ids:
        return
    for task in tasks:
        if task.get("role") not in {"test", "review"}:
            continue
        if task.get("depends_on"):
            continue
        task["depends_on"] = list(implement_ids)
        task["depends_mode"] = "any"


def task_depth(tasks: list[dict[str, Any]], task_id: str) -> int:
    by_id = {str(task["id"]): task for task in tasks}
    seen: set[str] = set()

    def walk(node: str) -> int:
        if node in seen:
            return 0
        seen.add(node)
        deps = by_id.get(node, {}).get("depends_on") or []
        return 1 + max((walk(str(dep)) for dep in deps), default=0)

    return walk(task_id)


def retry_context(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "attempt": int(result.get("attempt", 1)),
        "status": str(result.get("status", "")),
        "summary": str(result.get("summary", ""))[:1_000],
        "error": str(result.get("error", ""))[:1_000],
        "findings": [str(item) for item in (result.get("findings") or [])[:8]],
        "verification": result.get("verification") or {},
    }


def fallback_result(
    by_id: dict[str, dict[str, Any]],
    task_id: str,
    status: str,
    summary: str,
    *,
    error: str = "",
    timed_out: bool = False,
) -> WorkerResult:
    """worker 任务异常/取消时补一条结构化结果，保证事件与状态不缺记录。"""
    task = by_id.get(task_id, {})
    role = str(task.get("role", "explore"))
    return WorkerResult(
        task_id=task_id,
        role=role,
        worker_id=f"{role}:{task_id}",
        goal=str(task.get("goal", "")),
        status=status,
        summary=summary,
        error=error,
        timed_out=timed_out,
    )
