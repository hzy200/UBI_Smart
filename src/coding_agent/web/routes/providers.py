"""Provider 档案与密钥路由。"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from coding_agent.config import Settings
from coding_agent.identity.provider_profiles import (
    ProviderProfileStore,
    profile_from_payload,
)
from coding_agent.platform.secrets_store import SecretStoreUnavailable
from coding_agent.security.netguard import (
    OutboundBlocked,
    PinnedTarget,
    resolve_outbound_target,
)
from coding_agent.web.auth import AuthUser
from coding_agent.web.dependencies import UserDep


class ProviderKeyRequest(BaseModel):
    api_key: str = Field(min_length=1, max_length=500)


class ProviderTestRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)


def _pinned_request(
    pinned: PinnedTarget,
) -> tuple[str, dict[str, str], dict[str, str]]:
    """把连接钉到已校验的 IP，同时保住 Host/SNI 与原域名证书校验。

    DNS rebinding：校验与连接之间不能再解析一次。返回
    `(url, extra_headers, extensions)`；IP 与域名一致时原样返回。
    """
    if not pinned.ip or pinned.ip == pinned.host:
        return pinned.url, {}, {}
    parts = urlsplit(pinned.url)
    netloc = f"[{pinned.ip}]" if ":" in pinned.ip else pinned.ip
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    target = urlunsplit(
        (parts.scheme, netloc, parts.path, parts.query, parts.fragment)
    )
    host_header = parts.hostname or pinned.host
    if parts.port:
        host_header = f"{host_header}:{parts.port}"
    extensions = (
        {"sni_hostname": parts.hostname or pinned.host}
        if parts.scheme == "https"
        else {}
    )
    return target, {"Host": host_header}, extensions


def _models_from_response(response: Any) -> list[str]:
    """从 `/models` 响应里取模型 id；格式不标准不算不可达。"""
    try:
        parse = getattr(response, "json", None)
        payload = parse() if callable(parse) else {}
        rows = payload.get("data") if isinstance(payload, dict) else []
        return [
            str(row.get("id"))
            for row in (rows or [])
            if isinstance(row, dict) and row.get("id")
        ][:100]
    except Exception:  # noqa: BLE001 - 格式不标准不影响连通性判定
        return []


@dataclass(frozen=True)
class ProviderDeps:
    settings: Settings
    allow_write: bool
    is_multi_user: bool
    base_llm_providers: str
    profiles_path: Callable[[], Path]
    apply_profiles: Callable[[], list]
    resolve_provider: Callable[[Settings, Any, str], Any]
    user_store: Callable[[AuthUser], Any]
    rate_limit: Callable[[AuthUser], None]
    provider_rows: Callable[[Settings, Any], list[dict[str, Any]]]


def _reject_profile_write(deps: ProviderDeps) -> None:
    if deps.is_multi_user:
        raise HTTPException(
            status_code=403,
            detail="多用户模式请用 AGENT_LLM_PROVIDERS 配置 provider",
        )


def register_provider_routes(app: FastAPI, deps: ProviderDeps) -> None:
    @app.get("/api/providers")
    async def providers(user: UserDep) -> dict:
        deps.apply_profiles()
        return {
            "providers": deps.provider_rows(
                deps.settings, deps.user_store(user)
            ),
            "default_model": deps.settings.deepseek_model,
            "server_write_enabled": deps.allow_write,
            "profiles_enabled": not deps.is_multi_user,
        }

    @app.get("/api/provider-profiles")
    async def list_provider_profiles(user: UserDep) -> dict:
        profiles = deps.apply_profiles()
        return {
            "profiles": [profile.to_item() for profile in profiles],
            "base_configured": bool(deps.base_llm_providers),
            "profiles_enabled": not deps.is_multi_user,
        }

    @app.post("/api/provider-profiles")
    async def upsert_provider_profile(
        payload: dict[str, Any], user: UserDep
    ) -> dict:
        _reject_profile_write(deps)
        deps.rate_limit(user)
        try:
            profile = profile_from_payload(payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        store = ProviderProfileStore(deps.profiles_path())
        profiles = store.upsert(profile)
        deps.apply_profiles()
        return {
            "ok": True,
            "profiles": [item.to_item() for item in profiles],
        }

    @app.delete("/api/provider-profiles/{name}")
    async def delete_provider_profile(
        name: str, user: UserDep
    ) -> dict:
        _reject_profile_write(deps)
        store = ProviderProfileStore(deps.profiles_path())
        profiles = store.delete(name)
        deps.apply_profiles()
        return {
            "ok": True,
            "profiles": [item.to_item() for item in profiles],
        }

    @app.post("/api/providers/{name}/key")
    async def set_provider_key(
        name: str,
        request: ProviderKeyRequest,
        user: UserDep,
    ) -> dict:
        clean = re.sub(r"[^A-Za-z0-9_.-]", "", name)[:64]
        if not clean:
            raise HTTPException(status_code=422, detail="非法 provider 名")
        try:
            deps.user_store(user).set(clean, request.api_key)
        except SecretStoreUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"ok": True, "name": clean, "has_key": True}

    @app.delete("/api/providers/{name}/key")
    async def delete_provider_key(name: str, user: UserDep) -> dict:
        clean = re.sub(r"[^A-Za-z0-9_.-]", "", name)[:64]
        deps.user_store(user).delete(clean)
        return {"ok": True, "name": clean, "has_key": False}

    @app.post("/api/providers/test")
    async def test_provider(
        request: ProviderTestRequest,
        user: UserDep,
    ) -> dict:
        """轻量连通性探测：只请求 `/models`，不发送用户内容、不记录 key。"""
        deps.apply_profiles()
        spec = deps.resolve_provider(
            deps.settings, deps.user_store(user), request.name
        )
        if spec is None:
            raise HTTPException(status_code=404, detail="provider 不存在")
        if not spec["api_key"]:
            raise HTTPException(
                status_code=422, detail="provider 还没有配置密钥"
            )
        base_url = spec["base_url"].rstrip("/")
        # 请求会带上 provider 的真实 API Key，所以目标必须过 SSRF 守卫：
        # 只校验 scheme/host 挡不住 `http://169.254.169.254`（云元数据）与
        # `http://[::ffff:127.0.0.1]`，判定必须在解析之后按 IP 做。
        try:
            pinned = resolve_outbound_target(
                f"{base_url}/models",
                allowed_hosts=deps.settings.provider_probe_allowed_hosts,
                allow_private=(
                    deps.settings.provider_probe_allow_private_network
                ),
            )
        except OutboundBlocked as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        # DNS rebinding：校验与连接之间不能再解析一次。把连接钉到刚校验的 IP，
        # Host/SNI 仍用原域名，证书校验也按原域名做。
        target, extra_headers, extensions = _pinned_request(pinned)
        headers = {"Authorization": f"Bearer {spec['api_key']}", **extra_headers}
        try:
            # 不走环境代理：避免把 Authorization 发给 HTTP_PROXY。
            # 不跟随重定向：否则守卫过的目标可以把请求 302 到内网。
            async with httpx.AsyncClient(
                timeout=5.0, trust_env=False, follow_redirects=False
            ) as client:
                response = await client.get(
                    target,
                    headers=headers,
                    extensions=extensions or None,
                )
        except httpx.HTTPError as exc:
            return {
                "ok": False,
                "reachable": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
        ok = 200 <= response.status_code < 300
        models = _models_from_response(response) if ok else []
        return {
            "ok": ok,
            "reachable": True,
            "status_code": response.status_code,
            "model": spec["model"],
            "models": models,
            "error": "" if ok else f"provider 返回 HTTP {response.status_code}",
        }
