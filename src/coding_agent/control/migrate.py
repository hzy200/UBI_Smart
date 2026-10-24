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
        # Phase B 的旧库没有 alembic_version；只标记基线，不重建。
        command.stamp(config, "head")
        return {"action": "stamp", "revision": current_revision(url)}

    command.upgrade(config, "head")
    return {"action": "upgrade", "revision": current_revision(url)}


def metadata() -> Any:
    """供测试/诊断使用。"""
    return Base.metadata
