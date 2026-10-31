"""用量行 → dict 的共享转换。"""

from __future__ import annotations

from typing import Any


def _quota_row(row: Any) -> dict[str, Any]:
    if row is None:
        return {
            "daily_tokens": 0,
            "daily_cost_cny": 0.0,
            "enabled": False,
            "updated_by": "",
            "updated_at": "",
        }
    return {
        "daily_tokens": int(row.daily_tokens or 0),
        "daily_cost_cny": float(row.daily_cost_cny or 0.0),
        "enabled": bool(row.enabled),
        "updated_by": str(row.updated_by or ""),
        "updated_at": str(row.updated_at or ""),
    }
