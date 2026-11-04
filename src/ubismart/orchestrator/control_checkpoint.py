"""轻量控制检查点（L2 阶段回退）。

和 LangGraph 的恢复 checkpoint 分开：
- LangGraph checkpoint 保存完整图状态，用于崩溃/resume；
- 控制检查点只保存 contract、plan/tasks、artifact 引用、预算与早停计数，
  用于阶段门、跨阶段恢复与审计，不塞全量消息历史。

存储是 JSON 文件 + 文件锁；写入失败不阻断编排，但会记 silent failure。
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ubismart.platform.filelock import FileLock
from ubismart.platform.observability import note_silent_failure

_SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]+")


class ControlCheckpointStore:
    def __init__(self, root: str | Path | None, *, enabled: bool = True) -> None:
        self.root = Path(root) if root else None
        self.enabled = bool(enabled and self.root)

    def _thread_dir(self, thread_id: str) -> Path:
        assert self.root is not None
        safe = _SAFE_ID.sub("_", thread_id or "unknown")[:80]
        return self.root / safe

    def save(self, payload: Mapping[str, Any]) -> str:
        if not self.enabled or self.root is None:
            return ""
        data = dict(payload)
        thread_id = str(data.get("thread_id", "unknown"))
        checkpoint_id = str(data.get("checkpoint_id") or "")
        if not checkpoint_id:
            checkpoint_id = (
                f"stage-{data.get('stage', 0)}-"
                f"v{data.get('plan_version', 0)}-"
                f"{int(time.time() * 1000)}"
            )
            data["checkpoint_id"] = checkpoint_id
        data.setdefault("created_at", int(time.time() * 1000))
        directory = self._thread_dir(thread_id)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / f"{_SAFE_ID.sub('_', checkpoint_id)}.json"
            with FileLock(target.with_suffix(".lock")):
                temp = target.with_suffix(".json.tmp")
                temp.write_text(
                    json.dumps(data, ensure_ascii=False, default=str),
                    encoding="utf-8",
                )
                temp.replace(target)
        except OSError:
            note_silent_failure("control_checkpoint_save")
            return ""
        return checkpoint_id

    def load(self, thread_id: str, checkpoint_id: str) -> dict[str, Any] | None:
        if not self.enabled or self.root is None:
            return None
        target = (
            self._thread_dir(thread_id)
            / f"{_SAFE_ID.sub('_', checkpoint_id)}.json"
        )
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def list(self, thread_id: str) -> list[dict[str, Any]]:
        if not self.enabled or self.root is None:
            return []
        directory = self._thread_dir(thread_id)
        if not directory.exists():
            return []
        items: list[dict[str, Any]] = []
        for path in sorted(directory.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(payload, dict):
                items.append(payload)
        return sorted(
            items, key=lambda item: int(item.get("created_at", 0) or 0)
        )
