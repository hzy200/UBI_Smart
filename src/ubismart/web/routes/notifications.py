"""站内通知：跨设备历史与 SSE 推送。

通知由 A2 在消费运行事件时写入控制面数据库；前端 localStorage 只保留
离线缓存。没有控制面数据库时接口返回 `enabled: false`，前端继续用本地通知。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ubismart.protocol.sse_stream import sse_headers, with_heartbeat
from ubismart.security.secrets import redact_secrets
from ubismart.web.auth import AuthUser
from ubismart.web.dependencies import UserDep


def notification_payload(
    payload: dict[str, Any],
) -> tuple[str, str, str, str] | None:
    """把领域事件翻译成站内通知；返回 (kind, title, body, dedupe_key)。"""
    event_type = str(payload.get("type") or "")
    if event_type == "run_finished":
        return (
            "success",
            "运行完成",
            str(payload.get("answer") or "").strip()[:300],
            "",
        )
    if event_type == "run_failed":
        return (
            "error",
            "运行失败",
            str(payload.get("message") or "").strip()[:300],
            "",
        )
    if event_type == "approval_requested":
        command = str(payload.get("command") or "")
        body = f"{payload.get('tool') or ''}: {command}"
        return (
            "warning",
            "需要审批",
            redact_secrets(body).strip(": ")[:300],
            f"approval:{payload.get('request_id') or ''}",
        )
    return None


class ReadRequest(BaseModel):
    ids: list[str] = Field(default_factory=list)
    all: bool = False


@dataclass(frozen=True)
class NotificationDeps:
    control_store: Any | None
    rate_limit: Callable[[AuthUser], None]


def _register_list_notifications(
    app: FastAPI, deps: NotificationDeps
) -> None:
    @app.get("/api/notifications")
    async def list_notifications(
        user: UserDep,
        limit: int = Query(100, ge=1, le=200),
    ) -> dict:
        if deps.control_store is None:
            return {"enabled": False, "notifications": [], "unread": 0}
        return deps.control_store.list_notifications(user.name, limit=limit)

def _register_mark_notifications_read(
    app: FastAPI, deps: NotificationDeps
) -> None:
    @app.post("/api/notifications/read")
    async def mark_notifications_read(
        payload: ReadRequest, user: UserDep
    ) -> dict:
        deps.rate_limit(user)
        if deps.control_store is None:
            return {"ok": True, "updated": 0, "enabled": False}
        updated = deps.control_store.mark_notifications_read(
            user.name, ids=payload.ids, all_=payload.all
        )
        return {"ok": True, "updated": updated, "enabled": True}

def _register_clear_notifications(
    app: FastAPI, deps: NotificationDeps
) -> None:
    @app.delete("/api/notifications")
    async def clear_notifications(user: UserDep) -> dict:
        deps.rate_limit(user)
        if deps.control_store is None:
            return {"ok": True, "removed": 0, "enabled": False}
        removed = deps.control_store.clear_notifications(user.name)
        return {"ok": True, "removed": removed, "enabled": True}

def _register_notification_stream(
    app: FastAPI, deps: NotificationDeps
) -> None:
    @app.get("/api/notifications/stream")
    async def notification_stream(
        request: Request, user: UserDep
    ) -> StreamingResponse:
        """通知快照变化时推送；2 秒一次数据库对比，15 秒心跳。"""

        async def events() -> AsyncIterator[str]:
            if deps.control_store is None:
                yield (
                    'data: {"enabled": false, "notifications": [], '
                    '"unread": 0}\n\n'
                )
                return
            last = ""
            while not await request.is_disconnected():
                try:
                    data = await asyncio.to_thread(
                        deps.control_store.list_notifications,
                        user.name,
                        limit=100,
                    )
                except Exception as exc:  # noqa: BLE001 - 用事件告知客户端
                    yield (
                        "event: error\n"
                        f"data: {json.dumps({'error': str(exc)[:200]})}\n\n"
                    )
                    await asyncio.sleep(5)
                    continue
                payload = json.dumps(data, ensure_ascii=False)
                if payload != last:
                    last = payload
                    yield f"data: {payload}\n\n"
                await asyncio.sleep(2)

        return StreamingResponse(
            with_heartbeat(events(), interval_s=15.0),
            media_type="text/event-stream",
            headers=sse_headers(),
        )


def register_notification_routes(
    app: FastAPI, deps: NotificationDeps
) -> None:
    _register_list_notifications(app, deps)
    _register_mark_notifications_read(app, deps)
    _register_clear_notifications(app, deps)
    _register_notification_stream(app, deps)
