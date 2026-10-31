"""企业管理员 CLI：**只在冷启动时**创建第一个账号。

CLI 不持有控制面 store 句柄：账号管理的正路是 A2 的 `/api/admin/users`。
这里只调 `control.bootstrap.create_first_admin()`，它在"租户下已有账号"时
直接拒绝，越权的表达力因此不存在（TD-032）。
"""

from __future__ import annotations

import typer


def register_admin_commands(app: typer.Typer, console) -> None:
    admin_app = typer.Typer(
        add_completion=False,
        no_args_is_help=True,
        help="企业管理员账号管理",
    )

    @admin_app.command("create")
    def create(
        username: str = typer.Option(..., prompt="用户名", help="登录用户名"),
        password: str = typer.Option(
            ...,
            prompt="密码",
            hide_input=True,
            confirmation_prompt=True,
            help="至少 8 位；不会写入日志",
        ),
        role: str = typer.Option(
            "owner", "--role", help="owner/admin/member"
        ),
        email: str = typer.Option("", "--email", help="可选邮箱"),
    ) -> None:
        """冷启动创建**第一个**企业账号（日常管理走 A2 的 /api/admin/users）。"""
        from ubismart.config import get_settings
        from ubismart.control.accounts import AccountError
        from ubismart.control.bootstrap import create_first_admin

        try:
            row = create_first_admin(
                get_settings(),
                username,
                password,
                email=email,
                role=role,
            )
        except AccountError as exc:
            console.print(f"[red]{exc}[/]")
            raise typer.Exit(code=1) from exc
        console.print(
            f"[green]已创建 {row['username']}（{row['role']}）[/]"
        )
        console.print(
            "[dim]启用登录请在服务端设置 "
            "AGENT_WEB_ACCOUNTS_ENABLED=1，并重启 Web 服务。[/]"
        )

    app.add_typer(admin_app, name="admin")
