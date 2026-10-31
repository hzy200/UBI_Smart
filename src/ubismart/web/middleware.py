"""Web 安全中间件与简单限流。

这些是**单进程内**的防护：安全响应头、请求体上限、固定窗口限流。多进程
部署需要外部网关或共享限流存储；当前 Web 明确按单进程运行。
"""

from __future__ import annotations

import logging
import re
import time
from collections import deque

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ubismart.platform.request_context import (
    REQUEST_ID_HEADER,
    reset_request_id,
    set_request_id,
)
from ubismart.web.errors import (
    ensure_request_id,
    problem_response,
    request_id_from_scope,
)
from ubismart.web.version import frontend_is_too_old

_LOG = logging.getLogger(__name__)

# 发布包里的资源名形如 app.8821a0ed4a57.js；内容变则名字变，可以长缓存。
_HASHED_ASSET = re.compile(r"\.[0-9a-f]{8,}\.(?:js|css|svg|woff2?)$")
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def cache_control_for_path(path: str) -> str:
    """按路径给出 Cache-Control；未知响应保守走 no-store。"""
    if path == "/healthz" or path.startswith("/api/"):
        return "no-store"
    if path in {
        "/",
        "/index.html",
        "/admin",
        "/admin/",
        "/config.json",
        "/sw.js",
        "/manifest.webmanifest",
    }:
        return "no-cache"
    if path.startswith("/static/"):
        # 只有带内容哈希的文件才允许 immutable；包内固定名（开发/桌面）
        # 必须 no-cache，否则升级后浏览器会一直用旧文件。
        if _HASHED_ASSET.search(path):
            return "public, max-age=31536000, immutable"
        return "no-cache"
    return "no-store"

_CSP = (
    "default-src 'self'; "
    # 前端使用 Alpine CSP 构建；模板只允许属性访问/方法引用，
    # 不再需要 unsafe-eval。
    "script-src 'self'; "
    "style-src 'self'; "
    "connect-src 'self'; "
    "img-src 'self' data:; "
    # PDF viewer 使用前端生成的同源 blob URL；不允许外部 frame。
    "frame-src 'self' blob:; "
    "worker-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "report-uri /api/client-errors; "
    "report-to csp-endpoint"
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


class HttpMetricsMiddleware:
    """按路由模板与状态类计数；不把原始路径/ID 当标签。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        status = 0

        async def send_with_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_with_status)
        finally:
            from ubismart.platform.observability import inc

            route = scope.get("route")
            template = str(getattr(route, "path", "") or "unmatched")
            state = f"{status // 100}xx" if status else "error"
            inc(
                "agent_http_requests_total",
                {"route": template, "status": state},
            )


class ApiVersionRewriteMiddleware:
    """把 `/api/v1/*` 重写为 `/api/*`，并标记旧路径为弃用。

    路由只注册一份 `/api/*`，避免每个 `register_*_routes` 写两遍。
    v0 兼容窗口用 `Deprecation: true` 标注，不设置硬过期。
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        current: str = "v1",
        deprecated: str = "v0",
    ) -> None:
        self.app = app
        self.current = current
        self.deprecated = deprecated

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path") or "")
        prefix = f"/api/{self.current}"
        version = ""
        rewritten = path
        if path == prefix or path.startswith(prefix + "/"):
            rewritten = "/api" + path[len(prefix):]
            version = self.current
        elif path == "/api" or path.startswith("/api/"):
            version = self.deprecated
        else:
            await self.app(scope, receive, send)
            return

        scope = dict(scope)
        scope["path"] = rewritten
        state = scope.get("state")
        state = dict(state) if isinstance(state, dict) else {}
        state["api_version"] = version
        scope["state"] = state

        async def send_with_version(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-API-Version", version)
                if version == self.deprecated:
                    headers.setdefault("Deprecation", "true")
            await send(message)

        await self.app(scope, receive, send_with_version)


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
                headers.setdefault(
                    "Reporting-Endpoints",
                    'csp-endpoint="/api/client-errors"',
                )
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("Referrer-Policy", "no-referrer")
                headers.setdefault(
                    "Permissions-Policy",
                    "camera=(), microphone=(), geolocation=()",
                )
                headers.setdefault(
                    "Cache-Control",
                    cache_control_for_path(str(scope.get("path") or "")),
                )
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


_AUTH_PATHS = ("/api/auth", "/api/v1/auth")


class AuditorReadOnlyMiddleware:
    """`auditor` 角色只读：非安全方法的 `/api/*` 一律 403。

    这不是替代路由级鉴权，而是把“只读”从每个新路由都要记得检查，
    变成默认拒绝；`/api/auth/*` 例外（退出登录/会话切换本身是写操作）。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method = str(scope.get("method") or "").upper()
        path = str(scope.get("path") or "")
        if (
            method not in _UNSAFE_METHODS
            or not path.startswith("/api/")
            or any(
                path == prefix or path.startswith(prefix + "/")
                for prefix in _AUTH_PATHS
            )
        ):
            await self.app(scope, receive, send)
            return
        from starlette.requests import Request

        request = Request(scope)
        auth = getattr(request.app.state, "auth", None)
        if auth is None:
            await self.app(scope, receive, send)
            return
        try:
            user = auth.user_from_request(request)
        except Exception:  # noqa: BLE001 - 未认证交给路由返回 401
            await self.app(scope, receive, send)
            return
        if user.role != "auditor":
            await self.app(scope, receive, send)
            return
        response = problem_response(
            status_code=403,
            code="auth.read_only",
            detail="审计账号为只读角色，不能执行写操作",
            instance=path,
            request_id=request_id_from_scope(scope),
        )
        await response(scope, receive, send)


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


class RedisFixedWindowRateLimiter:
    """Redis 固定窗口；同一套语义，供多实例共享。

    Redis 故障时 `allow` 返回 False（fail closed），避免限流悄悄失效。
    """

    def __init__(
        self,
        client: object,
        *,
        per_minute: int,
        prefix: str = "coding-agent:rate",
    ) -> None:
        self.client = client
        self.per_minute = max(1, int(per_minute or 1))
        self.prefix = str(prefix or "coding-agent:rate")

    def _key(self, key: str, now: float) -> str:
        bucket = int(now // 60)
        return f"{self.prefix}:{key}:{bucket}"

    def allow(self, key: str, *, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        redis_key = self._key(key, current)
        try:
            count = int(self.client.incr(redis_key))
            if count == 1:
                self.client.expire(redis_key, 61)
        except Exception:  # noqa: BLE001 - 限流后端故障 fail closed
            _LOG.warning("Redis 限流不可用，暂拒绝请求", exc_info=True)
            return False
        return count <= self.per_minute

    def retry_after(self, key: str, *, now: float | None = None) -> int:
        current = time.time() if now is None else now
        try:
            ttl = int(self.client.ttl(self._key(key, current)))
        except Exception:  # noqa: BLE001
            return 60
        return max(1, ttl) if ttl and ttl > 0 else 60
