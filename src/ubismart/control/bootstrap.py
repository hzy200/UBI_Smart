"""控制面引导：创建**第一个**企业账号。

这不是日常管理入口。账号管理的正路是 A2 的 `/api/admin/users` —— 鉴权、审计、
四眼审批都在那条路上。但冷启动时服务还没起来、一个能登录的账号都没有，
必须留一条可以在宿主上执行的一次性路径；本模块就是那条路径，并且用
"租户下一条账号都没有"这个前置条件把它锁成**只此一次**。

为什么放在 `control/` 而不是 CLI：CLI 拿到 `ControlStore` 句柄之后能做任何
事（改角色、改密码、删账号），那才是 TD-032 说的"压平 A1/A2 边界"。收窄成
一个只会建首账号的函数后，越权的表达力就没了。
"""

from __future__ import annotations

from typing import Any

from ubismart.config import Settings
from ubismart.control.accounts import AccountError, UserStore
from ubismart.control.store import ControlStore


def create_first_admin(
    settings: Settings,
    username: str,
    password: str,
    *,
    email: str = "",
    role: str = "owner",
) -> dict[str, Any]:
    """创建租户下的第一个账号；已有任何账号时拒绝。

    失败一律抛 `AccountError`（`ValueError` 子类），调用方按普通业务错误处理。
    """
    url = str(settings.control_db_url or "").strip()
    if not url:
        raise AccountError(
            "未配置 AGENT_CONTROL_DB_URL；账号必须保存在控制面数据库。"
        )
    store = ControlStore(url, auto_create=bool(settings.control_db_auto_create))
    try:
        users = UserStore(store.engine, tenant=settings.web_account_tenant)
        if users.list(settings.web_account_tenant):
            raise AccountError(
                "控制面已有账号：本命令只用于冷启动创建第一个账号，"
                "后续请走 A2 的管理接口 /api/admin/users。"
            )
        return users.create(
            username,
            password,
            email=email,
            role=role,
            tenant=settings.web_account_tenant,
        )
    finally:
        store.close()
