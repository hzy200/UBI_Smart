"""复杂度信号驱动的动态任务图构建（RFC-0009）。

它不是第二套调度器：只负责在**确定性信号**足够强时直接产出 `TaskSpec`，
仍然交给现有 plan/validate/scheduler。信号不足时返回 None，回退四模板。
"""

from __future__ import annotations

import posixpath
from typing import Any

from ubismart.config import Settings

_MAX_SCOPES_PER_WAVE = 3


def _normalize(path: str) -> str:
    text = str(path or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.rstrip("/")


def _dedupe_scopes(scopes: list[str]) -> list[str]:
    normalized = [_normalize(item) for item in scopes if _normalize(item)]
    # 目录前缀覆盖其下文件，避免同一波次里父目录与子文件重复。
    result: list[str] = []
    for scope in sorted(set(normalized), key=len):
        if any(
            scope == kept or scope.startswith(kept.rstrip("/") + "/")
            for kept in result
        ):
            continue
        result.append(scope)
    return result


def _wave_scopes(wave: dict[str, Any]) -> list[str]:
    files = [_normalize(item) for item in wave.get("files") or []]
    modules = [str(item) for item in wave.get("modules") or []]
    scopes = _dedupe_scopes(files)
    if not scopes and modules:
        scopes = _dedupe_scopes(
            [module.replace(".", "/") + "/" for module in modules]
        )
    if len(scopes) > _MAX_SCOPES_PER_WAVE:
        # 文件太多时退化为共同父目录，保证不丢改动范围。
        parents = {posixpath.dirname(scope) or "." for scope in scopes}
        if len(parents) == 1:
            scopes = [next(iter(parents)) + "/"]
        else:
            scopes = scopes[:_MAX_SCOPES_PER_WAVE]
    return scopes


def build_dynamic_plan(
    request: str,
    summary: dict[str, Any],
    settings: Settings,
    *,
    write_available: bool,
) -> dict[str, Any] | None:
    """有 ChangeContract 且跨波次时，直接生成接口优先的任务图。"""
    if not bool(getattr(settings, "deep_workflows_enabled", False)):
        return None
    if not write_available:
        return None
    contract = summary.get("change_contract") or {}
    waves = [
        wave
        for wave in (contract.get("waves") or [])
        if isinstance(wave, dict)
    ]
    if len(waves) < 2:
        return None
    waves.sort(key=lambda item: int(item.get("stage", 0) or 0))

    max_tasks = max(1, int(settings.orchestrator_max_tasks or 6))
    tasks: list[dict[str, Any]] = []
    previous: list[str] = []
    for wave in waves:
        if len(tasks) >= max_tasks:
            break
        stage = int(wave.get("stage", 0) or 0)
        scopes = _wave_scopes(wave)
        if not scopes:
            continue
        wave_ids: list[str] = []
        for index, scope in enumerate(scopes):
            if len(tasks) >= max_tasks:
                break
            task_id = f"w{stage}-{index + 1}"
            tasks.append(
                {
                    "id": task_id,
                    "role": "implement",
                    "goal": (
                        f"{request}（第 {stage + 1} 波，范围：{scope}）"
                    ),
                    "inputs": [],
                    "acceptance": [
                        "只修改 write_scope 内文件",
                        "保持调用方/接口一致",
                    ],
                    "depends_on": list(previous),
                    "depends_mode": "any" if previous else "full",
                    "write_scope": [scope],
                    "stage": stage,
                }
            )
            wave_ids.append(task_id)
        if wave_ids:
            previous = wave_ids

    if not tasks:
        return None
    tests = [
        str(item) for item in (contract.get("affected_tests") or [])[:40]
    ]
    last_stage = max(int(task.get("stage", 0) or 0) for task in tasks)
    if tests and len(tasks) < max_tasks:
        tasks.append(
            {
                "id": "verify-affected",
                "role": "test",
                "goal": "运行受影响测试并总结失败项",
                "inputs": [],
                "acceptance": ["报告失败测试与原因"],
                "depends_on": [task["id"] for task in tasks],
                "depends_mode": "any",
                "stage": last_stage + 1,
            }
        )
    return {
        "workflow": "architecture_change",
        "reason": (
            f"ChangeContract 检测到 {len(waves)} 个写波次、"
            f"{len(contract.get('affected_modules') or [])} 个受影响模块"
        ),
        "tasks": tasks,
    }
