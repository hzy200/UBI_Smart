"""Alembic 环境：URL 优先取配置，其次取 AGENT_CONTROL_DB_URL。"""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool

from ubismart.control.schema import Base

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    url = str(config.get_main_option("sqlalchemy.url") or "").strip()
    if url:
        return url
    from ubismart.config import get_settings

    url = str(get_settings().control_db_url or "").strip()
    if not url:
        raise RuntimeError("AGENT_CONTROL_DB_URL 未配置")
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool, future=True)
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
