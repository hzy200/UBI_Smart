"""A3 工作区导入任务（RFC-0001）。

复制由 A3→A4 的受控宿主命令完成，A2 不接触工作区文件。

任务状态：

    queued → preparing → copying → verifying → active
                                        └──→ failed / cancelled

限制与诚实边界：

- 取消是**协作式**的：已启动的 tar 不会被强杀，工作线程在复制结束后
  检查取消标记并清理 staging，不会激活目标；
- 任务状态在 A3 进程内存里；A3 重启后未完成任务丢失，但 staging 目录
  带 `.import-<job_id>` 前缀，可由运维清理；
- 进度是阶段级，不是逐文件百分比。
"""

from __future__ import annotations

import asyncio
import glob
import hashlib
import json
import os
import re
import shlex
import shutil
import tarfile
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)

from coding_agent.agent_protocol import AgentContext
from coding_agent.agent_server_models import (
    WorkspaceArchiveDeleteBody,
    WorkspaceImportBody,
    WorkspaceRestoreBody,
    WorkspaceRollbackBody,
)
from coding_agent.config import Settings
from coding_agent.paths import (
    PathGuardError,
    ensure_inside,
    is_within,
    normalize_root,
)
from coding_agent.workspace.archive import ExtractLimits, safe_extract
from coding_agent.workspace.scan import ScanUnavailable, build_scanner


@dataclass(frozen=True)
class ImportDeps:
    context: Callable[[Request], AgentContext]
    new_runtime: Callable[[AgentContext], Any]
    close_runtime: Callable[[Any], Awaitable[None]]
    settings: Settings
    jobs: dict[str, dict[str, Any]] = field(default_factory=dict)
    lock: Any = field(default_factory=threading.Lock)


class _Cancelled(Exception):
    pass


_TERMINAL = {"active", "failed", "cancelled"}
# 导入/上传是后台任务；没有上限会在异常挂起时永久占着状态。
_JOB_TIMEOUT_S = 3600.0


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _jobs_root(settings: Settings) -> Path:
    base = str(settings.agent_data_root or "").strip()
    if base:
        return Path(base) / "workspace_jobs"
    return Path(settings.resolved_audit_dir).parent / "workspace_jobs"


def _write_job_file(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        key: value
        for key, value in data.items()
        if not str(key).startswith("_")
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _persist_job(deps: ImportDeps, job: dict[str, Any]) -> None:
    try:
        path = _jobs_root(deps.settings) / f"{job['job_id']}.json"
        _write_job_file(path, job)
    except OSError:
        # 状态落盘失败不应中断任务；内存态仍可用于本次运行。
        pass


def _load_jobs(deps: ImportDeps) -> None:
    """启动时载入任务；未完成的标记失败并清理 staging。"""
    root = _jobs_root(deps.settings)
    if not root.is_dir():
        return
    keep_s = max(1, int(deps.settings.workspace_job_retention_days or 7))
    cutoff = time.time() - keep_s * 86400
    try:
        paths = sorted(root.glob("*.json"))
    except OSError:
        return
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        job_id = str(data.get("job_id") or path.stem)
        status = str(data.get("status") or "")
        if status not in _TERMINAL:
            target = str(data.get("target") or "")
            if target:
                _recover_swap(target)
            for key in ("staging", "workdir"):
                leftover = str(data.get(key) or "")
                if leftover:
                    shutil.rmtree(leftover, ignore_errors=True)
            data.update(
                status="failed",
                error="A3 重启，任务中断",
                staging="",
                workdir="",
            )
            _write_job_file(path, data)
        elif path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)
            continue
        data["_cancel"] = threading.Event()
        deps.jobs[job_id] = data


def _roots(settings: Settings) -> tuple[str, ...]:
    raw = str(settings.workspace_mount_roots or "")
    found: list[str] = []
    for item in raw.split(","):
        text = item.strip()
        if not text:
            continue
        try:
            found.append(normalize_root(text))
        except PathGuardError:
            continue
    return tuple(found)


def validate_source(
    settings: Settings, source: str, *, target: str
) -> str:
    """校验导入源：必须在允许的本地根内，且不能是目标自身/祖先。"""
    try:
        normalized = normalize_root(str(source or "").strip())
    except PathGuardError as exc:
        raise HTTPException(
            status_code=400, detail=f"导入源路径非法：{exc}"
        ) from exc
    roots = _roots(settings)
    if not roots:
        raise HTTPException(
            status_code=400,
            detail="未配置 AGENT_WORKSPACE_MOUNT_ROOTS，禁止导入本地目录",
        )
    if not any(is_within(normalized, root) for root in roots):
        raise HTTPException(
            status_code=403,
            detail=f"导入源不在允许的根内：{normalized}",
        )
    if normalized == target or is_within(target, normalized):
        raise HTTPException(
            status_code=400,
            detail="导入源不能是目标目录自身或其祖先",
        )
    if not Path(normalized).is_dir():
        raise HTTPException(
            status_code=400, detail=f"导入源不存在或不是目录：{normalized}"
        )
    return normalized


def tar_command(source: str, staging: str) -> str:
    # `.agent` 含审计/checkpoint/备份，不随项目导入。
    return (
        f"tar -C {shlex.quote(source)} --exclude=./.agent -cf - . "
        f"| tar -C {shlex.quote(staging)} -xf -"
    )


def _tree_size(root: str) -> tuple[int, int]:
    total = 0
    files = 0
    for base, _dirs, names in os.walk(root, followlinks=False):
        for name in names:
            path = os.path.join(base, name)
            try:
                total += os.stat(path, follow_symlinks=False).st_size
            except OSError:
                continue
            files += 1
    return total, files


def _activate(staging: str, target: str) -> None:
    if os.path.exists(target):
        if os.path.isdir(target) and not os.listdir(target):
            os.rmdir(target)
        else:
            raise RuntimeError("目标工作区已存在且非空")
    os.replace(staging, target)


def _link_or_copy(src: str, dst: str) -> str:
    """优先硬链接（同一 ext4），失败再复制；config/HEAD 始终复制。"""
    if os.path.basename(src) in {"config", "HEAD"}:
        return shutil.copy2(src, dst)
    try:
        os.link(src, dst)
        return dst
    except OSError:
        return shutil.copy2(src, dst)


def _attach_git_history(target: str, staging: str) -> None:
    """把当前工作区的 .git 接入 staging，保证 revision 历史连续。"""
    source = os.path.join(target, ".git")
    if not os.path.isdir(source):
        return
    destination = os.path.join(staging, ".git")
    if os.path.isdir(destination):
        shutil.rmtree(destination, ignore_errors=True)
    shutil.copytree(
        source,
        destination,
        copy_function=_link_or_copy,
        symlinks=True,
        dirs_exist_ok=True,
    )


def _activate_replace(staging: str, target: str) -> None:
    """原子替换工作区；旧目录先备份，失败恢复。"""
    if not os.path.exists(target):
        os.replace(staging, target)
        return
    backup = f"{target}.old-{uuid.uuid4().hex[:8]}"
    os.replace(target, backup)
    try:
        os.replace(staging, target)
    except Exception:
        os.replace(backup, target)
        raise
    shutil.rmtree(backup, ignore_errors=True)


def _recover_swap(target: str) -> None:
    """恢复上次中断留下的 backup，并清理遗留 staging。"""
    parent = os.path.dirname(target) or "/"
    pattern = os.path.join(parent, os.path.basename(target) + ".old-*")
    backups = sorted(glob.glob(pattern))
    if backups and not os.path.exists(target):
        os.replace(backups.pop(), target)
    for leftover in backups:
        shutil.rmtree(leftover, ignore_errors=True)
    for prefix in (".import-", ".upload-"):
        stale = os.path.join(
            parent, os.path.basename(target) + prefix + "*"
        )
        for path in glob.glob(stale):
            shutil.rmtree(path, ignore_errors=True)


def _update(
    deps: ImportDeps, job: dict[str, Any], **fields: Any
) -> None:
    with deps.lock:
        job.update(fields)
        job["updated_at"] = _now_iso()
        _persist_job(deps, job)


def _progress_reporter(
    deps: ImportDeps,
    job: dict[str, Any],
    *,
    status: str,
    phase: str,
) -> Callable[[int, int, int], None]:
    """节流后的字节进度回调；每 0.25s 或每 1% 最多落盘一次。"""
    state = {"at": 0.0, "pct": -1}

    def report(
        done_bytes: int, total_bytes: int, files_done: int
    ) -> None:
        total = max(0, int(total_bytes or 0))
        done = max(0, int(done_bytes or 0))
        pct = int(done * 100 / total) if total else 0
        pct = max(0, min(100, pct))
        now = time.monotonic()
        if pct == state["pct"] and now - state["at"] < 0.25:
            return
        state["pct"] = pct
        state["at"] = now
        _update(
            deps,
            job,
            status=status,
            progress=pct,
            progress_phase=phase,
            processed_bytes=done,
            total_bytes=total,
            processed_files=max(0, int(files_done or 0)),
        )

    return report


def _public(job: dict[str, Any]) -> dict[str, Any]:
    # staging 是 A3 内部路径，不暴露给 A2/前端。
    hidden = {"staging", "workdir"}
    return {
        key: value
        for key, value in job.items()
        if not key.startswith("_") and key not in hidden
    }


def _fail_unfinished(deps: ImportDeps, job: dict[str, Any], message: str) -> None:
    """后台任务异常退出且 runner 没来得及写终态时兜底。

    staging/workdir 故意保留：`_load_jobs` 会在下次启动时按字段清理，
    提前清空反而会留下没人认领的目录。
    """
    with deps.lock:
        if job.get("status") in _TERMINAL:
            return
        job.update(
            status="failed",
            progress=None,
            progress_phase="failed",
            error=message,
        )
        job["updated_at"] = _now_iso()
        _persist_job(deps, job)


def _spawn_job(
    deps: ImportDeps,
    job: dict[str, Any],
    coro: Awaitable[None],
    *,
    timeout_s: float = _JOB_TIMEOUT_S,
) -> asyncio.Task:
    """启动后台任务，持有强引用并保证超时/异常都有终态。

    `asyncio` 只保留任务的弱引用；不把 task 放回 job 里，任务可能被 GC，
    前端会永远看到 queued/running。
    """

    async def _guard() -> None:
        try:
            await asyncio.wait_for(coro, timeout=timeout_s)
        except TimeoutError:
            # 通知仍在跑的协作式步骤尽快退出；真正落盘清理交给重启时的
            # `_load_jobs`（staging 在 WSL 内，宿主无法直接删）。
            job["_cancel"].set()
            _update(
                deps,
                job,
                status="failed",
                progress=None,
                progress_phase="failed",
                error=f"任务超时（>{int(timeout_s)}s）",
            )
            raise

    task = asyncio.create_task(_guard())
    job["_task"] = task

    def _done(finished: asyncio.Task) -> None:
        job.pop("_task", None)
        if finished.cancelled():
            _fail_unfinished(deps, job, "任务被取消")
            return
        exc = finished.exception()
        if exc is not None:
            _fail_unfinished(
                deps, job, f"{type(exc).__name__}: {exc}"
            )

    task.add_done_callback(_done)
    return task


def _cleanup(deps: ImportDeps, execution: Any, staging: str) -> None:
    if execution is None or not staging:
        return
    parent = os.path.dirname(staging) or "/"
    try:
        execution.host_run(
            f"rm -rf {shlex.quote(staging)}",
            kind="workspace_import_cleanup",
            cwd=parent,
        )
    except Exception:  # noqa: BLE001 - 清理失败不覆盖原始错误
        pass


def _remove_tree(path: str) -> None:
    if not path:
        return
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass


def _remove_file(path: str) -> None:
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


def _archive_root(settings: Settings) -> str:
    raw = str(settings.workspace_archive_root or "").strip()
    if not raw:
        return ""
    return normalize_root(raw)


def _host_ok(execution: Any, command: str, kind: str, cwd: str) -> Any:
    raw = execution.host_run(command, kind=kind, cwd=cwd)
    if not getattr(raw, "ok", False):
        detail = getattr(raw, "stderr", "") or getattr(raw, "stdout", "")
        raise RuntimeError(f"{kind} 失败：{str(detail)[:500]}")
    return raw


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _archive_stats(root: str) -> tuple[int, int]:
    total = 0
    files = 0
    for base, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [item for item in dirs if item != ".agent"]
        for name in names:
            full = os.path.join(base, name)
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            try:
                total += os.stat(full).st_size
            except OSError:
                continue
            files += 1
    return total, files


def _write_archive(
    root: str,
    archive_path: str,
    *,
    on_progress: Callable[[int, int, int], None] | None = None,
) -> None:
    """把工作区打成 tar.gz；排除 `.agent`，保留 `.git` 以便恢复历史。"""
    total_all, files_all = _archive_stats(root)
    processed = 0
    done = 0
    os.makedirs(os.path.dirname(archive_path), exist_ok=True)
    with tarfile.open(archive_path, "w:gz") as archive:
        for base, dirs, names in os.walk(root, followlinks=False):
            dirs[:] = [item for item in dirs if item != ".agent"]
            for name in dirs:
                full = os.path.join(base, name)
                if os.path.islink(full):
                    continue
                archive.add(
                    full,
                    arcname=os.path.relpath(full, root),
                    recursive=False,
                )
            for name in names:
                full = os.path.join(base, name)
                if os.path.islink(full) or not os.path.isfile(full):
                    continue
                archive.add(
                    full,
                    arcname=os.path.relpath(full, root),
                    recursive=False,
                )
                done += 1
                try:
                    processed += os.stat(full).st_size
                except OSError:
                    pass
                if on_progress is not None:
                    on_progress(
                        min(processed, total_all), total_all, done
                    )
    if on_progress is not None:
        on_progress(total_all, total_all, files_all)


def _create_revision(
    execution: Any,
    target: str,
    *,
    workspace_id: str,
    actor: str,
    archive_root: str,
    label: str,
    on_progress: Callable[[int, int, int], None] | None = None,
) -> dict[str, Any]:
    """在工作区里提交一个本地 git revision，并按需归档。"""
    if not os.path.isdir(os.path.join(target, ".git")):
        _host_ok(execution, "git init -q", "workspace_git_init", target)
    _host_ok(execution, "git add -A", "workspace_revision_add", target)
    env = (
        "GIT_CONFIG_COUNT=2 "
        "GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0=/dev/null "
        "GIT_CONFIG_KEY_1=commit.gpgsign GIT_CONFIG_VALUE_1=false "
        "GIT_AUTHOR_NAME=coding-agent "
        "GIT_AUTHOR_EMAIL=agent@localhost "
        "GIT_COMMITTER_NAME=coding-agent "
        "GIT_COMMITTER_EMAIL=agent@localhost"
    )
    message = f"{label}:{workspace_id}"
    _host_ok(
        execution,
        f"{env} git commit -q --allow-empty -m {shlex.quote(message)}",
        "workspace_revision_commit",
        target,
    )
    raw = _host_ok(
        execution, "git rev-parse HEAD", "workspace_revision_probe", target
    )
    commit = str(getattr(raw, "stdout", "") or "").strip()
    revision_id = uuid.uuid4().hex
    archive_path = ""
    digest = ""
    if archive_root:
        directory = os.path.join(archive_root, workspace_id)
        archive_path = os.path.join(directory, f"{revision_id}.tar.gz")
        _write_archive(
            target, archive_path, on_progress=on_progress
        )
        digest = _sha256(archive_path)
    total, files = _tree_size(target)
    return {
        "revision_id": revision_id,
        "workspace_id": workspace_id,
        "actor": str(actor or ""),
        "git_commit": commit,
        "archive_path": archive_path,
        "archive_sha256": digest,
        "bytes": total,
        "files": files,
    }


def _active_job_for(
    deps: ImportDeps, target: str
) -> dict[str, Any] | None:
    """调用方必须已持有 deps.lock。"""
    for existing in deps.jobs.values():
        if (
            existing.get("target") == target
            and existing.get("status")
            not in {"active", "failed", "cancelled"}
        ):
            return existing
    return None


async def _run_import(
    deps: ImportDeps,
    context: AgentContext,
    job: dict[str, Any],
    body: WorkspaceImportBody,
) -> None:
    execution: Any = None
    runtime: Any = None
    staging = ""
    revision: dict[str, Any] | None = None
    try:
        _update(
            deps,
            job,
            status="preparing",
            progress=0,
            progress_phase="preparing",
        )
        runtime = deps.new_runtime(context)
        execution = getattr(runtime, "execution", None)
        if execution is None:
            raise RuntimeError("执行面不可用，无法导入")
        target = context.workspace
        _recover_swap(target)
        staging = f"{target}.import-{job['job_id']}"
        parent = os.path.dirname(target) or "/"
        os.makedirs(parent, exist_ok=True)
        _update(deps, job, staging=staging)

        raw = await asyncio.to_thread(
            execution.host_run,
            f"mkdir -p {shlex.quote(staging)}",
            kind="workspace_import_prepare",
            cwd=parent,
        )
        if not raw.ok:
            raise RuntimeError(raw.stderr or "无法创建 staging 目录")
        if job["_cancel"].is_set():
            raise _Cancelled

        _update(
            deps,
            job,
            status="copying",
            progress=None,
            progress_phase="copying",
        )
        raw = await asyncio.to_thread(
            execution.host_run,
            tar_command(job["source"], staging),
            kind="workspace_import",
            cwd=parent,
        )
        if not raw.ok:
            raise RuntimeError(raw.stderr or "复制失败")
        if job["_cancel"].is_set():
            raise _Cancelled

        _update(
            deps,
            job,
            status="verifying",
            progress=None,
            progress_phase="verifying",
        )
        total, files = await asyncio.to_thread(_tree_size, staging)
        if body.quota_bytes and total > body.quota_bytes:
            raise RuntimeError(
                f"导入大小 {total} 超过工作区配额 {body.quota_bytes}"
            )
        if body.max_bytes and total > body.max_bytes:
            raise RuntimeError(
                f"导入大小 {total} 超过上限 {body.max_bytes}"
            )
        if files > body.max_files:
            raise RuntimeError(
                f"导入文件数 {files} 超过上限 {body.max_files}"
            )
        _update(
            deps,
            job,
            bytes=total,
            files=files,
            progress=100,
            progress_phase="verifying",
            processed_bytes=total,
            total_bytes=total,
            processed_files=files,
        )
        if job["_cancel"].is_set():
            raise _Cancelled

        _update(
            deps,
            job,
            status="revisioning",
            progress=0,
            progress_phase="revisioning",
        )
        await asyncio.to_thread(_attach_git_history, target, staging)
        revision = await asyncio.to_thread(
            _create_revision,
            execution,
            staging,
            workspace_id=context.workspace_id,
            actor=context.user,
            archive_root=_archive_root(deps.settings),
            label="import",
            on_progress=_progress_reporter(
                deps, job, status="revisioning", phase="revisioning"
            ),
        )
        if job["_cancel"].is_set():
            raise _Cancelled

        _update(
            deps,
            job,
            status="activating",
            progress=99,
            progress_phase="activating",
        )
        await asyncio.to_thread(_activate_replace, staging, target)
        _update(
            deps,
            job,
            status="active",
            progress=100,
            progress_phase="active",
            revision=revision,
            staging="",
        )
    except _Cancelled:
        _cleanup(deps, execution, staging)
        _remove_file(
            str((revision or {}).get("archive_path") or "")
        )
        _update(
            deps,
            job,
            status="cancelled",
            progress=None,
            progress_phase="cancelled",
            staging="",
        )
    except Exception as exc:  # noqa: BLE001 - 任务失败要回给调用方
        _cleanup(deps, execution, staging)
        _remove_file(
            str((revision or {}).get("archive_path") or "")
        )
        _update(
            deps,
            job,
            status="failed",
            progress=None,
            progress_phase="failed",
            error=f"{type(exc).__name__}: {exc}",
            staging="",
        )
    finally:
        if runtime is not None:
            try:
                await deps.close_runtime(runtime)
            except Exception:  # noqa: BLE001 - 关闭失败不影响任务结果
                pass


async def _run_upload(
    deps: ImportDeps,
    context: AgentContext,
    job: dict[str, Any],
    *,
    archive_path: str,
    workdir: str,
    filename: str,
    quota_bytes: int,
    max_files: int,
    max_bytes: int,
) -> None:
    execution: Any = None
    runtime: Any = None
    extract_dir = os.path.join(workdir, "root")
    revision: dict[str, Any] | None = None
    try:
        _update(
            deps,
            job,
            status="extracting",
            progress=0,
            progress_phase="extracting",
        )
        limits = ExtractLimits(
            max_files=max_files,
            max_total_bytes=(
                max_bytes or quota_bytes or 0
            ),
            max_ratio=int(deps.settings.workspace_extract_max_ratio or 200),
        )
        stats = await asyncio.to_thread(
            safe_extract,
            archive_path,
            extract_dir,
            name=filename,
            limits=limits,
            on_progress=_progress_reporter(
                deps, job, status="extracting", phase="extracting"
            ),
        )
        if job["_cancel"].is_set():
            raise _Cancelled

        _update(
            deps,
            job,
            status="scanning",
            progress=None,
            progress_phase="scanning",
            bytes=stats["bytes"],
            files=stats["files"],
            processed_bytes=stats["bytes"],
            total_bytes=stats["bytes"],
            processed_files=stats["files"],
        )
        scanner = build_scanner(deps.settings)
        report = await asyncio.to_thread(scanner.scan, extract_dir)
        _update(
            deps,
            job,
            scan_status=report.status,
            scan_detail=report.detail,
        )
        if report.status in {"warn", "failed"}:
            raise RuntimeError(
                f"扫描未通过（{report.status}）：{report.detail[:500]}"
            )
        if job["_cancel"].is_set():
            raise _Cancelled

        _update(
            deps,
            job,
            status="revisioning",
            progress=0,
            progress_phase="revisioning",
        )
        runtime = deps.new_runtime(context)
        execution = getattr(runtime, "execution", None)
        if execution is None:
            raise RuntimeError("执行面不可用，无法创建 revision")
        await asyncio.to_thread(
            _attach_git_history, context.workspace, extract_dir
        )
        revision = await asyncio.to_thread(
            _create_revision,
            execution,
            extract_dir,
            workspace_id=context.workspace_id,
            actor=context.user,
            archive_root=_archive_root(deps.settings),
            label="upload",
            on_progress=_progress_reporter(
                deps, job, status="revisioning", phase="revisioning"
            ),
        )
        if job["_cancel"].is_set():
            raise _Cancelled

        _update(
            deps,
            job,
            status="activating",
            progress=99,
            progress_phase="activating",
        )
        await asyncio.to_thread(
            _activate_replace, extract_dir, context.workspace
        )
        _remove_tree(workdir)
        _update(
            deps,
            job,
            status="active",
            progress=100,
            progress_phase="active",
            revision=revision,
            bytes=stats["bytes"],
            files=stats["files"],
            processed_bytes=stats["bytes"],
            total_bytes=stats["bytes"],
            processed_files=stats["files"],
            staging="",
        )
    except _Cancelled:
        _remove_tree(workdir)
        _remove_file(
            str((revision or {}).get("archive_path") or "")
        )
        _update(
            deps,
            job,
            status="cancelled",
            progress=None,
            progress_phase="cancelled",
            staging="",
        )
    except ScanUnavailable as exc:
        _remove_tree(workdir)
        _remove_file(
            str((revision or {}).get("archive_path") or "")
        )
        _update(
            deps,
            job,
            status="failed",
            progress=None,
            progress_phase="failed",
            error=f"扫描不可用：{exc}",
            staging="",
        )
    except Exception as exc:  # noqa: BLE001 - 失败要回给调用方
        _remove_tree(workdir)
        _remove_file(
            str((revision or {}).get("archive_path") or "")
        )
        _update(
            deps,
            job,
            status="failed",
            progress=None,
            progress_phase="failed",
            error=f"{type(exc).__name__}: {exc}",
            staging="",
        )
    finally:
        if runtime is not None:
            try:
                await deps.close_runtime(runtime)
            except Exception:  # noqa: BLE001
                pass


def register_import_routes(app: FastAPI, deps: ImportDeps) -> None:
    _load_jobs(deps)

    @app.post("/v1/workspaces/import")
    async def start_import(
        body: WorkspaceImportBody, request: Request
    ) -> dict:
        if not deps.settings.workspace_registry:
            raise HTTPException(
                status_code=404, detail="工作区注册表未启用"
            )
        context = deps.context(request)
        source = validate_source(
            deps.settings, body.source_path, target=context.workspace
        )
        with deps.lock:
            for existing in deps.jobs.values():
                if (
                    existing.get("target") == context.workspace
                    and existing.get("status")
                    not in {"active", "failed", "cancelled"}
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="该工作区已有进行中的导入任务",
                    )
            job_id = uuid.uuid4().hex[:16]
            job = {
                "job_id": job_id,
                "workspace_id": context.workspace_id,
                "kind": "import",
                "status": "queued",
                "source": source,
                "target": context.workspace,
                "bytes": 0,
                "files": 0,
                "progress": 0,
                "progress_phase": "queued",
                "processed_bytes": 0,
                "total_bytes": 0,
                "processed_files": 0,
                "error": "",
                "staging": "",
                "created_at": _now_iso(),
                "_cancel": threading.Event(),
            }
            deps.jobs[job_id] = job
            _persist_job(deps, job)
        _spawn_job(deps, job, _run_import(deps, context, job, body))
        return _public(job)

    @app.get("/v1/workspaces/import/{job_id}")
    async def import_status(job_id: str, request: Request) -> dict:
        if not deps.settings.workspace_registry:
            raise HTTPException(
                status_code=404, detail="工作区注册表未启用"
            )
        deps.context(request)
        with deps.lock:
            job = deps.jobs.get(job_id)
            if job is None:
                raise HTTPException(
                    status_code=404, detail="导入任务不存在"
                )
            return _public(job)

    @app.delete("/v1/workspaces/import/{job_id}")
    async def cancel_import(job_id: str, request: Request) -> dict:
        if not deps.settings.workspace_registry:
            raise HTTPException(
                status_code=404, detail="工作区注册表未启用"
            )
        deps.context(request)
        with deps.lock:
            job = deps.jobs.get(job_id)
            if job is None:
                raise HTTPException(
                    status_code=404, detail="导入任务不存在"
                )
            if job["status"] in {"active", "failed", "cancelled"}:
                return _public(job)
            job["_cancel"].set()
            return _public(job)

    @app.post("/v1/workspaces/uploads")
    async def start_upload(
        request: Request,
        archive: Annotated[UploadFile, File()],
        quota_bytes: int = Form(0),
        max_files: int = Form(200_000),
        max_bytes: int = Form(0),
    ) -> dict:
        if not deps.settings.workspace_registry:
            raise HTTPException(
                status_code=404, detail="工作区注册表未启用"
            )
        context = deps.context(request)
        target = context.workspace
        filename = archive.filename or "upload.bin"
        with deps.lock:
            if _active_job_for(deps, target) is not None:
                raise HTTPException(
                    status_code=409,
                    detail="该工作区已有进行中的导入/上传任务",
                )
            # 先恢复上次中断的 swap、清理遗留 staging；此时还没有本任务目录。
            _recover_swap(target)
            job_id = uuid.uuid4().hex[:16]
            workdir = f"{target}.upload-{job_id}"
            job = {
                "job_id": job_id,
                "workspace_id": context.workspace_id,
                "kind": "upload",
                "status": "receiving",
                "source": filename,
                "target": target,
                "workdir": workdir,
                "bytes": 0,
                "files": 0,
                "progress": 0,
                "progress_phase": "receiving",
                "processed_bytes": 0,
                "total_bytes": 0,
                "processed_files": 0,
                "scan_status": "",
                "scan_detail": "",
                "revision": None,
                "error": "",
                "created_at": _now_iso(),
                "_cancel": threading.Event(),
            }
            deps.jobs[job_id] = job
            _persist_job(deps, job)

        archive_path = os.path.join(workdir, "archive.bin")
        total = 0
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            os.makedirs(workdir, exist_ok=True)
            with open(archive_path, "wb") as output:
                while True:
                    chunk = await archive.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if (
                        deps.settings.workspace_upload_max_bytes
                        and total
                        > deps.settings.workspace_upload_max_bytes
                    ):
                        raise HTTPException(
                            status_code=413,
                            detail=(
                                "上传超过上限 "
                                f"{deps.settings.workspace_upload_max_bytes} 字节"
                            ),
                        )
                    output.write(chunk)
        except HTTPException:
            _remove_tree(workdir)
            with deps.lock:
                deps.jobs.pop(job_id, None)
            raise
        except OSError as exc:
            _remove_tree(workdir)
            with deps.lock:
                deps.jobs.pop(job_id, None)
            raise HTTPException(
                status_code=500,
                detail=f"无法保存上传归档：{type(exc).__name__}: {exc}",
            ) from exc

        _spawn_job(
            deps,
            job,
            _run_upload(
                deps,
                context,
                job,
                archive_path=archive_path,
                workdir=workdir,
                filename=filename,
                quota_bytes=quota_bytes,
                max_files=max_files,
                max_bytes=max_bytes,
            ),
        )
        return _public(job)

    @app.get("/v1/workspaces/uploads/{job_id}")
    async def upload_status(job_id: str, request: Request) -> dict:
        if not deps.settings.workspace_registry:
            raise HTTPException(
                status_code=404, detail="工作区注册表未启用"
            )
        deps.context(request)
        with deps.lock:
            job = deps.jobs.get(job_id)
            if job is None:
                raise HTTPException(
                    status_code=404, detail="上传任务不存在"
                )
            return _public(job)

    @app.delete("/v1/workspaces/uploads/{job_id}")
    async def cancel_upload(job_id: str, request: Request) -> dict:
        if not deps.settings.workspace_registry:
            raise HTTPException(
                status_code=404, detail="工作区注册表未启用"
            )
        deps.context(request)
        with deps.lock:
            job = deps.jobs.get(job_id)
            if job is None:
                raise HTTPException(
                    status_code=404, detail="上传任务不存在"
                )
            if job["status"] in {"active", "failed", "cancelled"}:
                return _public(job)
            job["_cancel"].set()
            return _public(job)

    @app.post("/v1/workspaces/rollback")
    async def rollback(
        body: WorkspaceRollbackBody, request: Request
    ) -> dict:
        if not deps.settings.workspace_registry:
            raise HTTPException(
                status_code=404, detail="工作区注册表未启用"
            )
        context = deps.context(request)
        if not re.fullmatch(r"[0-9a-fA-F]{7,64}", body.git_commit):
            raise HTTPException(
                status_code=400, detail="git_commit 非法"
            )
        runtime = deps.new_runtime(context)
        try:
            execution = getattr(runtime, "execution", None)
            if execution is None:
                raise HTTPException(
                    status_code=503, detail="执行面不可用"
                )
            pre = await asyncio.to_thread(
                _create_revision,
                execution,
                context.workspace,
                workspace_id=context.workspace_id,
                actor=context.user,
                archive_root=_archive_root(deps.settings),
                label="pre-rollback",
            )
            raw = await asyncio.to_thread(
                execution.host_run,
                f"git reset --hard {body.git_commit}",
                kind="workspace_rollback",
                cwd=context.workspace,
            )
            if not raw.ok:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "回滚失败："
                        f"{(raw.stderr or raw.stdout or '')[:500]}"
                    ),
                )
            total, files = await asyncio.to_thread(
                _tree_size, context.workspace
            )
            return {
                "status": "active",
                "pre_rollback": pre,
                "bytes": total,
                "files": files,
            }
        finally:
            await deps.close_runtime(runtime)

    @app.post("/v1/workspaces/restore")
    async def restore(
        body: WorkspaceRestoreBody, request: Request
    ) -> dict:
        if not deps.settings.workspace_registry:
            raise HTTPException(
                status_code=404, detail="工作区注册表未启用"
            )
        context = deps.context(request)
        root = _archive_root(deps.settings)
        if not root:
            raise HTTPException(
                status_code=400, detail="未配置归档根"
            )
        try:
            path = ensure_inside(
                normalize_root(body.archive_path), root
            )
        except PathGuardError as exc:
            raise HTTPException(
                status_code=400, detail=f"归档路径非法：{exc}"
            ) from exc
        if not os.path.isfile(path):
            raise HTTPException(
                status_code=400, detail="归档文件不存在"
            )
        digest = _sha256(path)
        if body.archive_sha256 and digest != body.archive_sha256:
            raise HTTPException(
                status_code=400, detail="归档 sha256 不匹配"
            )
        if body.git_commit and not re.fullmatch(
            r"[0-9a-fA-F]{7,64}", body.git_commit
        ):
            raise HTTPException(
                status_code=400, detail="git_commit 非法"
            )

        target = context.workspace
        _recover_swap(target)
        staging = f"{target}.restore-{uuid.uuid4().hex[:10]}"
        runtime: Any = None
        pre: dict[str, Any] | None = None
        try:
            runtime = deps.new_runtime(context)
            execution = getattr(runtime, "execution", None)
            if execution is None:
                raise HTTPException(
                    status_code=503, detail="执行面不可用"
                )
            if os.path.isdir(target):
                pre = await asyncio.to_thread(
                    _create_revision,
                    execution,
                    target,
                    workspace_id=context.workspace_id,
                    actor=context.user,
                    archive_root=root,
                    label="pre-restore",
                )
            await asyncio.to_thread(
                safe_extract,
                path,
                staging,
                name=path,
                limits=ExtractLimits(
                    max_files=500_000,
                    max_total_bytes=0,
                    max_single_bytes=0,
                    max_ratio=0,
                ),
                skip_names=frozenset(),
            )
            if body.git_commit:
                raw = await asyncio.to_thread(
                    execution.host_run,
                    (
                        "git rev-parse --verify "
                        f"{shlex.quote(body.git_commit)}^{{commit}}"
                    ),
                    kind="workspace_revision_probe",
                    cwd=staging,
                )
                if not raw.ok:
                    raise HTTPException(
                        status_code=409,
                        detail="归档中找不到该 git commit",
                    )
            total, files = await asyncio.to_thread(
                _tree_size, staging
            )
            await asyncio.to_thread(
                _activate_replace, staging, target
            )
            return {
                "status": "active",
                "pre_restore": pre,
                "bytes": total,
                "files": files,
                "archive_sha256": digest,
            }
        except HTTPException:
            _remove_file(
                str((pre or {}).get("archive_path") or "")
            )
            raise
        except Exception as exc:  # noqa: BLE001
            _remove_file(
                str((pre or {}).get("archive_path") or "")
            )
            raise HTTPException(
                status_code=500,
                detail=f"归档恢复失败：{type(exc).__name__}: {exc}",
            ) from exc
        finally:
            if os.path.isdir(staging):
                shutil.rmtree(staging, ignore_errors=True)
            if runtime is not None:
                try:
                    await deps.close_runtime(runtime)
                except Exception:  # noqa: BLE001
                    pass

    @app.delete("/v1/workspaces/revisions/archive")
    async def delete_archive(
        body: WorkspaceArchiveDeleteBody, request: Request
    ) -> dict:
        if not deps.settings.workspace_registry:
            raise HTTPException(
                status_code=404, detail="工作区注册表未启用"
            )
        deps.context(request)
        root = _archive_root(deps.settings)
        if not root:
            raise HTTPException(
                status_code=400, detail="未配置归档根"
            )
        try:
            path = ensure_inside(
                normalize_root(body.archive_path), root
            )
        except PathGuardError as exc:
            raise HTTPException(
                status_code=400, detail=f"归档路径非法：{exc}"
            ) from exc
        if os.path.isfile(path):
            os.remove(path)
        return {"ok": True, "path": path}
