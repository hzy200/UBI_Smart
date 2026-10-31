"""桌面壳生命周期路由：优雅退出与关闭前取消所有运行。"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, HTTPException

from ubismart.web.dependencies import UserDep


@dataclass(frozen=True)
class DesktopDeps:
    app: FastAPI
    desktop_mode: bool
    registry_lock: Any
    runtimes: Any
    tenants: Any


def register_desktop_routes(app: FastAPI, deps: DesktopDeps) -> None:
    @app.post("/api/desktop/shutdown")
    async def desktop_shutdown(user: UserDep) -> dict:
        """桌面 server 的优雅退出入口；仅 desktop_mode 暴露。"""
        if not deps.desktop_mode:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "desktop.unavailable",
                    "message": "桌面专属能力未启用",
                },
            )
        callback = getattr(deps.app.state, "shutdown", None)
        if callable(callback):
            asyncio.get_running_loop().call_soon(callback)
        return {"ok": True}

    @app.post("/api/desktop/cancel_all")
    async def desktop_cancel_all(user: UserDep) -> dict:
        """桌面窗口关闭前协作式取消所有运行；仅桌面模式暴露。"""
        if not deps.desktop_mode:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "desktop.unavailable",
                    "message": "桌面专属能力未启用",
                },
            )
        with deps.registry_lock:
            entries = list(deps.runtimes.items())
        cancelled = 0
        for (owner, public_thread), entry in entries:
            try:
                tenant = deps.tenants.for_user(owner)
                entry.runtime.steer(
                    tenant.thread_id(public_thread),
                    "cancel",
                    source="desktop-close",
                )
                cancelled += 1
            except Exception:  # noqa: BLE001 - 单个会话取消失败不阻断关窗
                continue
        deadline = time.monotonic() + 5.0
        active = False
        while time.monotonic() < deadline:
            with deps.registry_lock:
                active = any(
                    getattr(item.runtime, "_stream_active", False)
                    for item in deps.runtimes.values()
                )
            if not active:
                break
            await asyncio.sleep(0.1)
        return {"cancelled": cancelled, "waited": not active}
