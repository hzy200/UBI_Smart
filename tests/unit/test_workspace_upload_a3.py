"""RFC-0001：A3 上传任务、revision 与失败清理。"""

from __future__ import annotations

import asyncio
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace

from ubismart.agent_protocol import AgentContext
from ubismart.config import Settings
from ubismart.servers.agent_server_routes import workspace_import as imports
from ubismart.servers.agent_server_routes.workspace_import import (
    ImportDeps,
    _activate_replace,
    _attach_git_history,
    _create_revision,
    _recover_swap,
    _run_upload,
)
from ubismart.workspace.scan import ScanUnavailable


class _Exec:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def host_run(self, command: str, *, kind: str, cwd: str):
        self.calls.append((kind, command, cwd))
        if kind == "workspace_revision_probe":
            return SimpleNamespace(ok=True, stdout="deadbeef\n", stderr="")
        return SimpleNamespace(ok=True, stdout="", stderr="")


class _Runtime:
    def __init__(self, execution: _Exec) -> None:
        self.execution = execution


async def _noop(_runtime) -> None:
    return None


def _settings(tmp_path, **kwargs) -> Settings:
    return Settings(
        _env_file=None,
        workspace_registry=True,
        workspace_scan_provider="none",
        workspace_archive_root=str(tmp_path / "archive"),
        workspace_extract_max_ratio=200,
        agent_data_root=str(tmp_path / "agent"),
        **kwargs,
    )


def _deps(
    settings: Settings, execution: _Exec | None = None
) -> ImportDeps:
    execution = execution or _Exec()
    return ImportDeps(
        context=lambda _request: None,
        new_runtime=lambda _context: _Runtime(execution),
        close_runtime=_noop,
        settings=settings,
    )


def _zip(path, entries: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)


def _job(target: str) -> dict:
    return {
        "job_id": "job1",
        "workspace_id": "wid",
        "status": "queued",
        "source": "proj.zip",
        "target": target,
        "bytes": 0,
        "files": 0,
        "scan_status": "",
        "scan_detail": "",
        "revision": None,
        "error": "",
        "_cancel": threading.Event(),
    }


def test_create_revision_commits_and_archives(tmp_path) -> None:
    target = tmp_path / "ws"
    target.mkdir()
    (target / "a.txt").write_text("hello", encoding="utf-8")
    (target / ".git").mkdir()
    archive_root = tmp_path / "archive"
    execution = _Exec()

    revision = _create_revision(
        execution,
        str(target),
        workspace_id="wid",
        actor="alice",
        archive_root=str(archive_root),
        label="upload",
    )
    assert revision["git_commit"] == "deadbeef"
    assert revision["bytes"] > 0
    assert revision["files"] >= 1
    assert (target / ".git").exists()
    archive_path = revision["archive_path"]
    assert archive_path.endswith(".tar.gz")
    assert (tmp_path / "archive" / "wid").is_dir()
    assert revision["archive_sha256"]


def test_upload_success_activates_and_cleans(tmp_path, monkeypatch) -> None:
    archive_root = tmp_path / "archive"
    monkeypatch.setattr(imports, "_archive_root", lambda _s: str(archive_root))
    source = tmp_path / "proj.zip"
    _zip(source, {"hello.txt": b"hello"})
    target = tmp_path / "ws" / "demo"
    target.parent.mkdir(parents=True, exist_ok=True)
    # 使用真实路由的命名，防止清理逻辑误删当前任务的 staging。
    workdir = Path(f"{target}.upload-job1")
    job = _job(str(target))
    context = AgentContext(
        workspace=str(target), workspace_id="wid", user="alice"
    )
    asyncio.run(
        _run_upload(
            _deps(_settings(tmp_path)),
            context,
            job,
            archive_path=str(source),
            workdir=str(workdir),
            filename="proj.zip",
            quota_bytes=1000,
            max_files=100,
            max_bytes=0,
        )
    )
    assert job["status"] == "active"
    assert (target / "hello.txt").read_bytes() == b"hello"
    assert job["revision"]["revision_id"]
    assert job["progress"] == 100
    assert job["progress_phase"] == "active"
    assert job["total_bytes"] > 0
    assert job["processed_bytes"] == job["total_bytes"]
    assert job["processed_files"] >= 1
    assert not workdir.exists()


def test_upload_zip_slip_fails_and_cleans(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        imports, "_archive_root", lambda _s: str(tmp_path / "archive")
    )
    source = tmp_path / "evil.zip"
    _zip(source, {"../evil.txt": b"x"})
    target = tmp_path / "ws" / "demo"
    target.parent.mkdir(parents=True)
    workdir = tmp_path / "work"
    job = _job(str(target))
    context = AgentContext(
        workspace=str(target), workspace_id="wid", user="alice"
    )
    asyncio.run(
        _run_upload(
            _deps(_settings(tmp_path)),
            context,
            job,
            archive_path=str(source),
            workdir=str(workdir),
            filename="evil.zip",
            quota_bytes=0,
            max_files=100,
            max_bytes=0,
        )
    )
    assert job["status"] == "failed"
    assert not target.exists()
    assert not workdir.exists()


def test_upload_quota_failure(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        imports, "_archive_root", lambda _s: str(tmp_path / "archive")
    )
    source = tmp_path / "proj.zip"
    _zip(source, {"a.bin": b"0123456789"})
    target = tmp_path / "ws" / "demo"
    target.parent.mkdir(parents=True)
    job = _job(str(target))
    context = AgentContext(
        workspace=str(target), workspace_id="wid", user="alice"
    )
    asyncio.run(
        _run_upload(
            _deps(_settings(tmp_path)),
            context,
            job,
            archive_path=str(source),
            workdir=str(tmp_path / "work"),
            filename="proj.zip",
            quota_bytes=5,
            max_files=100,
            max_bytes=0,
        )
    )
    assert job["status"] == "failed"
    assert not target.exists()


def test_upload_scan_unavailable_fails_closed(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        imports, "_archive_root", lambda _s: str(tmp_path / "archive")
    )

    class _Scanner:
        def scan(self, _root):
            raise ScanUnavailable("no clamav")

    monkeypatch.setattr(imports, "build_scanner", lambda _s: _Scanner())
    source = tmp_path / "proj.zip"
    _zip(source, {"a.txt": b"x"})
    target = tmp_path / "ws" / "demo"
    target.parent.mkdir(parents=True)
    job = _job(str(target))
    context = AgentContext(
        workspace=str(target), workspace_id="wid", user="alice"
    )
    asyncio.run(
        _run_upload(
            _deps(_settings(tmp_path)),
            context,
            job,
            archive_path=str(source),
            workdir=str(tmp_path / "work"),
            filename="proj.zip",
            quota_bytes=0,
            max_files=100,
            max_bytes=0,
        )
    )
    assert job["status"] == "failed"
    assert "扫描不可用" in job["error"]
    assert not target.exists()


def test_activate_replace_swaps_existing_workspace(tmp_path) -> None:
    target = tmp_path / "ws"
    staging = tmp_path / "stage"
    target.mkdir()
    (target / "old.txt").write_text("old", encoding="utf-8")
    staging.mkdir()
    (staging / "new.txt").write_text("new", encoding="utf-8")
    _activate_replace(str(staging), str(target))
    assert not (target / "old.txt").exists()
    assert (target / "new.txt").read_text(encoding="utf-8") == "new"
    assert not list(tmp_path.glob("ws.old-*"))


def test_attach_git_history_preserves_config(tmp_path) -> None:
    target = tmp_path / "ws"
    staging = tmp_path / "stage"
    (target / ".git").mkdir(parents=True)
    (target / ".git" / "config").write_text("orig", encoding="utf-8")
    (target / ".git" / "objects").mkdir()
    (target / ".git" / "objects" / "blob").write_text(
        "blob", encoding="utf-8"
    )
    staging.mkdir()
    _attach_git_history(str(target), str(staging))
    assert (staging / ".git" / "config").read_text(
        encoding="utf-8"
    ) == "orig"
    assert (staging / ".git" / "objects" / "blob").exists()
    (staging / ".git" / "config").write_text("new", encoding="utf-8")
    assert (target / ".git" / "config").read_text(
        encoding="utf-8"
    ) == "orig"


def test_recover_swap_restores_backup(tmp_path) -> None:
    target = tmp_path / "ws"
    backup = tmp_path / "ws.old-deadbeef"
    backup.mkdir()
    (backup / "keep.txt").write_text("keep", encoding="utf-8")
    _recover_swap(str(target))
    assert (target / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert not backup.exists()


def test_upload_cancel_does_not_activate(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        imports, "_archive_root", lambda _s: str(tmp_path / "archive")
    )
    source = tmp_path / "proj.zip"
    _zip(source, {"a.txt": b"x"})
    target = tmp_path / "ws" / "demo"
    target.parent.mkdir(parents=True)
    job = _job(str(target))
    job["_cancel"].set()
    context = AgentContext(
        workspace=str(target), workspace_id="wid", user="alice"
    )
    asyncio.run(
        _run_upload(
            _deps(_settings(tmp_path)),
            context,
            job,
            archive_path=str(source),
            workdir=str(tmp_path / "work"),
            filename="proj.zip",
            quota_bytes=0,
            max_files=100,
            max_bytes=0,
        )
    )
    assert job["status"] == "cancelled"
    assert not target.exists()
