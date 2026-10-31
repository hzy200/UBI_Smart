"""RFC-0001：旧数据回填脚本默认 dry-run、显式 --apply 才写库。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ubismart.control.migrate import upgrade
from ubismart.control.schema import ControlSession

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "migrate_workspaces.py"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )


def _db(tmp_path) -> str:
    url = f"sqlite:///{tmp_path / 'control.db'}"
    upgrade(url)
    engine = create_engine(url, future=True)
    with Session(engine) as session, session.begin():
        session.add(
            ControlSession(
                tenant="t1",
                thread_id="t1",
                workspace="/srv/ws/t1/t1/demo",
                title="Demo",
            )
        )
    engine.dispose()
    return url


def _workspace_ids(url: str) -> list[str]:
    engine = create_engine(url, future=True)
    try:
        with Session(engine) as session:
            return list(
                session.scalars(select(ControlSession.workspace_id)).all()
            )
    finally:
        engine.dispose()


def test_default_is_dry_run(tmp_path) -> None:
    url = _db(tmp_path)
    result = _run("--url", url)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["ok"] is True
    assert report["dry_run"] is True
    assert report["created"] == 1
    assert _workspace_ids(url) == [""]


def test_apply_writes_workspace_id(tmp_path) -> None:
    url = _db(tmp_path)
    result = _run("--url", url, "--apply")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["dry_run"] is False
    assert report["created"] == 1
    ids = _workspace_ids(url)
    assert len(ids) == 1
    assert ids[0]


def test_apply_is_idempotent_after_reset(tmp_path) -> None:
    url = _db(tmp_path)
    assert _run("--url", url, "--apply").returncode == 0
    engine = create_engine(url, future=True)
    with Session(engine) as session, session.begin():
        row = session.scalars(select(ControlSession)).one()
        row.workspace_id = ""
    engine.dispose()

    result = _run("--url", url, "--apply")
    report = json.loads(result.stdout)
    assert report["reused"] == 1
    assert report["created"] == 0


def test_missing_url_returns_two(monkeypatch) -> None:
    monkeypatch.delenv("AGENT_CONTROL_DB_URL", raising=False)
    result = _run()
    assert result.returncode == 2
    assert json.loads(result.stdout)["ok"] is False


def test_missing_schema_returns_two(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'empty.db'}"
    result = _run("--url", url)
    assert result.returncode == 2
    assert "control_sessions" in json.loads(result.stdout)["error"]
