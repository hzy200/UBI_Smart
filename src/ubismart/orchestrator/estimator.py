"""执行前成本预估（S5）：规则先验 + 历史分桶 → Estimation Card。

不做的事：
- 不让模型凭直觉报一个数（长任务耗时大头在工具 IO/重试/等人，模型估不了）；
- 不读执行上下文，不调用执行工具；估算开销必须远小于它拦下的浪费。

分桶维度按文档：`task_type × role × model`。有嵌套后再加 depth；当前
`depth` 只记录不分桶，避免样本被过细的维度切碎。
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from ubismart.platform.filelock import FileLock

# 规则先验（建议初值；上线后由历史分位数校准，不要当实测结论引用）。
ROLE_PRIORS: dict[str, dict[str, int]] = {
    "explore": {"tokens": 6_000, "wall_ms": 60_000, "tools": 8},
    "test": {"tokens": 4_000, "wall_ms": 90_000, "tools": 4},
    "review": {"tokens": 5_000, "wall_ms": 60_000, "tools": 5},
    "implement": {"tokens": 12_000, "wall_ms": 180_000, "tools": 15},
    "document": {"tokens": 10_000, "wall_ms": 120_000, "tools": 10},
    "vision": {"tokens": 6_000, "wall_ms": 90_000, "tools": 4},
    "author": {"tokens": 8_000, "wall_ms": 120_000, "tools": 5},
}

_CONFIDENCE_ORDER = {"low": 0, "med": 1, "high": 2}


def _percentile(values: list[int], fraction: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    if len(ordered) == 1:
        return int(ordered[0])
    position = (len(ordered) - 1) * min(max(fraction, 0.0), 1.0)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return int(ordered[lower])
    weight = position - lower
    return int(ordered[lower] * (1 - weight) + ordered[upper] * weight)


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _sample_tokens(sample: Mapping[str, Any]) -> int:
    return _as_int(sample.get("input_tokens")) + _as_int(
        sample.get("output_tokens")
    )


def _sample_wall_ms(sample: Mapping[str, Any]) -> int:
    duration = _as_int(sample.get("duration_ms"))
    if duration > 0:
        return duration
    return _as_int(sample.get("llm_ms")) + _as_int(sample.get("tool_ms"))


class EstimationStore:
    """历史样本的 JSONL 存储；按 `task_type/role/model` 分桶。

    文件很小（每个 worker 一行），读取时整体加载。写入失败只影响估准度，
    不能阻断任务；但会记一条 silent failure。
    """

    def __init__(
        self,
        path: str | Path | None,
        *,
        enabled: bool = True,
        max_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        self.path = Path(path) if path else None
        self.enabled = bool(enabled and self.path)
        self.max_bytes = max(0, int(max_bytes or 0))

    def _append(self, sample: Mapping[str, Any]) -> None:
        if not self.enabled or self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with FileLock(self.path.with_suffix(self.path.suffix + ".lock")):
                if (
                    self.max_bytes
                    and self.path.exists()
                    and self.path.stat().st_size >= self.max_bytes
                ):
                    self.path.replace(self.path.with_suffix(".1.jsonl"))
                line = json.dumps(
                    dict(sample),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        except OSError:
            # 估准度是增强能力；磁盘满/权限不足时静默失败即可。
            from ubismart.platform.observability import note_silent_failure

            note_silent_failure("estimation_store_append")

    def record(self, sample: Mapping[str, Any]) -> None:
        self._append(sample)

    def load(self) -> list[dict[str, Any]]:
        if not self.enabled or self.path is None:
            return []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        except OSError:
            return []
        samples: list[dict[str, Any]] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(item, dict):
                samples.append(item)
        return samples


class Estimator:
    def __init__(
        self,
        *,
        store: EstimationStore | None = None,
        min_samples: int = 5,
        short_task_s: float = 60.0,
        long_task_s: float = 120.0,
        total_tokens: int = 0,
    ) -> None:
        self.store = store
        self.min_samples = max(1, int(min_samples or 1))
        self.short_task_s = max(1.0, float(short_task_s or 60.0))
        self.long_task_s = max(
            self.short_task_s, float(long_task_s or 120.0)
        )
        self.total_tokens = max(0, int(total_tokens or 0))

    @classmethod
    def from_settings(cls, settings: Any, *, store: EstimationStore | None = None) -> Estimator:
        return cls(
            store=store
            or EstimationStore(
                Path(settings.resolved_audit_dir) / "estimates.jsonl",
                enabled=bool(
                    getattr(settings, "orchestrator_estimation_enabled", True)
                ),
            ),
            min_samples=int(
                getattr(settings, "orchestrator_estimation_min_samples", 5)
                or 5
            ),
            short_task_s=float(
                getattr(settings, "orchestrator_short_task_threshold_s", 60)
                or 60
            ),
            long_task_s=float(
                getattr(settings, "orchestrator_long_task_threshold_s", 120)
                or 120
            ),
            total_tokens=int(
                getattr(settings, "orchestrator_total_tokens", 0) or 0
            ),
        )

    @staticmethod
    def _bucket(
        history: Iterable[Mapping[str, Any]],
        *,
        task_type: str,
        role: str,
        model: str,
    ) -> list[Mapping[str, Any]]:
        exact = [
            item
            for item in history
            if str(item.get("task_type", "")) == task_type
            and str(item.get("role", "")) == role
            and str(item.get("model", "")) == model
        ]
        if exact:
            return exact
        # 模型名变化（升级/回退）时先退回同 task_type × role 的桶。
        return [
            item
            for item in history
            if str(item.get("task_type", "")) == task_type
            and str(item.get("role", "")) == role
        ]

    def estimate(
        self,
        tasks: list[dict[str, Any]],
        *,
        task_type: str,
        model: str,
        history: Iterable[Mapping[str, Any]] = (),
        role_models: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        history = list(history)
        p50_tokens = 0
        p90_tokens = 0
        p50_wall = 0
        p90_wall = 0
        p50_tools = 0
        p90_tools = 0
        long_tail: list[str] = []
        low_confidence = False
        med_confidence = False

        for task in tasks:
            role = str(task.get("role", "explore"))
            prior = ROLE_PRIORS.get(role, ROLE_PRIORS["explore"])
            role_model = str((role_models or {}).get(role) or model)
            samples = self._bucket(
                history,
                task_type=task_type,
                role=role,
                model=role_model,
            )
            if len(samples) >= self.min_samples:
                token_values = [_sample_tokens(item) for item in samples]
                wall_values = [_sample_wall_ms(item) for item in samples]
                tool_values = [
                    _as_int(item.get("tool_calls")) for item in samples
                ]
                task_p50_tokens = _percentile(token_values, 0.5) or prior["tokens"]
                task_p90_tokens = _percentile(token_values, 0.9) or prior["tokens"]
                task_p50_wall = _percentile(wall_values, 0.5) or prior["wall_ms"]
                task_p90_wall = _percentile(wall_values, 0.9) or prior["wall_ms"]
                task_p50_tools = _percentile(tool_values, 0.5) or prior["tools"]
                task_p90_tools = _percentile(tool_values, 0.9) or prior["tools"]
            else:
                task_p50_tokens = int(prior["tokens"] * 0.8)
                task_p90_tokens = int(prior["tokens"] * 2.0)
                task_p50_wall = int(prior["wall_ms"] * 0.8)
                task_p90_wall = int(prior["wall_ms"] * 2.0)
                task_p50_tools = prior["tools"]
                task_p90_tools = max(prior["tools"] * 2, prior["tools"])
                if not samples:
                    low_confidence = True
                else:
                    med_confidence = True

            if str(task.get("branching", "none")) == "unbounded":
                low_confidence = True
                long_tail.append(f"{task.get('id', '?')}: 无界分支")
            if bool(task.get("needs_human")):
                long_tail.append(f"{task.get('id', '?')}: 人工卡点")

            p50_tokens += task_p50_tokens
            p90_tokens += task_p90_tokens
            p50_wall += task_p50_wall
            p90_wall += task_p90_wall
            p50_tools += task_p50_tools
            p90_tools += task_p90_tools

        # 重试与外部 IO 长尾：只作用于 P90，不把 P50 吹大。
        if p90_wall > self.long_task_s * 1000:
            med_confidence = True
        if low_confidence:
            confidence = "low"
        elif med_confidence or p90_wall > self.short_task_s * 1000:
            confidence = "med"
        else:
            confidence = "high"

        if self.total_tokens and p90_tokens > self.total_tokens:
            recommended = "split"
        elif p90_wall > self.long_task_s * 1000:
            recommended = "confirm"
        elif p90_wall > self.short_task_s * 1000:
            recommended = "notice"
        else:
            recommended = "auto"

        probe_required = bool(
            confidence == "low"
            and p90_wall > self.short_task_s * 1000
        )
        gate = _gate_for(
            recommended,
            p90_wall=p90_wall,
            task_count=len(tasks),
            probe_required=probe_required,
        )
        return {
            "task_type": task_type,
            "model": model,
            "role_models": dict(role_models or {}),
            "est_steps": len(tasks),
            "est_tool_calls": int(p90_tools),
            "p50_tokens": int(p50_tokens),
            "p90_tokens": int(p90_tokens),
            "p50_wall_ms": int(p50_wall),
            "p90_wall_ms": int(p90_wall),
            "p50_tool_calls": int(p50_tools),
            "confidence": confidence,
            "long_tail": long_tail,
            "human_wait": any(bool(t.get("needs_human")) for t in tasks),
            "recommended": recommended,
            "probe_required": probe_required,
            "gate": gate,
            "created_at": int(time.time() * 1000),
        }


def _gate_for(
    recommended: str,
    *,
    p90_wall: int,
    task_count: int,
    probe_required: bool,
) -> dict[str, Any]:
    if recommended in {"auto"}:
        return {
            "checkpoint_granularity": "coarse",
            "preemption": False,
            "phase_gates": False,
            "isolation_level": "L0",
            "watchdog_ms": 0,
            "probe": probe_required,
        }
    if recommended in {"notice"}:
        return {
            "checkpoint_granularity": "normal",
            "preemption": True,
            "phase_gates": False,
            "isolation_level": "L1",
            "watchdog_ms": 0,
            "probe": probe_required,
        }
    per_task = max(1, task_count)
    return {
        "checkpoint_granularity": "fine",
        "preemption": True,
        "phase_gates": True,
        "isolation_level": "L2",
        "watchdog_ms": max(60_000, int(p90_wall / per_task)),
        "probe": probe_required,
    }


_ROLE_MODEL_FIELDS = {
    "explore": "orchestrator_explore_model",
    "review": "orchestrator_review_model",
    "implement": "orchestrator_implement_model",
    "document": "orchestrator_document_model",
    "author": "orchestrator_author_model",
    "vision": "orchestrator_vision_model",
}


def role_models_from_settings(settings: Any) -> dict[str, str]:
    """按角色取实际模型名，用于 `task_type × role × model` 分桶。"""
    base = str(
        getattr(settings, "orchestrator_worker_model", "")
        or getattr(settings, "deepseek_model", "")
        or ""
    )
    models: dict[str, str] = {}
    for role, field_name in _ROLE_MODEL_FIELDS.items():
        value = str(getattr(settings, field_name, "") or "")
        models[role] = value or base
    models.setdefault("test", base)
    return models


def sample_from_result(
    result: Mapping[str, Any],
    *,
    task_type: str,
    model: str,
) -> dict[str, Any]:
    """把 WorkerResult 压成一条历史样本。"""
    tool_calls = result.get("tool_calls") or []
    return {
        "task_type": task_type,
        "role": str(result.get("role", "")),
        "model": model,
        "input_tokens": _as_int(result.get("input_tokens")),
        "output_tokens": _as_int(result.get("output_tokens")),
        "duration_ms": _as_int(result.get("duration_ms")),
        "llm_ms": _as_int(result.get("llm_ms")),
        "tool_ms": _as_int(result.get("tool_ms")),
        "tool_calls": len(tool_calls) if isinstance(tool_calls, list) else 0,
        "status": str(result.get("status", "")),
        "created_at": int(time.time() * 1000),
    }
