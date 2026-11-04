"""资料库的进程内指标。

Prometheus 导出不做直方图；这里用有界 deque 计算 search p95 并写 gauge。
所有指标名已在 `platform/observability.py` 登记，标签都是有界枚举。
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Any

from ubismart.platform.observability import (
    inc,
    inc_gauge,
    set_gauge,
)

_LATENCY_LOCK = threading.Lock()
_SEARCH_LATENCIES: dict[str, deque[float]] = {}
_INGEST_LATENCIES: dict[str, deque[float]] = {}
_MAX_SAMPLES = 512


def _safe(call: Any, *args: Any, **kwargs: Any) -> None:
    try:
        call(*args, **kwargs)
    except Exception:  # noqa: BLE001 - 指标失败不能影响检索/任务
        pass


def _p95(bucket: dict[str, deque[float]], key: str, value: float) -> float:
    with _LATENCY_LOCK:
        samples = bucket.setdefault(key, deque(maxlen=_MAX_SAMPLES))
        samples.append(max(0.0, float(value)))
        ordered = sorted(samples)
        return ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]


def record_search(mode: str, duration_ms: float, hit_count: int) -> None:
    mode = str(mode or "lexical")
    _safe(inc, "agent_library_search_total", {"mode": mode, "outcome": "ok"})
    _safe(
        inc,
        "agent_library_search_hits_total",
        {"mode": mode, "hit": "yes" if hit_count > 0 else "no"},
    )
    p95 = _p95(_SEARCH_LATENCIES, mode, duration_ms)
    _safe(set_gauge, "agent_library_search_p95_ms", p95, {"mode": mode})


def record_search_error(mode: str) -> None:
    _safe(
        inc,
        "agent_library_search_total",
        {"mode": str(mode or "lexical"), "outcome": "error"},
    )


def start_job() -> None:
    _safe(inc_gauge, "agent_library_jobs_active", 1)


def finish_job() -> None:
    _safe(inc_gauge, "agent_library_jobs_active", -1)


def set_pending(count: int) -> None:
    _safe(set_gauge, "agent_library_jobs_pending", max(0, int(count)))


def record_job(action: str, outcome: str, duration_ms: float) -> None:
    action = str(action or "unknown")
    _safe(
        inc,
        "agent_library_ingest_total",
        {"action": action, "outcome": str(outcome)},
    )
    if outcome == "failed":
        _safe(
            inc,
            "agent_library_failures_total",
            {"stage": action},
        )
    p95 = _p95(_INGEST_LATENCIES, action, duration_ms)
    _safe(
        set_gauge,
        "agent_library_ingest_p95_ms",
        p95,
        {"action": action},
    )


def set_vector_index_size(backend: str, size: int) -> None:
    _safe(
        set_gauge,
        "agent_library_vector_index_size",
        max(0, int(size)),
        {"backend": str(backend or "linear")},
    )
