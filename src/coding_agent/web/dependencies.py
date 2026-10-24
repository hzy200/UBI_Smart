"""Web 认证依赖；路由模块与 app 共用，避免从 create_app 反向导入。"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from coding_agent.web.auth import AuthConfig, AuthUser


def current_user(request: Request) -> AuthUser:
    """从 app.state 读取认证配置，避免把 token 表放进依赖闭包。"""
    config: AuthConfig = request.app.state.auth
    return config.user_from_request(request)


UserDep = Annotated[AuthUser, Depends(current_user)]
