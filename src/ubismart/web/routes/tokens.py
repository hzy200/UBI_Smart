"""个人访问令牌（PAT）管理；明文只在创建响应里返回一次。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from coding_agent.control.api_tokens import SCOPES, normalize_scopes
from coding_agent.web.auth import AuthUser
from coding_agent.web.dependencies import UserDep


class TokenRequest(BaseModel):
    name: str = Field(default="CI token", max_length=64)
    scopes: list[str] = Field(
        default_factory=lambda: ["read"], max_length=3
    )
    expires_in_days: int = Field(default=90, ge=0, le=3650)


@dataclass(frozen=True)
class TokenDeps:
    tokens: Any | None
    rate_limit: Callable[[AuthUser], None]


def register_token_routes(app: FastAPI, deps: TokenDeps) -> None:
    def _store():
        if deps.tokens is None:
            raise HTTPException(
                status_code=503,
                detail="个人访问令牌需要企业账号与控制面数据库",
            )
        return deps.tokens

    @app.get("/api/tokens")
    async def list_tokens(user: UserDep) -> dict:
        if deps.tokens is None:
            return {"enabled": False, "tokens": [], "scopes": list(SCOPES)}
        return {
            "enabled": True,
            "tokens": deps.tokens.list(user.name),
            "scopes": list(SCOPES),
        }

    @app.post("/api/tokens")
    async def create_token(
        payload: TokenRequest, user: UserDep
    ) -> dict:
        deps.rate_limit(user)
        store = _store()
        try:
            selected = normalize_scopes(payload.scopes)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not selected:
            raise HTTPException(
                status_code=422, detail="至少选择一个 scope"
            )
        record, token = store.create(
            username=user.name,
            user_id=user.user_id,
            name=payload.name,
            scopes=selected,
            expires_in_days=payload.expires_in_days,
        )
        return {"ok": True, "token": token, "record": record}

    @app.delete("/api/tokens/{token_id}")
    async def revoke_token(token_id: str, user: UserDep) -> dict:
        deps.rate_limit(user)
        removed = _store().revoke(user.name, token_id)
        return {"ok": True, "revoked": bool(removed)}
