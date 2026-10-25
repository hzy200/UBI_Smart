"""M4：A3 导入/上传任务状态持久化与重启恢复。"""

from __future__ import annotations

import json
import os
import threading
import time

from coding_agent.agent_server_routes.imports import (
    ImportDeps,
    _jobs_root,
    _load_jobs,
    _persist_job,
    _update,
)
from coding_agent.config import Settings


async def _noop(_runtime) -> None:
    return None


def _deps(tmp_path, jobs=None) -> ImportDeps:
    settings = Settings(
        _env_file=None,
        agent_data_root=str(tmp_path / "agent"),
        workspace_registry=True,
    )
    return ImportDeps(
        context=lambda _request: None,
        new_runtime=lambda _context: None,
        close_runtime=_noop,
        settings=settings,
        jobs=jobs if jobs is not None else {},
    )


def _job(job_id: str, **overrides) -> dict:
    data = {
        "job_id": job_id,
        "workspace_id": "wid",
        "kind": "import",
        "status": "queued",
        "source": "/mnt/d/proj",
        "target": "",
        "bytes": 0,
        "files": 0,
        "error": "",
        "staging": "",
        "created_at": "2026-10-25T00:00:00+00:00",
        "_cancel": threading.Event(),
    }
    data.update(overrides)
    return data


def test_persist_excludes_runtime_event(tmp_path) -> None:
    deps = _deps(tmp_path)
    job = _job("j1")
    _persist_job(deps, job)
    path = _jobs_root(deps.settings) / "j1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["job_id"] == "j1"
    assert "_cancel" not in payload


def test_update_persists_status(tmp_path) -> None:
    deps = _deps(tmp_path)
    job = _job("j1")
    _persist_job(deps, job)
    _update(deps, job, status="copying")
    payload = json.loads(
        (_jobs_root(deps.settings) / "j1.json").read_text(
            encoding="utf-8"
        )
    )
    assert payload["status"] == "copying"
    assert payload["updated_at"]


def test_load_terminal_job(tmp_path) -> None:
    deps = _deps(tmp_path)
    _persist_job(
        deps, _job("j1", status="active", revision={"revision_id": "r1"})
    )
    loaded: dict = {}
    _load_jobs(_deps(tmp_path, loaded))
    assert loaded["j1"]["status"] == "active"
    assert loaded["j1"]["revision"]["revision_id"] == "r1"


def test_incomplete_job_is_failed_and_cleaned(tmp_path) -> None:
    deps = _deps(tmp_path)
    staging = tmp_path / "ws.import-j1"
    workdir = tmp_path / "ws.upload-j1"
    staging.mkdir()
    workdir.mkdir()
    (staging / "x").write_text("x", encoding="utf-8")
    _persist_job(
        deps,
        _job(
            "j1",
            status="copying",
            staging=str(staging),
            workdir=str(workdir),
        ),
    )
    loaded: dict = {}
    _load_jobs(_deps(tmp_path, loaded))
    assert loaded["j1"]["status"] == "failed"
    assert "重启" in loaded["j1"]["error"]
    assert not staging.exists()
    assert not workdir.exists()


def test_stale_terminal_job_is_removed(tmp_path) -> None:
    deps = _deps(tmp_path)
    _persist_job(deps, _job("old", status="active"))
    path = _jobs_root(deps.settings) / "old.json"
    old = time.time() - 30 * 86400
    os.utime(path, (old, old))
    loaded: dict = {}
    _load_jobs(_deps(tmp_path, loaded))
    assert "old" not in loaded
    assert not path.exists()


def test_load_recovers_interrupted_swap(tmp_path) -> None:
    deps = _deps(tmp_path)
    target = tmp_path / "ws"
    backup = tmp_path / "ws.old-deadbeef"
    backup.mkdir()
    (backup / "keep.txt").write_text("keep", encoding="utf-8")
    _persist_job(
        deps,
        _job("j1", status="activating", target=str(target)),
    )
    loaded: dict = {}
    _load_jobs(_deps(tmp_path, loaded))
    assert (target / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert not backup.exists()
    assert loaded["j1"]["status"] == "failed"
