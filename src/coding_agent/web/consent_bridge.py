"""桌面端本机授权桥：A2L 发起请求，聊天界面里的按钮做决定。

A2L 不能直接操作 WebView。A2L 用内部 token 调 `/internal/consent`，
前端轮询 `/api/consent/pending` 并在对话流里渲染授权卡片；用户点击后
`/api/consent/{id}` 写入决定，A2L 轮询到结果后继续执行。

只在桌面模式且配置了 `AGENT_CONSENT_TOKEN` 时启用；A2 只监听 loopback。
"""

from __future__ import annotations

import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request

from coding_agent.web.dependencies import UserDep

REQUEST_TTL_S = 900.0
RESOLVED_TTL_S = 300.0


@dataclass
class ConsentItem:
    id: str
    scope: list[str] = field(default_factory=list)
    verbs: list[str] = field(default_factory=list)
    ttl_s: int = 1800
    reason: str = ""
    sensitive: bool = False
    created_at: float = field(default_factory=time.monotonic)
    decision: bool | None = None
    decided_at: float = 0.0

    def state(self) -> str:
        if self.decision is True:
            return "allowed"
        if self.decision is False:
            return "denied"
        return "pending"

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "scope": list(self.scope),
            "verbs": list(self.verbs),
            "ttl_s": int(self.ttl_s),
            "reason": self.reason,
            "sensitive": self.sensitive,
            "created_at": self.created_at,
        }


class ConsentBridge:
    def __init__(self) -> None:
        self._items: dict[str, ConsentItem] = {}
        self._lock = threading.Lock()

    def create(self, payload: dict[str, Any]) -> ConsentItem:
        item = ConsentItem(
            id=uuid.uuid4().hex,
            scope=[str(value)[:4096] for value in (payload.get("scope") or [])][:16],
            verbs=[str(value)[:32] for value in (payload.get("verbs") or [])][:16],
            ttl_s=max(0, int(payload.get("ttl_s") or 0)),
            reason=str(payload.get("reason") or "")[:512],
            sensitive=bool(payload.get("sensitive")),
        )
        with self._lock:
            self._prune_locked()
            self._items[item.id] = item
        return item

    def pending(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        with self._lock:
            self._prune_locked()
            items = [
                item.public()
                for item in self._items.values()
                if item.decision is None
                and now - item.created_at <= REQUEST_TTL_S
            ]
        return items

    def state(self, consent_id: str) -> str:
        with self._lock:
            item = self._items.get(consent_id)
            return item.state() if item is not None else "missing"

    def decide(self, consent_id: str, allow: bool) -> bool:
        with self._lock:
            item = self._items.get(consent_id)
            if item is None or item.decision is not None:
                return False
            item.decision = bool(allow)
            item.decided_at = time.monotonic()
        return True

    def _prune_locked(self) -> None:
        now = time.monotonic()
        stale = [
            key
            for key, item in self._items.items()
            if (
                item.decision is None
                and now - item.created_at > REQUEST_TTL_S
            )
            or (
                item.decision is not None
                and now - item.decided_at > RESOLVED_TTL_S
            )
        ]
        for key in stale:
            self._items.pop(key, None)


def _require_internal(token: str, supplied: str) -> None:
    if not supplied or not secrets.compare_digest(supplied, token):
        raise HTTPException(status_code=403, detail="内部授权 token 无效")


def register_consent_bridge(
    app: FastAPI, *, token: str
) -> ConsentBridge:
    bridge = ConsentBridge()
    app.state.consent_bridge = bridge

    @app.post("/internal/consent")
    async def create_consent(
        request: Request,
        x_consent_token: str = Header(default=""),
    ) -> dict:
        _require_internal(token, x_consent_token)
        try:
            payload = await request.json()
        except Exception as exc:  # noqa: BLE001 - 边界转 400
            raise HTTPException(status_code=400, detail="请求体不是 JSON") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="请求体必须是对象")
        item = bridge.create(payload)
        return {"id": item.id}

    @app.get("/internal/consent/{consent_id}")
    async def consent_state(
        consent_id: str,
        x_consent_token: str = Header(default=""),
    ) -> dict:
        _require_internal(token, x_consent_token)
        return {"state": bridge.state(consent_id)}

    @app.get("/api/consent/pending")
    async def pending_consents(user: UserDep) -> dict:
        return {"pending": bridge.pending()}

    @app.post("/api/consent/{consent_id}")
    async def decide_consent(
        consent_id: str,
        request: Request,
        user: UserDep,
    ) -> dict:
        try:
            payload = await request.json()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail="请求体不是 JSON") from exc
        if not isinstance(payload, dict) or "allow" not in payload:
            raise HTTPException(status_code=400, detail="缺少 allow")
        if not bridge.decide(consent_id, bool(payload.get("allow"))):
            raise HTTPException(status_code=404, detail="授权请求不存在或已处理")
        return {"ok": True}

    return bridge
