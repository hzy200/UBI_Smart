"""Web Push 配置与订阅。

订阅按用户隔离；endpoint 只在服务端保存，配置端点只暴露 VAPID 公钥。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, Field

from coding_agent.web.auth import AuthUser
from coding_agent.web.dependencies import UserDep


class SubscriptionKeys(BaseModel):
    p256dh: str = Field(min_length=1, max_length=255)
    auth: str = Field(min_length=1, max_length=128)


class SubscriptionRequest(BaseModel):
    endpoint: str = Field(min_length=1, max_length=4096)
    keys: SubscriptionKeys
    user_agent: str = Field(default="", max_length=255)


class UnsubscribeRequest(BaseModel):
    endpoint: str = Field(min_length=1, max_length=4096)


@dataclass(frozen=True)
class PushDeps:
    push: Any | None
    rate_limit: Callable[[AuthUser], None]


def register_push_routes(app: FastAPI, deps: PushDeps) -> None:
    @app.get("/api/push/config")
    async def push_config(user: UserDep) -> dict:
        if deps.push is None:
            return {"enabled": False, "public_key": ""}
        return deps.push.config()

    @app.post("/api/push/subscriptions")
    async def subscribe(
        payload: SubscriptionRequest, user: UserDep
    ) -> dict:
        deps.rate_limit(user)
        if deps.push is None or not deps.push.enabled:
            return {"ok": False, "enabled": False}
        subscription = deps.push.store.add_push_subscription(
            user.name,
            endpoint=payload.endpoint,
            p256dh=payload.keys.p256dh,
            auth=payload.keys.auth,
            user_agent=payload.user_agent,
        )
        return {
            "ok": subscription is not None,
            "enabled": True,
            "id": (subscription or {}).get("id", ""),
        }

    @app.delete("/api/push/subscriptions")
    async def unsubscribe(
        payload: UnsubscribeRequest, user: UserDep
    ) -> dict:
        deps.rate_limit(user)
        if deps.push is None or deps.push.store is None:
            return {"ok": True, "removed": False, "enabled": False}
        removed = deps.push.store.remove_push_subscription(
            user.name, endpoint=payload.endpoint
        )
        return {"ok": True, "removed": bool(removed), "enabled": True}
