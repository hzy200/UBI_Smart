"""真实 MySQL 的控制面契约（迁移 / store / 备份恢复）。

默认跳过。运行前指向一个**独立测试库**（库名必须包含 test）：

    AGENT_TEST_MYSQL_URL=mysql+pymysql://root:...@127.0.0.1:3306/coding_agent_test \
        pytest -m mysql

测试会清空该库中的表；库名不含 test 时直接跳过，避免误删业务库。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from coding_agent.control.backup import backup, restore
from coding_agent.control.migrate import (
    alembic_config,
    current_revision,
    upgrade,
)
from coding_agent.control.store import ControlStore

pytestmark = pytest.mark.mysql


def _url() -> str:
    url = os.environ.get("AGENT_TEST_MYSQL_URL", "").strip()
    if not url:
        # 测试便利：允许直接复用 .env 中配置的控制面测试库。
        from coding_agent.config import Settings

        url = str(Settings().control_db_url or "").strip()
    if not url:
        pytest.skip("未设置 AGENT_TEST_MYSQL_URL")
    database = make_url(url).database or ""
    if "test" not in database:
        pytest.skip("测试库名必须包含 test，避免误删业务库")
    return url


def _reset(url: str) -> None:
    engine = create_engine(url, future=True)
    try:
        with engine.begin() as connection:
            rows = connection.execute(text("SHOW TABLES")).scalars().all()
            for name in rows:
                connection.execute(text(f"DROP TABLE `{name}`"))
    finally:
        engine.dispose()


def _head(url: str) -> str:
    head = ScriptDirectory.from_config(alembic_config(url)).get_current_head()
    assert head
    return str(head)


def test_mysql_upgrade_store_backup_restore(tmp_path: Path) -> None:
    url = _url()
    _reset(url)
    head = _head(url)
    try:
        assert upgrade(url) == {
            "action": "upgrade",
            "revision": head,
        }
        store = ControlStore(url, auto_create=False)
        assert store.ping()
        store.save_sessions(
            "local", [{"thread_id": "t1", "title": "before"}], set()
        )
        rows, _ = store.load_sessions("local")
        assert rows[0]["title"] == "before"

        result = backup(url, tmp_path, keep=2)
        assert result.kind == "mysql"
        assert Path(result.path).is_file()

        store.save_sessions(
            "local", [{"thread_id": "t1", "title": "after"}], set()
        )
        store.close()

        restore(url, result.path, backup_current=False)
        store = ControlStore(url, auto_create=False)
        rows, _ = store.load_sessions("local")
        assert rows[0]["title"] == "before"
        store.close()
        assert current_revision(url) == head
    finally:
        _reset(url)
