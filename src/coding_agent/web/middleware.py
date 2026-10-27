"""Web 安全中间件与简单限流。

这些是**单进程内**的防护：安全响应头、请求体上限、固定窗口限流。多进程
部署需要外部网关或共享限流存储；当前 Web 明确按单进程运行。
"""

from __future__ import annotations

import logging
import time
from collections import deque

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from coding_agent.request_context import (
    REQUEST_ID_HEADER,
    reset_request_id,
    set_request_id,
)
from coding_agent.web.errors import (
    ensure_request_id,
    problem_response,
    request_id_from_scope,
)
from coding_agent.web.version import frontend_is_too_old

_LOG = logging.getLogger(__name__)

_CSP = (
    "default-src 'self'; "
    # 前端使用 Alpine CSP 构建；模板只允许属性访问/方法引用，
    # 不再需要 unsafe-eval。
    "script-src 'self'; "
    "style-src 'self'; "
    "connect-src 'self'; "
    "img-src 'self' data:; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'self'"
)


class RequestIdMiddleware:
    """为每个 HTTP 请求生成/透传 request id，并放到响应头。

    外层优先：必须包住 BodySize/Security 等中间件，保证它们提前返回的
    错误也带同一个 id。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        request_id = ensure_request_id(headers.get(REQUEST_ID_HEADER))
        scope.setdefault("state", {})["request_id"] = request_id
        token = set_request_id(request_id)
        status = 0
        started = time.monotonic()

        async def send_with_headers(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        finally:
            path = str(scope.get("path") or "")
            if path.startswith("/api") or status >= 400:
                _LOG.info(
                    "http %s %s -> %s request_id=%s %.0fms",
                    scope.get("method", ""),
                    path,
                    status or "?",
                    request_id,
                    (time.monotonic() - started) * 1000,
                )
            reset_request_id(token)


class FrontendVersionMiddleware:
    """可选地拒绝过旧的前端。

    未携带 `X-Frontend-Version` 的旧客户端仍放行，给升级留窗口；
    一旦携带非法或过旧版本，API 返回 426，避免继续用错误契约。
    """

    def __init__(self, app: ASGIApp, *, minimum: str) -> None:
        self.app = app
        self.minimum = minimum

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path") or "")
        if not path.startswith("/api/"):
            await self.app(scope, receive, send)
            return
        value = Headers(scope=scope).get("X-Frontend-Version", "").strip()
        if not value:
            await self.app(scope, receive, send)
            return
        too_old = frontend_is_too_old(value, self.minimum)
        if too_old is False:
            await self.app(scope, receive, send)
            return
        detail = (
            f"前端版本 {value} 低于最低要求 {self.minimum}"
            if too_old
            else f"X-Frontend-Version 不是合法版本号：{value}"
        )
        response = problem_response(
            status_code=426,
            code="version.frontend_too_old",
            detail=detail,
            details={"minimum": self.minimum, "received": value},
            instance=path,
            request_id=request_id_from_scope(scope),
        )
        await response(scope, receive, send)


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, *, hsts: bool = False) -> None:
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("Content-Security-Policy", _CSP)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("Referrer-Policy", "no-referrer")
                headers.setdefault(
                    "Permissions-Policy",
                    "camera=(), microphone=(), geolocation=()",
                )
                headers.setdefault("Cache-Control", "no-store")
                if self.hsts or scope.get("scheme") == "https":
                    headers.setdefault(
                        "Strict-Transport-Security",
                        "max-age=31536000; includeSubDomains",
                    )
            await send(message)

        await self.app(scope, receive, send_with_headers)


class BodySizeLimitMiddleware:
    """按 Content-Length 拒绝超大请求；上传端点单独使用更大的上限。"""

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_bytes: int,
        upload_max_bytes: int | None = None,
        upload_suffix: str = "/uploads",
        workspace_upload_max_bytes: int | None = None,
    ) -> None:
        self.app = app
        self.max_bytes = max(1, int(max_bytes or 1))
        self.upload_max_bytes = (
            max(1, int(upload_max_bytes)) if upload_max_bytes else None
        )
        self.upload_suffix = upload_suffix
        self.workspace_upload_max_bytes = (
            max(1, int(workspace_upload_max_bytes))
            if workspace_upload_max_bytes
            else None
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers") or []
        }
        limit = self.max_bytes
        path = str(scope.get("path") or "")
        if (
            self.workspace_upload_max_bytes
            and "/workspaces/" in path
            and path.endswith(self.upload_suffix)
        ):
            # 项目归档上传走单独的、更大的上限；普通附件仍用 upload_max_bytes。
            limit = self.workspace_upload_max_bytes
        elif self.upload_max_bytes and path.endswith(self.upload_suffix):
            limit = self.upload_max_bytes
        raw = headers.get("content-length")
        method = str(scope.get("method") or "").upper()
        transfer = headers.get("transfer-encoding", "").lower()
        if (
            raw is None
            and method in {"POST", "PUT", "PATCH"}
            and "chunked" in transfer
        ):
            # 没有 Content-Length 就无法在读取前拒绝超大请求；直接要求
            # 定长请求，避免 chunked 绕过上限。
            response = problem_response(
                status_code=411,
                code="request.length_required",
                detail="chunked 请求体不受支持；请发送 Content-Length",
                instance=path,
                request_id=request_id_from_scope(scope),
            )
            await response(scope, receive, send)
            return
        if raw is not None:
            try:
                length = int(raw)
            except ValueError:
                length = 0
            if length > limit:
                response = problem_response(
                    status_code=413,
                    code="request.too_large",
                    detail=f"请求体超过上限 {limit} 字节",
                    details={
                        "limit_bytes": limit,
                        "scope": (
                            "upload"
                            if path.endswith(self.upload_suffix)
                            else "body"
                        ),
                    },
                    instance=path,
                    request_id=request_id_from_scope(scope),
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


class FixedWindowRateLimiter:
    """按 key 的 60 秒固定窗口限流（单进程、内存态）。"""

    def __init__(self, *, per_minute: int) -> None:
        self.per_minute = max(1, int(per_minute or 1))
        self._events: dict[str, deque[float]] = {}

    def allow(self, key: str, *, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        bucket = self._events.setdefault(key, deque())
        cutoff = current - 60.0
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= self.per_minute:
            return False
        bucket.append(current)
        return True

    def retry_after(self, key: str, *, now: float | None = None) -> int:
        current = time.monotonic() if now is None else now
        bucket = self._events.get(key)
        if not bucket:
            return 0
        return max(1, int(60.0 - (current - bucket[0])))
