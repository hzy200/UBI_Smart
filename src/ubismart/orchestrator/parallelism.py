"""任务图的并行收益预估与只读任务去重。

并行不是目的：一条依赖链拆成多个 worker 只会多付上下文与调度成本，
墙钟时间并不会缩短。这里用**确定性规则**回答两个问题：

- 这个图真正能同时跑多宽（stage 宽度 / 关键路径 / 收益比）；
- 有没有完全重复的只读任务（同角色、同目标、同输入）可以安全去掉。

只做保守判断：拿不准时保持原计划；去重只删除**没有任何依赖方**的任务，
因此不需要重写 `depends_on`。
"""

from __future__ import annotations

import posixpath
from collections import Counter
from typing import Any

# 只对只读角色去重。implement 即使目标相同也各自有 write_scope，
# document/vision 的 inputs 是逐文件拆分，交给各自的上限处理。
DEDUP_ROLES = frozenset({"explore", "review", "test"})


def _normalize_text(text: Any) -> str:
    return " ".join(str(text or "").split()).lower()


def _normalize_input(path: Any) -> str:
    value = str(path or "").strip().replace("\\", "/")
    if not value:
        return ""
    return posixpath.normpath(value).lstrip("/")


def task_key(task: dict[str, Any]) -> tuple[str, str, tuple[str, ...]]:
    return (
        str(task.get("role", "")),
        _normalize_text(task.get("goal", "")),
        tuple(sorted(_normalize_input(item) for item in task.get("inputs") or [])),
    )


def dedupe_readonly_tasks(
    tasks: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """去掉完全重复的只读任务；返回 (保留任务, 被删除的 id)。

    只删除没有任何任务依赖的重复项：这样 `depends_on` 不需要改写，
    不会制造悬空依赖。
    """
    dependents = Counter(
        str(dep)
        for task in tasks
        for dep in (task.get("depends_on") or [])
    )
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    kept: list[dict[str, Any]] = []
    dropped: list[str] = []
    for task in tasks:
        task_id = str(task.get("id", ""))
        role = str(task.get("role", ""))
        if role in DEDUP_ROLES:
            key = task_key(task)
            if key in seen and dependents[task_id] == 0:
                dropped.append(task_id)
                continue
            seen.add(key)
        kept.append(task)
    return kept, dropped


def parallel_stats(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    """返回 stage 宽度、关键路径与收益比。

    `gain = task_count / critical_path` 是并行收益的**上界**：链式任务
    gain≈1，彼此独立的任务 gain≈任务数。只用于决定「值不值得并行」，
    不是精确调度模型。
    """
    by_id = {str(task.get("id", "")): task for task in tasks}
    memo: dict[str, int] = {}

    def depth(task_id: str, seen: set[str]) -> int:
        if task_id in memo:
            return memo[task_id]
        if task_id in seen or task_id not in by_id:
            return 0
        seen.add(task_id)
        deps = by_id[task_id].get("depends_on") or []
        value = 1 + max((depth(str(dep), seen) for dep in deps), default=0)
        memo[task_id] = value
        return value

    levels: Counter[int] = Counter()
    for task in tasks:
        levels[depth(str(task.get("id", "")), set())] += 1
    critical_path = max(levels, default=0)
    # 同一依赖层里能同时启动的任务数。只按 `stage` 计数会把依赖链上的
    # 任务也算成可并行；必须按依赖深度分层。
    width = max(levels.values(), default=0)
    count = len(tasks)
    gain = (count / critical_path) if critical_path else float(count)
    return {
        "task_count": count,
        "width": width,
        "critical_path": critical_path,
        "gain": round(gain, 3),
    }


def worth_parallelizing(tasks: list[dict[str, Any]], min_gain: float) -> bool:
    """收益比低于阈值时，并行只增加成本而不缩短墙钟时间。

    `min_gain <= 0` 表示关闭预估，保持原工作流不变。
    """
    if min_gain <= 0:
        return True
    if len(tasks) <= 1:
        return False
    return float(parallel_stats(tasks)["gain"]) >= min_gain
