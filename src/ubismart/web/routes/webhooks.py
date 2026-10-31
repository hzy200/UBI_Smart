"""出站 Webhook 管理；secret 只在创建响应里返回一次。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from ubismart.control.webhooks import EVENTS, WebhookError
from ubismart.web.auth import AuthUser
from ubismart.web.dependencies import UserDep


class WebhookRequest(BaseModel):
    name: str = Field(default="Webhook", max_length=64)
    url: str = Field(min_length=8, max_length=1024)
    events: list[str] = Field(
        default_factory=lambda: ["run_finished"], max_length=3
    )


@dataclass(frozen=True)
class WebhookDeps:
    service: Any | None
    rate_limit: Callable[[AuthUser], None]


def register_webhook_routes(app: FastAPI, deps: WebhookDeps) -> None:
    def _store():
        if deps.service is None or deps.service.store is None:
            raise HTTPException(
                status_code=503,
                detail="Webhook 需要控制面数据库",
            )
        return deps.service.store

    @app.get("/api/webhooks")
    async def list_webhooks(user: UserDep) -> dict:
        if deps.service is None or deps.service.store is None:
            return {"enabled": False, "webhooks": [], "events": list(EVENTS)}
        return {
            "enabled": True,
            "webhooks": deps.service.store.list(user.name),
            "events": list(EVENTS),
        }

    @app.post("/api/webhooks")
    async def create_webhook(
        payload: WebhookRequest, user: UserDep
    ) -> dict:
        deps.rate_limit(user)
        store = _store()
        try:
            record, secret = store.create(
                user.name,
                url=payload.url,
                events=payload.events,
                name=payload.name,
            )
        except WebhookError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"ok": True, "webhook": record, "secret": secret}

    @app.delete("/api/webhooks/{webhook_id}")
    async def delete_webhook(
        webhook_id: str, user: UserDep
    ) -> dict:
        deps.rate_limit(user)
        removed = _store().delete(user.name, webhook_id)
        return {"ok": True, "removed": bool(removed)}
