"""宿主侧文件快照：落在工作区外，写入前留底、可校验恢复。

与 `sandbox/snapshots.py` 的区别：
- 快照内容不在工作区内，agent 不能通过文件工具删除自己的留底；
- 每个快照只对应一个文件，manifest 记录 sha256 与“当时是否存在”；
- 路径必须经过 `HostPathPolicy` 的 canonicalization 才能落盘或恢复。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

from ubismart.host_broker.paths import HostPathPolicy
from ubismart.protocol.diffing import count_changes, unified_diff
from ubismart.sandbox.snapshots import (
    ABSENT_SUFFIX,
    RestoreResult,
    SnapshotEntry,
)

_SNAPSHOT_ID_RE = re.compile(r"^[0-9A-Za-z-]{8,80}$")
MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024
_SEQ = 0


def _new_snapshot_id() -> str:
    global _SEQ
    _SEQ += 1
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    return f"{stamp}{_SEQ % 1000:03d}-{uuid.uuid4().hex[:6]}"


def _workspace_key(root: str) -> str:
    normalized = os.path.normcase(os.path.normpath(str(root)))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def default_snapshot_root() -> Path:
    override = os.environ.get("AGENT_HOST_SNAPSHOT_DIR", "").strip()
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("TMPDIR") or "/tmp"
    return Path(base) / "CodingAgent" / "host-snapshots"


class HostSnapshotError(RuntimeError):
    pass


class HostSnapshotStore:
    """按工作区隔离的宿主快照；内容与清单都在工作区之外。"""

    def __init__(
        self,
        base_dir: str | Path | None = None,
        *,
        path_policy: HostPathPolicy | None = None,
    ) -> None:
        self.base_dir = Path(base_dir or default_snapshot_root())
        self.path_policy = path_policy or HostPathPolicy()
        self._lock = threading.Lock()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 路径
    # ------------------------------------------------------------------
    def _snapshot_dir(self, root: str, snapshot_id: str) -> Path:
        if not _SNAPSHOT_ID_RE.fullmatch(str(snapshot_id or "")):
            raise HostSnapshotError(f"非法 snapshot_id：{snapshot_id!r}")
        return self.base_dir / _workspace_key(root) / snapshot_id

    def _relpath(self, root: str, path: str) -> str:
        return self.path_policy.relative_to_root(root, path)

    def _manifest(self, root: str, snapshot_id: str) -> dict | None:
        directory = self._snapshot_dir(root, snapshot_id)
        try:
            data = json.loads(
                (directory / "manifest.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def save_bytes(
        self,
        root: str,
        path: str,
        data: bytes,
        *,
        existed: bool,
    ) -> str:
        relpath = self._relpath(root, path)
        snapshot_id = _new_snapshot_id()
        directory = self._snapshot_dir(root, snapshot_id)
        directory.mkdir(parents=True, exist_ok=False)
        payload = data if existed else b""
        (directory / "content.bin").write_bytes(payload)
        manifest = {
            "snapshot_id": snapshot_id,
            "root": os.path.normpath(str(root)),
            "path": relpath,
            "exists": bool(existed),
            "absent": not bool(existed),
            "sha256": _sha256(payload),
            "size": len(payload),
            "created_at": datetime.now(UTC).isoformat(),
        }
        (directory / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False),
            encoding="utf-8",
        )
        return snapshot_id

    def save(self, root: str, path: str, *, existed: bool | None = None) -> str:
        """为 path 当前内容留底；`existed` 可显式声明“原来不存在”。"""
        if existed is None:
            existed = os.path.isfile(path)
        if existed:
            try:
                size = os.path.getsize(path)
            except OSError as exc:
                raise HostSnapshotError(f"无法读取待留底文件：{exc}") from exc
            if size > MAX_SNAPSHOT_BYTES:
                raise HostSnapshotError(
                    f"文件过大，拒绝留底：{size} 字节"
                )
            try:
                data = Path(path).read_bytes()
            except OSError as exc:
                raise HostSnapshotError(f"无法读取待留底文件：{exc}") from exc
        else:
            data = b""
        with self._lock:
            return self.save_bytes(root, path, data, existed=bool(existed))

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def list(self, root: str, *, limit: int = 0) -> list[SnapshotEntry]:
        workspace = self.base_dir / _workspace_key(root)
        try:
            directories = [
                item
                for item in workspace.iterdir()
                if item.is_dir() and _SNAPSHOT_ID_RE.fullmatch(item.name)
            ]
        except OSError:
            return []
        entries: list[SnapshotEntry] = []
        for directory in directories:
            manifest = self._manifest(root, directory.name)
            if manifest and manifest.get("path"):
                entries.append(
                    SnapshotEntry(
                        snapshot_id=directory.name,
                        path=str(manifest["path"]),
                    )
                )
        entries.sort(key=lambda item: item.snapshot_id, reverse=True)
        return entries[:limit] if limit and limit > 0 else entries

    def find(
        self, root: str, snapshot_id: str, path: str | None = None
    ) -> SnapshotEntry | None:
        manifest = self._manifest(root, snapshot_id)
        if not manifest or not manifest.get("path"):
            return None
        relpath = str(manifest["path"])
        if path is not None and path.lstrip("/") != relpath:
            return None
        return SnapshotEntry(snapshot_id=str(snapshot_id), path=relpath)

    def latest_for(self, root: str, path: str) -> SnapshotEntry | None:
        relpath = self._relpath(root, path)
        for entry in self.list(root):
            if entry.path == relpath:
                return entry
        return None

    def was_absent(self, root: str, entry: SnapshotEntry) -> bool:
        manifest = self._manifest(root, entry.snapshot_id)
        return bool(manifest and manifest.get("absent"))

    def read(self, root: str, entry: SnapshotEntry) -> bytes:
        manifest = self._manifest(root, entry.snapshot_id)
        if not manifest or not manifest.get("path"):
            raise HostSnapshotError(f"快照不存在：{entry.snapshot_id}")
        if str(manifest["path"]) != entry.path:
            raise HostSnapshotError("快照路径与清单不一致")
        content = self._snapshot_dir(
            root, entry.snapshot_id
        ) / "content.bin"
        try:
            data = content.read_bytes()
        except OSError as exc:
            raise HostSnapshotError(f"无法读取快照内容：{exc}") from exc
        expected = str(manifest.get("sha256") or "")
        if expected and _sha256(data) != expected:
            raise HostSnapshotError("快照内容校验失败，可能已被修改")
        return data

    # ------------------------------------------------------------------
    # 恢复
    # ------------------------------------------------------------------
    def restore(
        self, root: str, snapshot_id: str, path: str | None = None
    ) -> RestoreResult:
        entry = self.find(root, snapshot_id, path)
        if entry is None:
            return RestoreResult(
                ok=False,
                snapshot_id=str(snapshot_id),
                message=f"找不到快照 {snapshot_id}",
            )
        try:
            restored = self.read(root, entry)
        except HostSnapshotError as exc:
            return RestoreResult(
                ok=False, path=entry.path, message=str(exc)
            )
        try:
            target = self.path_policy.resolve_child(root, entry.path)
        except Exception as exc:  # noqa: BLE001 - 边界处转成消息
            return RestoreResult(
                ok=False, path=entry.path, message=str(exc)
            )

        absent = self.was_absent(root, entry)
        current_exists = os.path.isfile(target)
        current = b""
        if current_exists:
            try:
                current = Path(target).read_bytes()
            except OSError as exc:
                return RestoreResult(
                    ok=False,
                    path=entry.path,
                    message=f"无法读取当前文件：{exc}",
                )

        if absent:
            if current_exists:
                undo = self.save_bytes(
                    root, target, current, existed=True
                )
                try:
                    os.remove(target)
                except OSError as exc:
                    return RestoreResult(
                        ok=False,
                        path=entry.path,
                        message=f"删除失败：{exc}",
                    )
                return RestoreResult(
                    ok=True,
                    path=entry.path,
                    snapshot_id=entry.snapshot_id,
                    undo_snapshot_id=undo,
                    removed=len(current.splitlines()),
                    message=f"已删除 {entry.path}（快照时不存在）",
                )
            return RestoreResult(
                ok=True,
                path=entry.path,
                snapshot_id=entry.snapshot_id,
                message=f"{entry.path} 当前不存在，无需恢复",
            )

        if current_exists and current == restored:
            return RestoreResult(
                ok=True,
                path=entry.path,
                snapshot_id=entry.snapshot_id,
                message=f"{entry.path} 已是快照内容，无需恢复",
            )

        undo = self.save_bytes(
            root, target, current, existed=current_exists
        )
        try:
            atomic_write(target, restored)
        except OSError as exc:
            return RestoreResult(
                ok=False,
                path=entry.path,
                snapshot_id=entry.snapshot_id,
                undo_snapshot_id=undo,
                message=f"恢复失败：{exc}",
            )

        try:
            old_text = current.decode("utf-8")
            new_text = restored.decode("utf-8")
        except UnicodeDecodeError:
            diff = ""
            added = removed = 0
        else:
            diff = unified_diff(old_text, new_text, entry.path)
            added, removed = count_changes(diff)
        return RestoreResult(
            ok=True,
            path=entry.path,
            snapshot_id=entry.snapshot_id,
            undo_snapshot_id=undo,
            added=added,
            removed=removed,
            diff=diff,
            message=f"已恢复 {entry.path}",
        )


def atomic_write(path: str, data: bytes) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    temp = os.path.join(
        directory, f".codingagent-{uuid.uuid4().hex}.tmp"
    )
    try:
        with open(temp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        try:
            os.remove(temp)
        except OSError:
            pass


__all__ = [
    "ABSENT_SUFFIX",
    "HostSnapshotError",
    "HostSnapshotStore",
    "atomic_write",
    "default_snapshot_root",
]
