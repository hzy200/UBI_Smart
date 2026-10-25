"""Phase D3：SQLite 控制面备份/恢复。"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from coding_agent.control import backup as backup_module
from coding_agent.control.backup import (
    _mysql_restore,
    _mysqldump,
    _prune,
    backup,
    default_backup_dir,
    restore,
)
from coding_agent.control.store import ControlStore

pytestmark = pytest.mark.regression


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


def test_default_backup_dir_prefers_env(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AGENT_CONTROL_BACKUP_DIR", str(tmp_path / "bk"))
    assert default_backup_dir() == tmp_path / "bk"

    monkeypatch.delenv("AGENT_CONTROL_BACKUP_DIR")
    monkeypatch.chdir(tmp_path)
    assert default_backup_dir() == tmp_path / "backups"


def test_prune_keeps_newest_and_ignores_nonpositive_keep(tmp_path) -> None:
    for index in range(3):
        path = tmp_path / f"control-{index}.db"
        path.write_text(str(index), encoding="utf-8")
        os.utime(path, (index, index))

    _prune(tmp_path, "control", 2)
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "control-1.db",
        "control-2.db",
    ]

    _prune(tmp_path, "control", 0)
    assert len(list(tmp_path.iterdir())) == 2


def test_sqlite_backup_missing_source_fails(tmp_path) -> None:
    with pytest.raises(RuntimeError, match="不存在"):
        backup(f"sqlite:///{tmp_path / 'missing.db'}", tmp_path)


def test_unsupported_backend_fails(tmp_path) -> None:
    with pytest.raises(RuntimeError, match="不支持"):
        backup("postgresql://u@h/db", tmp_path)

    source = tmp_path / "dump.sql"
    source.write_text("x", encoding="utf-8")
    with pytest.raises(RuntimeError, match="不支持"):
        restore(
            "postgresql://u@h/db",
            source,
            backup_current=False,
            target_dir=tmp_path,
        )


def test_mysql_dump_passes_password_via_env(tmp_path, monkeypatch) -> None:
    calls: dict[str, object] = {}

    def fake_run(command, **kwargs):
        calls["command"] = command
        calls["env"] = kwargs["env"]
        kwargs["stdout"].write(b"SQL")
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(
        backup_module.shutil, "which", lambda name: f"/usr/bin/{name}"
    )
    monkeypatch.setattr(backup_module.subprocess, "run", fake_run)

    result = backup(
        "mysql+pymysql://user:secret@db.local:3307/app",
        tmp_path,
        prefix="control",
    )

    assert result.kind == "mysql"
    assert result.path.endswith(".sql")
    assert Path(result.path).read_bytes() == b"SQL"
    command = calls["command"]
    assert "--user" in command and "user" in command
    assert all("secret" not in str(part) for part in command)
    assert calls["env"]["MYSQL_PWD"] == "secret"
    assert command[-1] == "app"


def test_mysql_dump_failure_removes_partial_file(
    tmp_path, monkeypatch
) -> None:
    seen: list[Path] = []

    def fake_run(command, **kwargs):
        seen.append(Path(kwargs["stdout"].name))
        return SimpleNamespace(returncode=2, stderr=b"denied")

    monkeypatch.setattr(
        backup_module.shutil, "which", lambda name: f"/usr/bin/{name}"
    )
    monkeypatch.setattr(backup_module.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="denied"):
        backup("mysql+pymysql://u:p@h/app", tmp_path)
    assert seen and not seen[0].exists()


def test_mysql_requires_client_and_database(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(backup_module.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="mysqldump"):
        backup("mysql+pymysql://u:p@h/app", tmp_path)

    monkeypatch.setattr(
        backup_module.shutil, "which", lambda name: f"/usr/bin/{name}"
    )
    with pytest.raises(RuntimeError, match="数据库名"):
        backup("mysql+pymysql://u:p@h/", tmp_path)


def test_mysql_restore_success_and_missing_client(
    tmp_path, monkeypatch
) -> None:
    source = tmp_path / "dump.sql"
    source.write_text("SQL", encoding="utf-8")
    calls: dict[str, object] = {}

    def fake_run(command, **kwargs):
        calls["command"] = command
        calls["data"] = kwargs["stdin"].read()
        calls["env"] = kwargs["env"]
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(
        backup_module.shutil, "which", lambda name: f"/usr/bin/{name}"
    )
    monkeypatch.setattr(backup_module.subprocess, "run", fake_run)
    _mysql_restore(
        backup_module.make_url("mysql+pymysql://u:p@h/app"), source
    )
    assert calls["data"] == b"SQL"

    monkeypatch.setattr(backup_module.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="mysql 客户端"):
        _mysql_restore(
            backup_module.make_url("mysql+pymysql://u:p@h/app"), source
        )


def test_mysql_restore_requires_database(tmp_path, monkeypatch) -> None:
    source = tmp_path / "dump.sql"
    source.write_text("SQL", encoding="utf-8")
    monkeypatch.setattr(
        backup_module.shutil, "which", lambda name: f"/usr/bin/{name}"
    )
    with pytest.raises(RuntimeError, match="数据库名"):
        _mysql_restore(backup_module.make_url("mysql+pymysql://u:p@h/"), source)


def test_mysqldump_requires_database(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        backup_module.shutil, "which", lambda name: f"/usr/bin/{name}"
    )
    with pytest.raises(RuntimeError, match="数据库名"):
        _mysqldump(
            backup_module.make_url("mysql+pymysql://u:p@h/"),
            tmp_path / "x.sql",
        )


def test_mysql_restore_failure_raises(tmp_path, monkeypatch) -> None:
    source = tmp_path / "dump.sql"
    source.write_text("SQL", encoding="utf-8")
    monkeypatch.setattr(
        backup_module.shutil, "which", lambda name: f"/usr/bin/{name}"
    )
    monkeypatch.setattr(
        backup_module.subprocess,
        "run",
        lambda command, **kwargs: SimpleNamespace(
            returncode=3, stderr=b"denied"
        ),
    )
    with pytest.raises(RuntimeError, match="mysql 恢复失败"):
        _mysql_restore(
            backup_module.make_url("mysql+pymysql://u:p@h/app"), source
        )


def test_restore_mysql_flow_with_current_backup(
    tmp_path, monkeypatch
) -> None:
    source = tmp_path / "dump.sql"
    source.write_text("SQL", encoding="utf-8")
    restored: list[bytes] = []

    def fake_run(command, **kwargs):
        if kwargs.get("stdout") is not None:
            kwargs["stdout"].write(b"current")
        if kwargs.get("stdin") is not None:
            restored.append(kwargs["stdin"].read())
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(
        backup_module.shutil, "which", lambda name: f"/usr/bin/{name}"
    )
    monkeypatch.setattr(backup_module.subprocess, "run", fake_run)

    target_dir = tmp_path / "bk"
    restore(
        "mysql+pymysql://u:p@h/app",
        source,
        backup_current=True,
        target_dir=target_dir,
    )
    restore(
        "mysql+pymysql://u:p@h/app",
        source,
        backup_current=False,
        target_dir=target_dir,
    )

    assert list(target_dir.glob("control-*.sql"))
    assert restored == [b"SQL", b"SQL"]


def test_restore_backs_up_current_database(tmp_path) -> None:
    url = _url(tmp_path)
    ControlStore(url).close()
    source = backup(url, tmp_path / "backups")

    target_dir = tmp_path / "auto"
    restore(
        url,
        source.path,
        backup_current=True,
        target_dir=target_dir,
    )
    assert list(target_dir.glob("control-*.db"))
