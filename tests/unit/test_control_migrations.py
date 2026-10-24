"""Phase D3：Alembic baseline 迁移与旧库 stamp。"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("alembic")

from coding_agent.control.migrate import (  # noqa: E402
    current_revision,
    has_table,
    upgrade,
)
from coding_agent.control.store import ControlStore  # noqa: E402

REVISION = "0001_control_schema"


def _url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'control.db'}"


def test_upgrade_creates_schema_and_is_idempotent(tmp_path) -> None:
    url = _url(tmp_path)
    result = upgrade(url)
    assert result == {"action": "upgrade", "revision": REVISION}
    assert has_table(url, "control_meta")
    assert has_table(url, "control_sessions")
    assert current_revision(url) == REVISION

    again = upgrade(url)
    assert again == {"action": "upgrade", "revision": REVISION}

    store = ControlStore(url, auto_create=False)
    store.save_sessions(
        "local", [{"thread_id": "t1", "title": "迁移后"}], set()
    )
    rows, deleted = store.load_sessions("local")
    assert rows[0]["title"] == "迁移后"
    assert deleted == set()
    store.close()


def test_legacy_create_all_is_stamped_without_rebuild(tmp_path) -> None:
    url = _url(tmp_path)
    store = ControlStore(url)  # Phase B 的 create_all 路径
    store.save_sessions(
        "local", [{"thread_id": "legacy", "title": "旧库"}], set()
    )
    store.close()
    assert current_revision(url) is None

    result = upgrade(url)
    assert result == {"action": "stamp", "revision": REVISION}
    assert current_revision(url) == REVISION

    store = ControlStore(url, auto_create=False)
    rows, _ = store.load_sessions("local")
    assert rows[0]["thread_id"] == "legacy"
    store.close()


def test_upgrade_rejects_empty_url() -> None:
    try:
        upgrade("")
    except ValueError as exc:
        assert "URL" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("空 URL 应该报错")
