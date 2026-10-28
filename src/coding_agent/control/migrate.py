"""A2 控制面数据库迁移入口（Phase D3）。

契约：

- 新库：`alembic upgrade head` 建表并写 `alembic_version`；
- 旧库（Phase B 用 `create_all` 建过表、没有 `alembic_version`）：
  先 `stamp head`，不重复建表；
- 迁移前可选备份；备份失败不继续迁移。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, pool, text

from coding_agent.control.schema import Base

_LOG = logging.getLogger(__name__)

_MIGRATIONS = Path(__file__).resolve().parent / "migrations"
_INI = Path(__file__).resolve().parent / "alembic.ini"

# Phase B 的 create_all 只建过这一版基线。旧库没有 alembic_version 时，
# 必须先 stamp 到它，再 upgrade，否则会漏掉后续增量迁移。
BASELINE_REVISION = "0001_control_schema"


def alembic_config(url: str) -> Config:
    config = Config(str(_INI))
    config.set_main_option("script_location", str(_MIGRATIONS))
    config.set_main_option("sqlalchemy.url", str(url))
    return config


def _connect(url: str):
    return create_engine(url, poolclass=pool.NullPool, future=True)


def has_table(url: str, name: str) -> bool:
    engine = _connect(url)
    try:
        return bool(inspect(engine).has_table(name))
    finally:
        engine.dispose()


def has_column(url: str, table: str, column: str) -> bool:
    engine = _connect(url)
    try:
        inspector = inspect(engine)
        if not inspector.has_table(table):
            return False
        return any(
            item["name"] == column
            for item in inspector.get_columns(table)
        )
    finally:
        engine.dispose()


def _is_current_schema(url: str) -> bool:
    """判断旧库是否已经是“新 create_all”建出的最新结构。

    `Base.metadata.create_all` 会给新库直接建出 workspaces / workspace_id；
    这类库只需补 alembic_version，不能重复执行 0002。
    """
    return (
        has_table(url, "workspaces")
        and has_column(url, "control_sessions", "workspace_id")
        and has_column(url, "workspace_revisions", "pinned")
        and has_table(url, "run_leases")
        and has_table(url, "idempotency_keys")
        and has_table(url, "control_auth_events")
        and has_table(url, "control_quota_reservations")
        and has_column(url, "control_usage_cursor", "user")
        and has_column(url, "control_user_quotas", "daily_cost_cny")
    )


def current_revision(url: str) -> str | None:
    engine = _connect(url)
    try:
        if not inspect(engine).has_table("alembic_version"):
            return None
        with engine.connect() as connection:
            return connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar()
    finally:
        engine.dispose()


def upgrade(
    url: str,
    *,
    backup: bool = False,
    backup_dir: str | Path | None = None,
) -> dict[str, Any]:
    """把数据库升级到 head；返回动作与最终 revision。"""
    url = str(url or "").strip()
    if not url:
        raise ValueError("control DB URL 为空")
    existing = has_table(url, "control_meta")
    if backup and existing:
        from coding_agent.control.backup import backup as backup_db

        backup_db(url, backup_dir)
        _LOG.info("迁移前已备份控制面数据库")

    config = alembic_config(url)
    if existing and current_revision(url) is None:
        if _is_current_schema(url):
            # 新 create_all 已建出最新结构，只需补版本标记。
            command.stamp(config, "head")
            return {"action": "stamp", "revision": current_revision(url)}
        # Phase B 旧库只建过基线：先 stamp 到 0001，再执行增量迁移。
        command.stamp(config, BASELINE_REVISION)
        command.upgrade(config, "head")
        return {"action": "upgrade", "revision": current_revision(url)}

    command.upgrade(config, "head")
    return {"action": "upgrade", "revision": current_revision(url)}


def metadata() -> Any:
    """供测试/诊断使用。"""
    return Base.metadata


def _main() -> int:
    import argparse
    import json

    from coding_agent.config import get_settings

    parser = argparse.ArgumentParser(description="迁移控制面数据库")
    parser.add_argument(
        "--url", default="", help="默认读取 AGENT_CONTROL_DB_URL"
    )
    parser.add_argument("--backup", action="store_true", help="迁移前备份")
    parser.add_argument("--backup-dir", default="")
    args = parser.parse_args()
    url = str(args.url or get_settings().control_db_url or "").strip()
    if not url:
        print("必须提供 --url 或设置 AGENT_CONTROL_DB_URL")
        return 2
    try:
        result = upgrade(
            url,
            backup=bool(args.backup),
            backup_dir=args.backup_dir or None,
        )
    except Exception as exc:  # noqa: BLE001 - CLI 边界
        print(
            json.dumps(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                ensure_ascii=False,
            )
        )
        return 1
    print(json.dumps({"ok": True, **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
