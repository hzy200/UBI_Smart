"""HTTP tracing 中间件（中立层）。

A2（web）与 A3（agent_server）都要用，但不能让 A3 反向依赖 web 包；
这里只依赖 starlette 的 ASGI 类型，由各入口自行决定是否安装。
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send


class TracingMiddleware:
    """为每个 HTTP 请求创建 span；tracer 为空时完全跳过。"""

    def __init__(self, app: ASGIApp, *, tracer: object | None = None) -> None:
        self.app = app
        self.tracer = tracer

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self.tracer is None:
            await self.app(scope, receive, send)
            return
        method = str(scope.get("method") or "")
        path = str(scope.get("path") or "")
        status = 0
        with self.tracer.start_as_current_span(
            f"{method} {path}"
        ) as span:  # type: ignore[attr-defined]
            span.set_attribute("http.method", method)
            span.set_attribute("http.route", path)
            request_id = str(
                (scope.get("state") or {}).get("request_id") or ""
            )
            if request_id:
                span.set_attribute("request.id", request_id)

            async def send_with_status(message: Message) -> None:
                nonlocal status
                if message["type"] == "http.response.start":
                    status = int(message["status"])
                await send(message)

            try:
                await self.app(scope, receive, send_with_status)
            except Exception:
                # 异常会被外层的 ServerErrorMiddleware 转成 500；这里先记上。
                status = status or 500
                raise
            finally:
                route = scope.get("route")
                template = str(getattr(route, "path", "") or path)
                span.set_attribute("http.route", template)
                if status:
                    span.set_attribute("http.status_code", status)
                    if status >= 500:
                        try:
                            from opentelemetry.trace import StatusCode

                            span.set_status(StatusCode.ERROR)
                        except ImportError:  # pragma: no cover
                            pass
