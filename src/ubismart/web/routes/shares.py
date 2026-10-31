"""只读分享链接：创建/列出/撤销 + 匿名只读页面。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from coding_agent.identity.tenancy import WebTenant
from coding_agent.web.auth import AuthUser
from coding_agent.web.dependencies import UserDep
from coding_agent.web.shares import build_share_payload, render_share_html
from coding_agent.web.validation import check_thread_id


class ShareRequest(BaseModel):
    expires_in_days: int = Field(default=7, ge=1, le=30)


@dataclass(frozen=True)
class ShareDeps:
    shares: Any | None
    config_and_tenant: Callable[[AuthUser, str], tuple[WebTenant, Any]]
    load_transcript: Callable[..., tuple[list[dict[str, Any]], bool]]
    rate_limit: Callable[[AuthUser], None]


_SHARE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Robots-Tag": "noindex, nofollow",
    "Referrer-Policy": "no-referrer",
}


def _not_found() -> HTMLResponse:
    return HTMLResponse(
        "<!DOCTYPE html><html lang=\"zh-CN\"><head>"
        "<meta charset=\"utf-8\"><meta name=\"robots\" content=\"noindex\">"
        "<title>分享不存在</title>"
        "<link rel=\"stylesheet\" href=\"/static/share.css\"></head><body>"
        "<main class=\"share-shell\"><h1>分享不存在或已过期</h1>"
        "<p>链接可能已被撤销、过期，或从未存在。</p></main></body></html>",
        status_code=404,
        headers=_SHARE_HEADERS,
    )


def register_share_routes(app: FastAPI, deps: ShareDeps) -> None:
    def _store():
        if deps.shares is None:
            raise HTTPException(
                status_code=503, detail="分享需要控制面数据库"
            )
        return deps.shares

    @app.post("/api/sessions/{thread_id}/shares")
    async def create_share(
        thread_id: str,
        payload: ShareRequest,
        user: UserDep,
    ) -> dict:
        deps.rate_limit(user)
        store = _store()
        public_thread = check_thread_id(thread_id)
        tenant, config = deps.config_and_tenant(
            user, public_thread
        )
        messages, _ = deps.load_transcript(
            tenant, public_thread, limit=None
        )
        if not messages:
            raise HTTPException(
                status_code=422,
                detail="该会话还没有可分享的对话轨迹",
            )
        title = (
            (config.title if config is not None else "")
            or public_thread
        )
        workspace = config.workspace if config is not None else ""
        data = build_share_payload(
            messages, title=title, workspace=workspace
        )
        try:
            record, token = store.create(
                user.name,
                thread_id=public_thread,
                created_by=user.name,
                title=title,
                payload=data,
                expires_in_days=payload.expires_in_days,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "ok": True,
            "share": record,
            "path": f"/share/{token}",
        }

    @app.get("/api/sessions/{thread_id}/shares")
    async def list_shares(thread_id: str, user: UserDep) -> dict:
        public_thread = check_thread_id(thread_id)
        if deps.shares is None:
            return {"enabled": False, "shares": []}
        return {
            "enabled": True,
            "shares": deps.shares.list(user.name, public_thread),
        }

    @app.delete("/api/shares/{share_id}")
    async def revoke_share(share_id: str, user: UserDep) -> dict:
        deps.rate_limit(user)
        removed = _store().revoke(user.name, share_id)
        return {"ok": True, "revoked": bool(removed)}

    @app.get("/api/share/{token}")
    async def share_json(token: str) -> JSONResponse:
        if deps.shares is None:
            return JSONResponse(
                {"error": "share.disabled"}, status_code=404,
                headers=_SHARE_HEADERS,
            )
        data = deps.shares.resolve(token)
        if data is None:
            return JSONResponse(
                {"error": "share.not_found"}, status_code=404,
                headers=_SHARE_HEADERS,
            )
        return JSONResponse(data, headers=_SHARE_HEADERS)

    @app.get("/share/{token}")
    async def share_page(token: str) -> HTMLResponse:
        if deps.shares is None:
            return _not_found()
        data = deps.shares.resolve(token)
        if data is None:
            return _not_found()
        body = render_share_html(
            {
                "title": data.get("title", ""),
                "created_at": data.get("created_at", ""),
                "expires_at": data.get("expires_at", ""),
                "messages": (data.get("payload") or {}).get(
                    "messages", []
                ),
            }
        )
        return HTMLResponse(body, headers=_SHARE_HEADERS)
