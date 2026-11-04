"""四维预算守卫（steps / tool_calls / tokens / wall_ms）。

文档要求预算按 P90 设、70% 预告警、100% 熔断。调度器原先只有 token
`TokenBudget` 与零散的 timeout/retry 配置；这里把四维收敛成一个只读守卫：

- 统计口径统一从 `worker_results` 推导，跨 checkpoint 重入时不会重复计数；
- `check()` 只返回状态，不写状态机；是否停止派发由 scheduler 决定；
- token 维度复用 `TokenBudget` 的已用/预留，避免两套账。

守卫本身不做 IO，也不调用模型，是控制面里最便宜的一层。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from ubismart.orchestrator.scheduling import usage_tokens

_POSITIVE_DIMENSIONS = ("steps", "tool_calls", "tokens", "wall_ms")


@dataclass(slots=True)
class BudgetSnapshot:
    steps: int
    step_limit: int
    tool_calls: int
    tool_call_limit: int
    tokens: int
    token_limit: int
    wall_ms: int
    wall_limit_ms: int

    def ratios(self) -> dict[str, float]:
        pairs = {
            "steps": (self.steps, self.step_limit),
            "tool_calls": (self.tool_calls, self.tool_call_limit),
            "tokens": (self.tokens, self.token_limit),
            "wall_ms": (self.wall_ms, self.wall_limit_ms),
        }
        return {
            name: (used / limit if limit > 0 else 0.0)
            for name, (used, limit) in pairs.items()
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "steps": self.steps,
            "step_limit": self.step_limit,
            "tool_calls": self.tool_calls,
            "tool_call_limit": self.tool_call_limit,
            "tokens": self.tokens,
            "token_limit": self.token_limit,
            "wall_ms": self.wall_ms,
            "wall_limit_ms": self.wall_limit_ms,
            "ratios": self.ratios(),
        }


@dataclass(slots=True)
class BudgetStatus:
    warning: list[str]
    exhausted: list[str]
    snapshot: BudgetSnapshot

    @property
    def ok(self) -> bool:
        return not self.warning and not self.exhausted


class BudgetGuard:
    """四维预算的只读换算与阈值判断。"""

    def __init__(
        self,
        *,
        step_limit: int = 0,
        tool_call_limit: int = 0,
        token_limit: int = 0,
        wall_limit_ms: int = 0,
        spent_tokens: int = 0,
        reserved_tokens: int = 0,
        scheduler_deadline: float = 0.0,
        results: Iterable[dict[str, Any]] = (),
        warning_ratio: float = 0.7,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.step_limit = max(0, int(step_limit or 0))
        self.tool_call_limit = max(0, int(tool_call_limit or 0))
        self.token_limit = max(0, int(token_limit or 0))
        self.wall_limit_ms = max(0, int(wall_limit_ms or 0))
        self.spent_tokens = max(0, int(spent_tokens or 0))
        self.reserved_tokens = max(0, int(reserved_tokens or 0))
        self.scheduler_deadline = float(scheduler_deadline or 0.0)
        self.warning_ratio = min(max(float(warning_ratio or 0.7), 0.0), 1.0)
        self._now = now
        self.steps = 0
        self.tool_calls = 0
        self.tokens = 0
        self._seen: set[tuple[str, int]] = set()
        for result in results:
            self.consume(result)

    @classmethod
    def from_settings(
        cls,
        settings: Any,
        *,
        results: Iterable[dict[str, Any]] = (),
        scheduler_deadline: float = 0.0,
        token_spent: int = 0,
        token_reserved: int = 0,
        token_limit: int | None = None,
        wall_limit_ms: int | None = None,
    ) -> BudgetGuard:
        return cls(
            step_limit=int(getattr(settings, "orchestrator_total_steps", 0) or 0),
            tool_call_limit=int(
                getattr(settings, "orchestrator_total_tool_calls", 0) or 0
            ),
            token_limit=(
                int(token_limit)
                if token_limit is not None
                else int(
                    getattr(settings, "orchestrator_total_tokens", 0) or 0
                )
            ),
            wall_limit_ms=(
                int(wall_limit_ms)
                if wall_limit_ms is not None
                else int(
                    float(
                        getattr(settings, "orchestrator_run_deadline_s", 0)
                        or 0
                    )
                    * 1000
                )
            ),
            spent_tokens=token_spent,
            reserved_tokens=token_reserved,
            scheduler_deadline=scheduler_deadline,
            results=results,
            warning_ratio=float(
                getattr(settings, "orchestrator_budget_warning_ratio", 0.7)
                or 0.7
            ),
        )

    @staticmethod
    def _key(result: dict[str, Any]) -> tuple[str, int]:
        return (
            str(result.get("task_id", "")),
            max(1, int(result.get("attempt", 1) or 1)),
        )

    def consume(self, result: dict[str, Any]) -> None:
        key = self._key(result)
        if key in self._seen:
            return
        self._seen.add(key)
        self.steps += 1
        self.tool_calls += len(result.get("tool_calls") or [])
        self.tokens += usage_tokens(result)

    def refresh_tokens(self, *, spent: int, reserved: int) -> None:
        self.spent_tokens = max(0, int(spent or 0))
        self.reserved_tokens = max(0, int(reserved or 0))

    def remaining_tokens(self) -> int | None:
        if self.token_limit <= 0:
            return None
        return max(
            0, self.token_limit - self.spent_tokens - self.reserved_tokens
        )

    def remaining_wall_ms(self) -> int | None:
        if self.wall_limit_ms <= 0:
            return None
        if self.scheduler_deadline <= 0:
            return self.wall_limit_ms
        remaining = (self.scheduler_deadline - self._now()) * 1000
        return max(0, int(remaining))

    def snapshot(self) -> BudgetSnapshot:
        remaining_wall = self.remaining_wall_ms()
        wall_ms = (
            max(0, self.wall_limit_ms - remaining_wall)
            if remaining_wall is not None
            else 0
        )
        return BudgetSnapshot(
            steps=self.steps,
            step_limit=self.step_limit,
            tool_calls=self.tool_calls,
            tool_call_limit=self.tool_call_limit,
            tokens=max(self.spent_tokens, self.tokens),
            token_limit=self.token_limit,
            wall_ms=wall_ms,
            wall_limit_ms=self.wall_limit_ms,
        )

    def check(self) -> BudgetStatus:
        snapshot = self.snapshot()
        ratios = snapshot.ratios()
        warning: list[str] = []
        exhausted: list[str] = []
        for dimension in _POSITIVE_DIMENSIONS:
            ratio = ratios.get(dimension, 0.0)
            if ratio >= 1.0:
                exhausted.append(dimension)
            elif ratio >= self.warning_ratio:
                warning.append(dimension)
        return BudgetStatus(
            warning=warning, exhausted=exhausted, snapshot=snapshot
        )

    def clamp_max_tokens(self, requested: int) -> int:
        """把模型给的 max_tokens 夹到父预算剩余额度。"""
        remaining = self.remaining_tokens()
        if remaining is None:
            return max(0, int(requested or 0))
        if requested is None or int(requested or 0) <= 0:
            return remaining
        return max(0, min(int(requested), remaining))

    def clamp_timeout(self, requested: float) -> float:
        """把模型给的 timeout 夹到剩余墙钟；0 表示没有可用时间。"""
        remaining = self.remaining_wall_ms()
        if remaining is None:
            return max(0.0, float(requested or 0))
        remaining_s = remaining / 1000.0
        if requested is None or float(requested or 0) <= 0:
            return remaining_s
        return max(0.0, min(float(requested), remaining_s))

    def abort_report(
        self,
        *,
        completed_tasks: list[str],
        blocked_tasks: list[str],
        suggestion: str = "",
    ) -> dict[str, Any]:
        snapshot = self.snapshot()
        exceeded = [
            name
            for name, ratio in snapshot.ratios().items()
            if ratio >= 1.0
        ]
        return {
            "reason": "预算耗尽：" + "、".join(exceeded or ["unknown"]),
            "completed_tasks": completed_tasks[:50],
            "blocked_tasks": blocked_tasks[:50],
            "suggestion": suggestion
            or "检查失败任务与上下文膨胀；缩小范围或提高预算后从检查点继续",
            "snapshot": snapshot.as_dict(),
        }
