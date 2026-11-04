"""任务图校验与 ready 队列。

校验失败**不抛异常**：编排器降级为单个 explore 任务，并让事件/审计把
`degraded` 显示出来。模型给出的图不可信，不能让它拖垮整次运行。
"""

from __future__ import annotations

import posixpath
from typing import Any

from ubismart.orchestrator.contracts import TaskSpec

ALLOWED_ROLES = frozenset(
    {
        "explore",
        "test",
        "review",
        "implement",
        "document",
        "vision",
        "author",
        "sub_orchestrator",
    }
)


def default_task(request: str) -> dict[str, Any]:
    return {
        "id": "explore-1",
        "role": "explore",
        "goal": request or "理解当前工作区",
        "inputs": [],
        "acceptance": [],
        "depends_on": [],
    }


def _has_cycle(tasks: list[dict[str, Any]]) -> bool:
    by_id = {task["id"]: task for task in tasks}
    visiting: set[str] = set()
    done: set[str] = set()

    def visit(node: str) -> bool:
        if node in done:
            return False
        if node in visiting:
            return True
        visiting.add(node)
        for dep in by_id[node].get("depends_on") or []:
            if dep in by_id and visit(dep):
                return True
        visiting.remove(node)
        done.add(node)
        return False

    return any(visit(task_id) for task_id in by_id)


def _normalize_scope(scope: str) -> str:
    text = str(scope).strip().replace("\\", "/")
    if not text or text.startswith("/") or ".." in text.split("/"):
        return ""
    normalized = posixpath.normpath(text)
    return "" if normalized in {"", "."} else normalized


def _scope_contains(path: str, scope: str) -> bool:
    normalized = posixpath.normpath(path.replace("\\", "/"))
    return normalized == scope or normalized.startswith(scope.rstrip("/") + "/")


def _scopes_overlap(left: str, right: str) -> bool:
    return _scope_contains(left, right) or _scope_contains(right, left)


def _coerce_raw_tasks(
    raw: list[TaskSpec | dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    for item in raw or []:
        if isinstance(item, TaskSpec):
            tasks.append(item.model_dump())
        elif isinstance(item, dict):
            tasks.append(dict(item))
    return tasks


def _normalize_depends_mode(
    task: dict[str, Any], task_id: str, errors: list[str]
) -> str:
    mode = str(task.get("depends_mode", "full")).strip().lower()
    if mode not in {"full", "any"}:
        errors.append(f"任务 {task_id or '?'} 的 depends_mode 非法：{mode}")
        mode = "full"
    return mode


def _normalize_stage(
    task: dict[str, Any], task_id: str, errors: list[str]
) -> int:
    try:
        return max(0, int(task.get("stage", 0) or 0))
    except (TypeError, ValueError):
        errors.append(f"任务 {task_id or '?'} 的 stage 非法")
        return 0


def _normalize_author_fields(
    task: dict[str, Any], task_id: str, errors: list[str]
) -> None:
    output_format = str(task.get("output_format", "")).strip().lower()
    if output_format not in {"", "md", "docx", "pdf"}:
        errors.append(
            f"任务 {task_id or '?'} 的 output_format 非法：{output_format}"
        )
        output_format = "md"
    task["output_format"] = output_format or "md"
    task["output_path"] = str(task.get("output_path", "")).strip()
    task["template"] = str(task.get("template", "")).strip()
    task["sources"] = [
        dict(item)
        for item in (task.get("sources") or [])
        if isinstance(item, dict)
    ]


def _normalize_task_budgets(task: dict[str, Any]) -> None:
    # 任务级预算：模型给的值只做归一化，真正的上限由宿主在 runner 里
    # 与全局配置取小；非法值直接丢弃，避免把 worker 预算设成负数。
    for budget_key in (
        "max_repairs",
        "max_rounds",
        "timeout",
        "max_tokens",
        "priority",
    ):
        raw_value = task.get(budget_key)
        if raw_value is None:
            continue
        try:
            task[budget_key] = max(0, int(raw_value))
        except (TypeError, ValueError):
            task.pop(budget_key, None)


def _normalize_acceptance_refs(task: dict[str, Any]) -> list[int]:
    refs: list[int] = []
    for raw in task.get("acceptance_refs") or []:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value >= 0 and value not in refs:
            refs.append(value)
    return refs


def _normalize_write_scope(
    task: dict[str, Any],
    task_id: str,
    role: str,
    errors: list[str],
) -> None:
    raw_scope = [str(v) for v in (task.get("write_scope") or [])]
    scope = [_normalize_scope(item) for item in raw_scope]
    scope = [item for item in scope if item]
    task["write_scope"] = scope
    if role in {"implement", "author"}:
        if not scope:
            errors.append(
                f"任务 {task_id or '?'} 是 {role}，但没有 write_scope"
            )
    elif scope:
        errors.append(f"任务 {task_id or '?'} 是只读角色，却声明了 write_scope")


def _normalize_task(
    task: dict[str, Any],
    ids: list[str],
    errors: list[str],
    *,
    allow_write: bool,
    allow_author: bool,
) -> None:
    task_id = str(task.get("id", "")).strip()
    goal = str(task.get("goal", "")).strip()
    role = str(task.get("role", "explore"))
    if not task_id:
        errors.append("任务 id 为空")
    if task_id in ids:
        errors.append(f"任务 id 重复：{task_id}")
    ids.append(task_id)
    if not goal:
        errors.append(f"任务 {task_id or '?'} 没有 goal")
    if role not in ALLOWED_ROLES:
        errors.append(f"任务 {task_id or '?'} 的角色不允许：{role}")
    if role == "implement" and not allow_write:
        errors.append(f"任务 {task_id or '?'} 是 implement，但当前未启用写编排")
    if role == "author" and not allow_author:
        errors.append(f"任务 {task_id or '?'} 是 author，但当前没有写权限")
    task["id"] = task_id
    task["goal"] = goal
    task["role"] = role
    task["inputs"] = [str(v) for v in (task.get("inputs") or [])]
    task["acceptance"] = [str(v) for v in (task.get("acceptance") or [])]
    task["depends_on"] = [str(v) for v in (task.get("depends_on") or [])]
    task["acceptance_refs"] = _normalize_acceptance_refs(task)
    task["constraints"] = [
        str(v) for v in (task.get("constraints") or []) if str(v).strip()
    ]
    known = task.get("known")
    task["known"] = dict(known) if isinstance(known, dict) else {}
    task["unknown"] = [
        str(v) for v in (task.get("unknown") or []) if str(v).strip()
    ]
    task["side_effects"] = [
        dict(item)
        for item in (task.get("side_effects") or [])
        if isinstance(item, dict)
    ]
    task["verifiable"] = bool(task.get("verifiable", True))
    branching = str(task.get("branching", "none")).strip().lower()
    task["branching"] = (
        branching if branching in {"none", "bounded", "unbounded"} else "none"
    )
    sub_contract = task.get("sub_contract")
    task["sub_contract"] = (
        dict(sub_contract) if isinstance(sub_contract, dict) else None
    )
    budget_slice = task.get("budget_slice")
    task["budget_slice"] = (
        {
            str(key): max(0, int(value))
            for key, value in budget_slice.items()
            if str(key) in {"max_tokens", "max_wall_ms"}
            and str(value).isdigit()
        }
        if isinstance(budget_slice, dict)
        else {}
    )
    if role == "sub_orchestrator":
        if not task.get("sub_contract"):
            errors.append(f"任务 {task_id or '?'} 是子编排器，但没有 sub_contract")
        if task.get("write_scope"):
            errors.append(
                f"任务 {task_id or '?'} 是子编排器，不能被当作写任务"
            )
    # implement 的 depends_mode 在依赖关系解析后统一校验：顺序写下
    # implement→implement 依赖必须是 any（partial 也可能已合并）。
    task["depends_mode"] = _normalize_depends_mode(task, task_id, errors)
    task["stage"] = _normalize_stage(task, task_id, errors)
    if role == "author":
        _normalize_author_fields(task, task_id, errors)
    _normalize_task_budgets(task)
    _normalize_write_scope(task, task_id, role, errors)


def _validate_dependency_refs(
    tasks: list[dict[str, Any]], known: set[str], errors: list[str]
) -> None:
    for task in tasks:
        deps = task.get("depends_on") or []
        for dep in deps:
            if dep == task["id"]:
                errors.append(f"任务 {task['id']} 依赖自己")
            elif dep not in known:
                errors.append(f"任务 {task['id']} 依赖不存在的 {dep}")


def _validate_stage_limit(
    tasks: list[dict[str, Any]],
    errors: list[str],
    *,
    allow_sequential_write: bool,
    max_write_stages: int | None,
) -> None:
    if not allow_sequential_write or max_write_stages is None:
        return
    for task in tasks:
        if task["role"] != "implement":
            continue
        if int(task.get("stage", 0) or 0) > int(max_write_stages):
            errors.append(
                f"任务 {task['id']} 的 stage {task['stage']} "
                f"超过写波次上限 {max_write_stages}"
            )


def _validate_implement_dependencies(
    tasks: list[dict[str, Any]],
    by_id: dict[str, dict[str, Any]],
    errors: list[str],
    *,
    allow_sequential_write: bool,
) -> None:
    # implement 默认必须互相独立：worktree 都基于同一个 HEAD，若一个依赖
    # 另一个，后者的 worktree 看不到前者的改动。开启顺序写后，只允许依赖
    # stage 严格更小的 implement（前一波已合并，worktree 会被注入基线）。
    for task in tasks:
        if task["role"] != "implement":
            continue
        for dep in task.get("depends_on") or []:
            dep_task = by_id.get(dep) or {}
            if dep_task.get("role") != "implement":
                continue
            if not allow_sequential_write:
                errors.append(
                    f"任务 {task['id']} 依赖另一个 implement {dep}；"
                    "写任务必须独立，不能互相依赖"
                )
                continue
            try:
                dep_stage = int(dep_task.get("stage", 0) or 0)
                task_stage = int(task.get("stage", 0) or 0)
            except (TypeError, ValueError):
                dep_stage = task_stage = 0
            if dep_stage >= task_stage:
                errors.append(
                    f"任务 {task['id']} 依赖 implement {dep}，"
                    f"但 stage 必须严格更小（{dep_stage} >= {task_stage}）"
                )


def _normalize_implement_depends_modes(
    tasks: list[dict[str, Any]],
    by_id: dict[str, dict[str, Any]],
    errors: list[str],
    *,
    allow_sequential_write: bool,
) -> None:
    # 顺序写：implement→implement 依赖用 any。partial 表示补丁已经合并、
    # 只是 worker 内部验证未通过；下一波仍应基于这个合并结果继续。
    for task in tasks:
        if task["role"] != "implement":
            continue
        implement_deps = [
            dep
            for dep in task.get("depends_on") or []
            if by_id.get(str(dep), {}).get("role") == "implement"
        ]
        if implement_deps and allow_sequential_write:
            task["depends_mode"] = "any"
        elif str(task.get("depends_mode")) != "full":
            errors.append(
                f"任务 {task['id']} 是 implement，depends_mode 必须是 full"
            )
            task["depends_mode"] = "full"


def _validate_role_limits(
    tasks: list[dict[str, Any]],
    errors: list[str],
    *,
    max_documents: int | None,
    max_images: int | None,
) -> None:
    authors = [task for task in tasks if task.get("role") == "author"]
    if len(authors) > 3:
        errors.append("author 任务最多 3 个（显式多格式时每种格式一个）")
    author_paths = [
        str(task.get("output_path") or "")
        for task in authors
        if str(task.get("output_path") or "")
    ]
    if len(author_paths) != len(set(author_paths)):
        errors.append("author 任务的 output_path 不能重复")
    documents = [task for task in tasks if task.get("role") == "document"]
    images = [task for task in tasks if task.get("role") == "vision"]
    if max_documents is not None and len(documents) > max_documents:
        errors.append(
            f"document 任务数 {len(documents)} 超过上限 {max_documents}"
        )
    if max_images is not None and len(images) > max_images:
        errors.append(
            f"vision 任务数 {len(images)} 超过上限 {max_images}"
        )


def _validate_write_overlaps(
    tasks: list[dict[str, Any]],
    errors: list[str],
    *,
    allow_overlap: bool,
    allow_sequential_write: bool,
) -> None:
    # 默认并发写范围必须两两不重叠；开启 allow_overlap 后交给 3-way 合并。
    if allow_overlap:
        return
    implement = [task for task in tasks if task["role"] == "implement"]
    for index, left in enumerate(implement):
        for right in implement[index + 1 :]:
            # 顺序写允许跨 stage 的 scope 重叠（后一波本来就基于前一波）；
            # 同 stage 仍必须两两不重叠。
            if allow_sequential_write and int(
                left.get("stage", 0) or 0
            ) != int(right.get("stage", 0) or 0):
                continue
            for left_scope in left.get("write_scope") or []:
                for right_scope in right.get("write_scope") or []:
                    if _scopes_overlap(left_scope, right_scope):
                        errors.append(
                            f"写范围重叠：{left['id']} {left_scope} 与 "
                            f"{right['id']} {right_scope}"
                        )


def validate_tasks(
    raw: list[TaskSpec | dict[str, Any]] | None,
    *,
    request: str,
    max_tasks: int,
    allow_write: bool = False,
    allow_overlap: bool = False,
    allow_author: bool = False,
    max_documents: int | None = None,
    max_images: int | None = None,
    allow_sequential_write: bool = False,
    max_write_stages: int | None = None,
) -> tuple[list[dict[str, Any]], bool, list[str]]:
    """把模型输出归一化为任务列表。

    返回 `(tasks, degraded, errors)`。任何结构问题都退化为单 explore 任务，
    不做部分采纳 —— 半张图比明确的降级更难排查。
    """
    tasks = _coerce_raw_tasks(raw)
    errors: list[str] = []
    if not tasks:
        errors.append("任务图为空")
    # **先检查原始长度再截断**：先截断的话这个分支永远不可能触发，
    # 超限的任务图会被静默丢弃一部分。
    if len(tasks) > max_tasks:
        errors.append(f"任务数 {len(tasks)} 超过上限 {max_tasks}")
    tasks = tasks[: max(max_tasks, 1)]

    ids: list[str] = []
    for task in tasks:
        _normalize_task(
            task,
            ids,
            errors,
            allow_write=allow_write,
            allow_author=allow_author,
        )

    for index, task in enumerate(
        [task for task in tasks if task.get("role") == "sub_orchestrator"]
    ):
        task["fanout_index"] = index
        task["sub_spawn_mode"] = "static"

    known = set(ids)
    _validate_dependency_refs(tasks, known, errors)

    by_id = {task["id"]: task for task in tasks}
    _validate_stage_limit(
        tasks,
        errors,
        allow_sequential_write=allow_sequential_write,
        max_write_stages=max_write_stages,
    )
    _validate_implement_dependencies(
        tasks,
        by_id,
        errors,
        allow_sequential_write=allow_sequential_write,
    )
    _normalize_implement_depends_modes(
        tasks,
        by_id,
        errors,
        allow_sequential_write=allow_sequential_write,
    )
    _validate_role_limits(
        tasks,
        errors,
        max_documents=max_documents,
        max_images=max_images,
    )
    _validate_write_overlaps(
        tasks,
        errors,
        allow_overlap=allow_overlap,
        allow_sequential_write=allow_sequential_write,
    )

    if not errors and _has_cycle(tasks):
        errors.append("任务图存在环")

    if errors:
        return [default_task(request)], True, errors
    return tasks, False, []


_PROPOSAL_ROLES = frozenset({"explore", "test", "review", "document", "vision"})


def _goal_key(text: Any) -> str:
    return " ".join(str(text).strip().lower().split())


def validate_proposals(
    raw: list[dict[str, Any]] | None,
    *,
    existing: list[dict[str, Any]],
    max_total: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    """把 worker 提议的只读任务归一化为可调度任务。

    worker 只拿到自己的任务，不知道整张图，所以这里必须重新做 host 侧边界：
    只读角色、非空 goal、去重、依赖存在、总量上限、无环。任何不合规的提议
    丢弃并记录原因，**不让 worker 的自由文本直接影响写路径**。
    """
    ids = {str(task.get("id", "")) for task in existing}
    goals = {_goal_key(task.get("goal", "")) for task in existing}
    errors: list[str] = []
    accepted: list[dict[str, Any]] = []
    max_new = max(int(max_total) - len(existing), 0)

    for item in raw or []:
        if len(accepted) >= max_new:
            errors.append(
                f"追加任务达到总上限 {max_total}，其余提议已忽略"
            )
            break
        role = str(item.get("role", "explore")).strip().lower()
        goal = str(item.get("goal", "")).strip()
        if role not in _PROPOSAL_ROLES:
            errors.append(f"拒绝非只读提议（role={role}）：{goal}")
            continue
        if not goal:
            errors.append("拒绝空 goal 的任务提议")
            continue
        key = _goal_key(goal)
        if key in goals:
            errors.append(f"重复任务提议已忽略：{goal}")
            continue
        if item.get("write_scope"):
            errors.append(f"只读提议不允许 write_scope：{goal}")
            continue
        deps = [
            str(dep).strip()
            for dep in (item.get("depends_on") or [])
            if str(dep).strip()
        ]
        unknown = [dep for dep in deps if dep not in ids]
        if unknown:
            errors.append(
                f"任务提议依赖不存在的 id {unknown}，已忽略：{goal}"
            )
            continue

        index = 1
        while f"ext-{index}" in ids:
            index += 1
        task_id = f"ext-{index}"
        accepted.append(
            {
                "id": task_id,
                "role": role,
                "goal": goal,
                "inputs": [str(v) for v in (item.get("inputs") or [])],
                "acceptance": [
                    str(v) for v in (item.get("acceptance") or [])
                ],
                "depends_on": deps,
                "depends_mode": "full",
                "write_scope": [],
            }
        )
        ids.add(task_id)
        goals.add(key)

    if accepted and _has_cycle(existing + accepted):
        return [], errors + ["追加任务引入了依赖环，全部丢弃"]
    return accepted, errors


def _dependency_met(task: dict[str, Any], dep: str, status: dict[str, str]) -> bool:
    """单个依赖是否已满足。

    `depends_mode=any` 允许消费 partial（例如 review 只需要 implement 的产出，
    不需要它通过完整验证）；failed/blocked/cancelled 任何模式都不满足。
    """
    value = status.get(dep)
    if value == "success":
        return True
    return value == "partial" and task.get("depends_mode") == "any"


def ready_tasks(
    tasks: list[dict[str, Any]], status: dict[str, str]
) -> list[dict[str, Any]]:
    """依赖已满足的 pending 任务。"""
    ready = []
    for task in tasks:
        if status.get(task["id"], "pending") != "pending":
            continue
        deps = task.get("depends_on") or []
        if all(_dependency_met(task, dep, status) for dep in deps):
            ready.append(task)
    return ready


def mark_blocked(
    tasks: list[dict[str, Any]], status: dict[str, str]
) -> dict[str, str]:
    """把依赖 failed/blocked 的 pending 任务标成 blocked（原地返回新 dict）。"""
    updated = dict(status)
    changed = True
    while changed:
        changed = False
        for task in tasks:
            task_id = task["id"]
            if updated.get(task_id, "pending") != "pending":
                continue
            deps = task.get("depends_on") or []
            # failed/blocked/cancelled 任何模式都阻塞；partial 只阻塞
            # depends_mode=full 的任务，review/test 可用 any 继续消费产出。
            mode = str(task.get("depends_mode") or "full")
            if any(
                updated.get(dep) in {"failed", "blocked", "cancelled"}
                or (updated.get(dep) == "partial" and mode != "any")
                for dep in deps
            ):
                updated[task_id] = "blocked"
                changed = True
    return updated
