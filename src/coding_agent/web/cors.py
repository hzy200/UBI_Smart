"""CORS 允许列表解析与预检状态码修正。"""

from __future__ import annotations

from urllib.parse import urlsplit

from starlette.middleware.cors import CORSMiddleware
from starlette.responses import Response


class CorsConfigError(ValueError):
    pass


def parse_allowed_origins(raw: str) -> list[str]:
    """解析逗号分隔的精确 origin；任何非法条目都失败关闭。"""
    text = str(raw or "").strip()
    if not text:
        return []
    origins: list[str] = []
    seen: set[str] = set()
    for item in text.split(","):
        value = item.strip()
        if not value:
            continue
        lowered = value.lower()
        if lowered == "null" or "*" in value:
            raise CorsConfigError(
                f"CORS origin 不允许通配符或 null：{value}"
            )
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"}:
            raise CorsConfigError(
                f"CORS origin 只允许 http/https：{value}"
            )
        if not parts.netloc:
            raise CorsConfigError(f"CORS origin 缺少主机：{value}")
        if parts.username or parts.password:
            raise CorsConfigError(
                f"CORS origin 不能包含用户名或密码：{value}"
            )
        if parts.query or parts.fragment:
            raise CorsConfigError(
                f"CORS origin 不能包含 query 或 fragment：{value}"
            )
        if parts.path not in {"", "/"}:
            raise CorsConfigError(
                f"CORS origin 不能包含路径：{value}"
            )
        if "*" in parts.hostname or not parts.hostname:
            raise CorsConfigError(f"CORS origin 主机非法：{value}")
        try:
            _ = parts.port
        except ValueError as exc:
            raise CorsConfigError(f"CORS origin 端口非法：{value}") from exc
        origin = f"{parts.scheme.lower()}://{parts.netloc.lower()}"
        if origin in seen:
            continue
        seen.add(origin)
        origins.append(origin)
    return origins


class StrictCORSMiddleware(CORSMiddleware):
    """把允许的预检响应改成 204（Starlette 默认 200 OK）。"""

    def preflight_response(self, request_headers) -> Response:
        response = super().preflight_response(request_headers)
        if response.status_code == 200:
            headers = {
                key: value
                for key, value in response.headers.items()
                if key.lower() not in {"content-length", "content-type"}
            }
            return Response(status_code=204, headers=headers)
        return response
