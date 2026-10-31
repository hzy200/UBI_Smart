"""会话轨迹文件的读写。

轨迹是桌面/Web UI 的富文本对话（含工具卡片），与 checkpoint 的自然语言
历史分开存放。这里集中路径规则、清洗上限与分页，供 runs/sessions 路由复用。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from coding_agent.identity.tenancy import WebTenant

MAX_MESSAGES = 500
MAX_CHARS = 4_000_000


def transcript_path(tenant: WebTenant, public_thread: str) -> Path:
    name = re.sub(
        r"[^A-Za-z0-9_.:-]+", "_", tenant.thread_id(public_thread)
    )[:120]
    return (
        Path(tenant.settings.resolved_audit_dir)
        / "transcripts"
        / f"{name or 'thread'}.json"
    )


def clean_transcript(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise HTTPException(status_code=400, detail="messages 必须是数组")
    cleaned: list[dict[str, Any]] = []
    total = 0
    for item in raw[:MAX_MESSAGES]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "")[:16]
        if role not in {"user", "assistant", "system", "工具"}:
            continue
        text = str(item.get("text") or "")[:200_000]
        summary = str(item.get("summary") or "")[:20_000]
        preview = str(item.get("preview") or "")[:80_000]
        total += len(text) + len(summary) + len(preview) + 256
        if total > MAX_CHARS:
            break
        cleaned.append(
            {
                "role": role,
                "kind": str(item.get("kind") or "")[:16],
                "phase": str(item.get("phase") or "")[:16],
                "text": text,
                "summary": summary,
                "preview": preview,
                "name": str(item.get("name") or "")[:64],
                "level": str(item.get("level") or "")[:32],
                "state": str(item.get("state") or "")[:16],
                "call_id": str(item.get("call_id") or "")[:64],
                "duration_ms": item.get("duration_ms"),
                "exit_code": item.get("exit_code"),
                "ok": item.get("ok"),
            }
        )
    return cleaned


def load_transcript(
    tenant: WebTenant,
    public_thread: str,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], bool]:
    """按“从最新往回”分页读取；offset = 已加载的最新条目数。"""
    path = transcript_path(tenant, public_thread)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], False
    if not isinstance(payload, dict):
        return [], False
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return [], False
    items = [item for item in messages if isinstance(item, dict)]
    if limit is None or limit <= 0:
        return items, False
    total = len(items)
    end = max(0, total - max(0, int(offset)))
    start = max(0, end - int(limit))
    return items[start:end], start > 0


def save_transcript(
    tenant: WebTenant,
    public_thread: str,
    messages: list[dict[str, Any]],
) -> None:
    path = transcript_path(tenant, public_thread)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(
        json.dumps(
            {"thread_id": public_thread, "messages": messages},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    os.replace(temp, path)
