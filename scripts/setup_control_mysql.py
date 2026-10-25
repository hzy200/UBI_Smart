"""初始化 A2 控制面的 MySQL 数据库与最小权限账号。

**本脚本不安装 MySQL**，只负责建库、建用户、授权，并给出
`AGENT_CONTROL_DB_URL`。MySQL 本身由部署方安装。

    # 先看要执行的 SQL，不连接数据库
    python scripts/setup_control_mysql.py --dry-run

    # 用一个有建库/建用户权限的账号执行
    python scripts/setup_control_mysql.py \
      --admin-url "mysql+pymysql://root:***@127.0.0.1:3306/" \
      --show-url

默认只授权 SELECT/INSERT/UPDATE/DELETE，不授予 DDL；迁移由
`control_server --migrate` 或 Alembic 使用管理员账号执行。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

_IDENT = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_PASSWORD = re.compile(r"^[A-Za-z0-9_.~!@#%^&*+=:-]{12,128}$")


def _ident(value: str, label: str) -> str:
    text_value = str(value or "").strip()
    if not _IDENT.fullmatch(text_value):
        raise ValueError(
            f"{label} 只允许字母、数字、下划线（1–64）：{value!r}"
        )
    return text_value


def _password(value: str) -> str:
    text_value = str(value or "").strip()
    if not text_value:
        text_value = secrets.token_urlsafe(24)
    if not _PASSWORD.fullmatch(text_value):
        raise ValueError(
            "密码需 12–128 位，且不能包含引号、反斜杠或空白"
        )
    return text_value


def build_statements(
    database: str, user: str, password: str
) -> list[str]:
    db = _ident(database, "database")
    name = _ident(user, "user")
    secret = _password(password)
    return [
        (
            f"CREATE DATABASE IF NOT EXISTS `{db}` "
            "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        ),
        (
            f"CREATE USER IF NOT EXISTS '{name}'@'localhost' "
            f"IDENTIFIED BY '{secret}'"
        ),
        (
            f"ALTER USER '{name}'@'localhost' "
            f"IDENTIFIED BY '{secret}'"
        ),
        (
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON `{db}`.* "
            f"TO '{name}'@'localhost'"
        ),
    ]


def target_url(
    host: str, port: int, database: str, user: str, password: str
) -> URL:
    return URL.create(
        drivername="mysql+pymysql",
        username=_ident(user, "user"),
        password=_password(password),
        host=host,
        port=int(port),
        database=_ident(database, "database"),
    )


def _redacted(url: URL) -> str:
    return str(url.set(password="***"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="初始化控制面 MySQL 数据库与最小权限账号"
    )
    parser.add_argument(
        "--admin-url",
        default=os.environ.get("AGENT_MYSQL_ADMIN_URL", ""),
        help="有建库/建用户权限的 MySQL URL",
    )
    parser.add_argument("--database", default="coding_agent")
    parser.add_argument("--user", default="coding_agent")
    parser.add_argument(
        "--password",
        default=os.environ.get("AGENT_MYSQL_PASSWORD", ""),
        help="应用账号密码；空则随机生成",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3306)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--show-url",
        action="store_true",
        help="输出含密码的 AGENT_CONTROL_DB_URL（默认脱敏）",
    )
    args = parser.parse_args(argv)

    try:
        password = _password(args.password)
        statements = build_statements(
            args.database, args.user, password
        )
        url = target_url(
            args.host, args.port, args.database, args.user, password
        )
    except ValueError as exc:
        print(
            json.dumps(
                {"ok": False, "error": str(exc)}, ensure_ascii=False
            )
        )
        return 2

    if args.dry_run:
        print(
            json.dumps(
                {
                    "ok": True,
                    "dry_run": True,
                    "statements": [
                        item.replace(password, "<redacted>")
                        for item in statements
                    ],
                    "url": _redacted(url),
                },
                ensure_ascii=False,
            )
        )
        return 0

    admin = str(args.admin_url or "").strip()
    if not admin:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "缺少 --admin-url 或 AGENT_MYSQL_ADMIN_URL",
                },
                ensure_ascii=False,
            )
        )
        return 2
    try:
        engine = create_engine(admin, pool_pre_ping=True, future=True)
        with engine.begin() as connection:
            for statement in statements:
                connection.execute(text(statement))
        engine.dispose()
    except Exception as exc:  # noqa: BLE001 - 脚本边界
        print(
            json.dumps(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                ensure_ascii=False,
            )
        )
        return 1

    print(
        json.dumps(
            {
                "ok": True,
                "host": args.host,
                "port": int(args.port),
                "database": args.database,
                "user": args.user,
                "url": (
                    str(url) if args.show_url else _redacted(url)
                ),
                "note": (
                    "先运行 control_server --migrate（使用管理员账号），"
                    "再让 A2 使用该 URL"
                ),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
