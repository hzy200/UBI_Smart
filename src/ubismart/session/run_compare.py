"""同一会话的运行切分与对比（纯函数）。

审计记录是运行边界与用量的唯一事实源：RUN_START / RUN_END / RUN_ERROR
之间归为一次运行；FILE_CHANGE 的 `snapshot_id` 用于按运行取 diff。
"""

from __future__ import annotations

from typing import Any

from ubismart.audit import models as audit_models


def _new_run(record: dict[str, Any], index: int) -> dict[str, Any]:
    return {
        "index": index,
        "run_epoch": str(record.get("run_epoch") or ""),
        "started_at": str(record.get("ts") or ""),
        "ended_at": "",
        "status": "running",
        "prompt": str(record.get("detail") or ""),
        "answer": "",
        "error": "",
        "provider": str(record.get("provider") or ""),
        "model": str(record.get("model") or ""),
        "tool_calls": 0,
        "failed_tools": 0,
        "files": {},
        "input_tokens": 0,
        "output_tokens": 0,
        "worker_input_tokens": 0,
        "worker_output_tokens": 0,
        "cost_usd": 0.0,
    }


def _finish(run: dict[str, Any], status: str) -> dict[str, Any]:
    if run["status"] == "running":
        run["status"] = status
    run["total_tokens"] = (
        int(run["input_tokens"] or 0)
        + int(run["output_tokens"] or 0)
        + int(run["worker_input_tokens"] or 0)
        + int(run["worker_output_tokens"] or 0)
    )
    run["files"] = sorted(
        run["files"].values(), key=lambda item: item["path"]
    )
    return run


def _apply_tool_call(current: dict[str, Any], record: dict[str, Any]) -> None:
    current["tool_calls"] = int(current["tool_calls"]) + 1
    if record.get("ok") is False:
        current["failed_tools"] = int(current["failed_tools"]) + 1


def _apply_file_change(
    current: dict[str, Any], record: dict[str, Any]
) -> None:
    path = str(record.get("path") or "")
    if not path:
        return
    item = current["files"].get(path)
    if item is None:
        item = {
            "path": path,
            "snapshot_id": str(record.get("snapshot_id") or ""),
            "action": str(record.get("action") or ""),
            "added": 0,
            "removed": 0,
        }
        current["files"][path] = item
    elif not item["snapshot_id"]:
        item["snapshot_id"] = str(record.get("snapshot_id") or "")
    item["added"] = int(item["added"]) + int(record.get("added") or 0)
    item["removed"] = int(item["removed"]) + int(
        record.get("removed") or 0
    )


def _apply_plan(current: dict[str, Any], record: dict[str, Any]) -> None:
    steps = record.get("steps")
    if isinstance(steps, list) and steps:
        current["plan"] = [str(step) for step in steps][:20]


def _apply_run_end(current: dict[str, Any], record: dict[str, Any]) -> None:
    kind = str(record.get("kind") or "")
    current["ended_at"] = str(record.get("ts") or "")
    if kind == audit_models.RUN_ERROR:
        current["status"] = "failed"
        current["error"] = str(record.get("detail") or "")[:2000]
    else:
        current["status"] = "ok"
        current["answer"] = str(record.get("detail") or "")[:4000]
    current["input_tokens"] = int(record.get("input_tokens") or 0)
    current["output_tokens"] = int(record.get("output_tokens") or 0)
    current["worker_input_tokens"] = int(
        record.get("worker_input_tokens") or 0
    )
    current["worker_output_tokens"] = int(
        record.get("worker_output_tokens") or 0
    )
    current["cost_usd"] = float(record.get("cost_usd") or 0.0)
    current["provider"] = str(
        record.get("provider") or current["provider"]
    )
    current["model"] = str(record.get("model") or current["model"])


def _apply_record(current: dict[str, Any], record: dict[str, Any]) -> bool:
    """把一条记录并入当前运行；返回该运行是否在此记录上收尾。"""
    kind = str(record.get("kind") or "")
    if kind == audit_models.TOOL_CALL:
        _apply_tool_call(current, record)
    elif kind == audit_models.FILE_CHANGE:
        _apply_file_change(current, record)
    elif kind == audit_models.PLAN:
        _apply_plan(current, record)
    elif kind in (audit_models.RUN_END, audit_models.RUN_ERROR):
        _apply_run_end(current, record)
        return True
    return False


def build_runs(
    records: list[dict[str, Any]], *, limit: int = 20
) -> list[dict[str, Any]]:
    """把按时间排序的审计记录切成运行；返回最近 limit 次。"""
    runs: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    index = 0
    for record in records or []:
        kind = str(record.get("kind") or "")
        if kind == audit_models.RUN_START:
            if current is not None:
                runs.append(_finish(current, "interrupted"))
            index += 1
            current = _new_run(record, index)
            continue
        if current is None:
            continue
        if _apply_record(current, record):
            runs.append(_finish(current, current["status"]))
            current = None
    if current is not None:
        runs.append(_finish(current, "interrupted"))
    size = max(1, int(limit))
    return runs[-size:]


def compare_runs(
    runs: list[dict[str, Any]], left: int, right: int
) -> dict[str, Any]:
    """按运行序号（1 起）返回并排对比元数据；diff 由路由补。"""
    by_index = {int(run["index"]): run for run in runs}
    left_run = by_index.get(int(left))
    right_run = by_index.get(int(right))
    if left_run is None or right_run is None:
        raise KeyError("运行序号不存在")
    left_files = {item["path"]: item for item in left_run["files"]}
    right_files = {item["path"]: item for item in right_run["files"]}
    files = []
    for path in sorted(set(left_files) | set(right_files)):
        files.append(
            {
                "path": path,
                "left": left_files.get(path),
                "right": right_files.get(path),
            }
        )
    return {
        "left": left_run,
        "right": right_run,
        "files": files,
        "usage": {
            "left": {
                "tokens": left_run.get("total_tokens", 0),
                "cost_usd": left_run.get("cost_usd", 0),
                "tool_calls": left_run.get("tool_calls", 0),
                "failed_tools": left_run.get("failed_tools", 0),
            },
            "right": {
                "tokens": right_run.get("total_tokens", 0),
                "cost_usd": right_run.get("cost_usd", 0),
                "tool_calls": right_run.get("tool_calls", 0),
                "failed_tools": right_run.get("failed_tools", 0),
            },
        },
    }
