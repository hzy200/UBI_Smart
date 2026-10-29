"""Web 认证依赖；路由模块与 app 共用，避免从 create_app 反向导入。"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request

from coding_agent.web.auth import AuthConfig, AuthUser


def current_user(request: Request) -> AuthUser:
    """从 app.state 读取认证配置，避免把 token 表放进依赖闭包。"""
    config: AuthConfig = request.app.state.auth
    user = config.user_from_request(request)
    config.require_csrf(request)
    return user


UserDep = Annotated[AuthUser, Depends(current_user)]


def current_admin(user: UserDep) -> AuthUser:
    """管理员权限；静态 token/桌面默认角色不是管理员。"""
    if user.role not in {"owner", "admin"}:
        raise HTTPException(
            status_code=403,
            detail={"code": "auth.forbidden", "message": "需要管理员权限"},
        )
    return user


AdminDep = Annotated[AuthUser, Depends(current_admin)]


def current_approver(user: UserDep) -> AuthUser:
    """四眼审批人：owner/admin 可兼，approver 只负责审批队列。"""
    if user.role not in {"owner", "admin", "approver"}:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "auth.forbidden",
                "message": "需要审批权限",
            },
        )
    return user


ApproverDep = Annotated[AuthUser, Depends(current_approver)]


def current_auditor(user: UserDep) -> AuthUser:
    """审计只读角色：可看账号/用量/事件，不能写（写由中间件兜底）。"""
    if user.role not in {"owner", "admin", "auditor"}:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "auth.forbidden",
                "message": "需要审计权限",
            },
        )
    return user


AuditorDep = Annotated[AuthUser, Depends(current_auditor)]
