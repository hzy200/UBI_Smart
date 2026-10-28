"""企业管理员 CLI：初始化首个账号。"""

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
        """在控制面数据库创建企业账号。"""
        from coding_agent.config import get_settings
        from coding_agent.control.accounts import AccountError, UserStore
        from coding_agent.control.store import ControlStore

        settings = get_settings()
        url = str(settings.control_db_url or "").strip()
        if not url:
            console.print(
                "[red]未配置 AGENT_CONTROL_DB_URL；账号必须保存在控制面数据库。[/]"
            )
            raise typer.Exit(code=1)
        store = ControlStore(
            url, auto_create=bool(settings.control_db_auto_create)
        )
        try:
            row = UserStore(
                store.engine, tenant=settings.web_account_tenant
            ).create(
                username,
                password,
                email=email,
                role=role,
                tenant=settings.web_account_tenant,
            )
        except AccountError as exc:
            console.print(f"[red]{exc}[/]")
            raise typer.Exit(code=1) from exc
        finally:
            store.close()
        console.print(
            f"[green]已创建 {row['username']}（{row['role']}）[/]"
        )
        console.print(
            "[dim]启用登录请在服务端设置 "
            "AGENT_WEB_ACCOUNTS_ENABLED=1，并重启 Web 服务。[/]"
        )

    app.add_typer(admin_app, name="admin")
