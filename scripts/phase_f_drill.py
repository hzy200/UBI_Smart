"""Phase F：控制面迁移 / 回滚 / 备份 / 恢复演练。

    python scripts/phase_f_drill.py
    python scripts/phase_f_drill.py --mysql-url "mysql+pymysql://root:...@127.0.0.1:3306/coding_agent_test"

SQLite 默认在临时目录执行；MySQL 只在库名包含 `test` 时执行，并会清空该库
的表（仅限专用测试库）。演练顺序：

    upgrade → 写入 → 备份 → downgrade base → upgrade → 恢复 → 校验
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from alembic import command  # noqa: E402
from alembic.script import ScriptDirectory  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from ubismart.control.backup import backup, restore  # noqa: E402
from ubismart.control.migrate import (  # noqa: E402
    alembic_config,
    current_revision,
    has_table,
    upgrade,
)
from ubismart.control.schema import Base  # noqa: E402
from ubismart.control.store import ControlStore  # noqa: E402


def _head_revision(url: str) -> str:
    """从迁移脚本读取 head，避免每加一版就改死常量。"""
    head = ScriptDirectory.from_config(alembic_config(url)).get_current_head()
    if not head:
        raise RuntimeError("迁移脚本没有 head revision")
    return str(head)


def _reset_mysql(url: str) -> None:
    engine = create_engine(url, future=True)
    try:
        Base.metadata.drop_all(engine)
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
    finally:
        engine.dispose()


def _drill(url: str, workdir: Path, *, reset=None) -> dict:
    if reset is not None:
        reset(url)
    expected = _head_revision(url)
    steps: dict = {}
    steps["upgrade"] = upgrade(url)
    if steps["upgrade"].get("revision") != expected:
        raise RuntimeError(f"迁移 revision 不符：{steps['upgrade']}")

    store = ControlStore(url, auto_create=False)
    store.save_sessions(
        "local", [{"thread_id": "drill", "title": "before"}], set()
    )
    store.close()

    result = backup(url, workdir / "backups", keep=2)
    steps["backup"] = {"path": result.path, "bytes": result.bytes}
    if result.bytes <= 0:
        raise RuntimeError("备份文件为空")

    command.downgrade(alembic_config(url), "base")
    steps["downgraded"] = not has_table(url, "control_sessions")
    if not steps["downgraded"]:
        raise RuntimeError("downgrade 后表仍存在")

    steps["reupgrade"] = upgrade(url)
    store = ControlStore(url, auto_create=False)
    empty = store.load_sessions("local")
    store.close()
    steps["empty_after_reupgrade"] = empty == ([], set())
    if not steps["empty_after_reupgrade"]:
        raise RuntimeError("重升级后不应保留旧数据")

    restore(url, result.path, backup_current=False)
    store = ControlStore(url, auto_create=False)
    rows, _ = store.load_sessions("local")
    store.close()
    steps["restored_title"] = rows[0]["title"] if rows else ""
    if steps["restored_title"] != "before":
        raise RuntimeError("恢复后数据不一致")
    steps["revision"] = current_revision(url)
    if steps["revision"] != expected:
        raise RuntimeError(f"恢复后 revision 不符：{steps['revision']}")
    return steps


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase F 迁移/回滚演练")
    parser.add_argument("--mysql-url", default="")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args(argv)

    tmp = Path(tempfile.mkdtemp(prefix="phase-f-drill-"))
    report: dict = {"sqlite": {}}
    try:
        url = f"sqlite:///{(tmp / 'control.db').as_posix()}"
        report["sqlite"] = _drill(url, tmp)

        if args.mysql_url:
            parsed = make_url(args.mysql_url)
            database = parsed.database or ""
            if "test" not in database:
                raise RuntimeError("MySQL 库名必须包含 test")
            if not shutil.which("mysqldump") or not shutil.which("mysql"):
                raise RuntimeError("缺少 mysqldump/mysql 客户端")
            report["mysql"] = _drill(
                args.mysql_url, tmp, reset=_reset_mysql
            )
    except Exception as exc:  # noqa: BLE001 - CLI 要可读
        print(
            json.dumps(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}", **report},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    finally:
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)

    print(json.dumps({"ok": True, **report}, ensure_ascii=False))
    if args.keep:
        print(f"临时目录：{tmp}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
