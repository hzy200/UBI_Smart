"""Phase D3：SQLite 控制面备份/恢复。"""

from __future__ import annotations

from pathlib import Path

from coding_agent.control.backup import backup, restore
from coding_agent.control.store import ControlStore


def _url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'control.db'}"


def test_sqlite_backup_and_restore(tmp_path) -> None:
    url = _url(tmp_path)
    store = ControlStore(url)
    store.save_sessions(
        "local", [{"thread_id": "t1", "title": "before"}], set()
    )
    store.close()

    result = backup(url, tmp_path / "backups", keep=7)
    path = Path(result.path)
    assert path.is_file()
    assert result.kind == "sqlite"
    assert result.bytes > 0

    store = ControlStore(url, auto_create=False)
    store.save_sessions(
        "local", [{"thread_id": "t1", "title": "after"}], set()
    )
    store.close()

    restore(url, path, backup_current=False)
    store = ControlStore(url, auto_create=False)
    rows, deleted = store.load_sessions("local")
    assert rows[0]["title"] == "before"
    assert deleted == set()
    store.close()


def test_restore_missing_backup_fails(tmp_path) -> None:
    url = _url(tmp_path)
    ControlStore(url).close()
    try:
        restore(url, tmp_path / "missing.db", backup_current=False)
    except RuntimeError as exc:
        assert "不存在" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("缺失备份应该报错")
