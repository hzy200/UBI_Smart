"""跑偏轨迹回归集：把 ABORT / 连续验证失败 / 预算触顶 / 重复动作记下来。

只记录结构化摘要与指标，不记录提示词、工具原文或密钥。回归集用于验证
“更早拦住且不误杀”，不是另一个用户可见日志。
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from ubismart.platform.filelock import FileLock
from ubismart.platform.observability import note_silent_failure


def record_trajectory(
    path: str | Path,
    *,
    kind: str,
    thread_id: str = "",
    task_id: str = "",
    stage: int = 0,
    reason: str = "",
    metrics: Mapping[str, Any] | None = None,
) -> None:
    target = Path(path)
    payload = {
        "kind": str(kind),
        "thread_id": str(thread_id),
        "task_id": str(task_id),
        "stage": int(stage or 0),
        "reason": str(reason)[:500],
        "metrics": dict(metrics or {}),
        "created_at": int(time.time() * 1000),
    }
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(target.with_suffix(target.suffix + ".lock")):
            with target.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
    except OSError:
        note_silent_failure("regression_record")


def load_trajectories(path: str | Path) -> list[dict[str, Any]]:
    target = Path(path)
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    items: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            items.append(item)
    return items


def summarize_trajectories(items: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        kind = str(item.get("kind", ""))
        if kind:
            counts[kind] = counts.get(kind, 0) + 1
    return counts
