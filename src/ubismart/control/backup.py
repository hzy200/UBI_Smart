"""A2 控制面数据库备份/恢复（Phase D3）。

只处理 A2 自己的业务数据库；checkpoint、工作区、API Key 不在备份范围内。
SQLite 直接复制文件；MySQL 走 mysqldump/mysql，密码通过 MYSQL_PWD 传递，
不出现在命令行。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


@dataclass(frozen=True)
class BackupResult:
    path: str
    kind: str
    bytes: int


def default_backup_dir() -> Path:
    env = os.environ.get("AGENT_CONTROL_BACKUP_DIR", "").strip()
    return Path(env) if env else Path.cwd() / "backups"


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S")


def _sqlite_checkpoint(source: Path) -> None:
    engine = create_engine(
        f"sqlite:///{source.as_posix()}", future=True
    )
    try:
        with engine.connect() as connection:
            connection.execute(text("PRAGMA wal_checkpoint(FULL)"))
    finally:
        engine.dispose()


def _mysqldump(parsed: Any, path: Path) -> None:
    exe = shutil.which("mysqldump")
    if not exe:
        raise RuntimeError(
            "未找到 mysqldump；请安装 MySQL 客户端或改用 SQLite"
        )
    database = str(parsed.database or "")
    if not database:
        raise RuntimeError("MySQL URL 缺少数据库名")
    command = [
        exe,
        "--host",
        str(parsed.host or "127.0.0.1"),
        "--port",
        str(parsed.port or 3306),
        "--single-transaction",
        "--no-tablespaces",
        "--routines",
        "--triggers",
        "--default-character-set=utf8mb4",
    ]
    if parsed.username:
        command += ["--user", str(parsed.username)]
    command.append(database)
    env = os.environ.copy()
    if parsed.password:
        env["MYSQL_PWD"] = str(parsed.password)
    with path.open("wb") as handle:
        proc = subprocess.run(
            command,
            stdout=handle,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
    if proc.returncode != 0:
        path.unlink(missing_ok=True)
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip()
        raise RuntimeError(f"mysqldump 失败：{detail or proc.returncode}")


def _mysql_restore(parsed: Any, source: Path) -> None:
    exe = shutil.which("mysql")
    if not exe:
        raise RuntimeError("未找到 mysql 客户端；无法恢复 MySQL 备份")
    database = str(parsed.database or "")
    if not database:
        raise RuntimeError("MySQL URL 缺少数据库名")
    command = [
        exe,
        "--host",
        str(parsed.host or "127.0.0.1"),
        "--port",
        str(parsed.port or 3306),
    ]
    if parsed.username:
        command += ["--user", str(parsed.username)]
    command.append(database)
    env = os.environ.copy()
    if parsed.password:
        env["MYSQL_PWD"] = str(parsed.password)
    with source.open("rb") as handle:
        proc = subprocess.run(
            command,
            stdin=handle,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
    if proc.returncode != 0:
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip()
        raise RuntimeError(f"mysql 恢复失败：{detail or proc.returncode}")


def _prune(directory: Path, prefix: str, keep: int) -> None:
    if keep <= 0:
        return
    files = sorted(
        directory.glob(f"{prefix}-*"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in files[keep:]:
        path.unlink(missing_ok=True)


def backup(
    url: str,
    target_dir: str | Path | None = None,
    *,
    keep: int = 7,
    prefix: str = "control",
) -> BackupResult:
    parsed = make_url(str(url))
    directory = Path(target_dir) if target_dir else default_backup_dir()
    directory.mkdir(parents=True, exist_ok=True)
    kind = parsed.get_backend_name()
    stamp = _stamp()
    if kind == "sqlite":
        source = Path(parsed.database or "")
        if not source.is_file():
            raise RuntimeError(f"SQLite 数据库不存在：{source}")
        path = directory / f"{prefix}-{stamp}.db"
        _sqlite_checkpoint(source)
        shutil.copy2(source, path)
    elif kind == "mysql":
        path = directory / f"{prefix}-{stamp}.sql"
        _mysqldump(parsed, path)
    else:
        raise RuntimeError(f"不支持的数据库类型：{kind}")
    _prune(directory, prefix, keep)
    return BackupResult(
        path=str(path), kind=kind, bytes=path.stat().st_size
    )


def restore(
    url: str,
    source: str | Path,
    *,
    backup_current: bool = True,
    target_dir: str | Path | None = None,
) -> str:
    parsed = make_url(str(url))
    source_path = Path(source)
    if not source_path.is_file():
        raise RuntimeError(f"备份文件不存在：{source_path}")
    kind = parsed.get_backend_name()
    if backup_current:
        if kind == "sqlite":
            target = Path(parsed.database or "")
            if target.is_file():
                backup(url, target_dir)
        else:
            backup(url, target_dir)
    if kind == "sqlite":
        target = Path(parsed.database or "")
        if not str(target):
            raise RuntimeError("SQLite URL 缺少数据库路径")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target)
    elif kind == "mysql":
        _mysql_restore(parsed, source_path)
    else:
        raise RuntimeError(f"不支持的数据库类型：{kind}")
    return str(source_path)
