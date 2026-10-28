"""把 A2L 快照 op 适配成 SnapshotStore 的工具层接口。

工具层（`tools/files.py`）只依赖 save/write_with_snapshot/... 这些方法名；
真正的快照内容与校验清单都在 A2L 工作区外。
"""

from __future__ import annotations

import base64
from typing import Any

from coding_agent.execution.host_file_access import (
    CHUNK_BYTES,
    HostCallerProtocol,
    _relative,
)
from coding_agent.sandbox.snapshots import RestoreResult, SnapshotEntry


class HostSnapshotStore:
    root = ""

    def __init__(
        self,
        caller: HostCallerProtocol,
        *,
        workspace_id: str,
        root_handle: str,
        grant_id: str = "",
    ) -> None:
        self.caller = caller
        self.workspace = {
            "workspace_id": str(workspace_id),
            "kind": "host",
            "root_handle": str(root_handle),
        }
        self.grant_id = str(grant_id)
        self._absent: dict[str, bool] = {}

    def _call(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        return dict(
            self.caller.call(
                kind,
                workspace=self.workspace,
                payload=payload,
                grant_id=self.grant_id,
            )
            or {}
        )

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def _write(self, path: str, content: str) -> dict[str, Any]:
        relative = _relative(path)
        data = content.encode("utf-8")
        upload = self._call("host.write_begin", {"path": relative})
        upload_id = str(upload.get("upload_id") or "")
        if not upload_id:
            raise RuntimeError("write_begin 未返回 upload_id")
        try:
            for offset in range(0, len(data), CHUNK_BYTES):
                self._call(
                    "host.write_chunk",
                    {
                        "upload_id": upload_id,
                        "offset": offset,
                        "data_b64": base64.b64encode(
                            data[offset : offset + CHUNK_BYTES]
                        ).decode("ascii"),
                    },
                )
            return self._call(
                "host.write_commit", {"upload_id": upload_id}
            )
        except Exception:
            try:
                self._call(
                    "host.write_abort", {"upload_id": upload_id}
                )
            except Exception:  # noqa: BLE001
                pass
            raise

    def save(self, path: str, original: str, *, existed: bool = True) -> str:
        data = self._call(
            "host.snapshot_save", {"path": _relative(path)}
        )
        return str(data.get("snapshot_id") or "")

    def write_with_snapshot(
        self,
        path: str,
        content: str,
        original: str,
        *,
        existed: bool,
    ) -> str:
        result = self._write(path, content)
        return str(result.get("snapshot_id") or "")

    def move_with_snapshot(
        self,
        source: str,
        destination: str,
        content: str,
        dest_original: str,
        *,
        dest_existed: bool,
    ) -> tuple[str, str]:
        source_id = self.save(source, content, existed=True)
        dest_id = self.write_with_snapshot(
            destination,
            content,
            dest_original,
            existed=dest_existed,
        )
        return source_id, dest_id

    def write_many_with_snapshots(
        self,
        entries: list[tuple[str, str, str, bool]],
    ) -> dict[str, str]:
        if not entries:
            return {}
        uploads: list[str] = []
        paths: list[str] = []
        try:
            for path, content, _original, _existed in entries:
                relative = _relative(path)
                data = content.encode("utf-8")
                upload = self._call(
                    "host.write_begin", {"path": relative}
                )
                upload_id = str(upload.get("upload_id") or "")
                if not upload_id:
                    raise RuntimeError("write_begin 未返回 upload_id")
                uploads.append(upload_id)
                paths.append(relative)
                for offset in range(0, len(data), CHUNK_BYTES):
                    self._call(
                        "host.write_chunk",
                        {
                            "upload_id": upload_id,
                            "offset": offset,
                            "data_b64": base64.b64encode(
                                data[offset : offset + CHUNK_BYTES]
                            ).decode("ascii"),
                        },
                    )
            result = self._call(
                "host.write_many_commit", {"uploads": uploads}
            )
        except Exception:
            for upload_id in uploads:
                try:
                    self._call(
                        "host.write_abort", {"upload_id": upload_id}
                    )
                except Exception:  # noqa: BLE001
                    pass
            raise
        written = result.get("written")
        if not isinstance(written, list):
            raise RuntimeError("write_many_commit 返回格式错误")
        mapping: dict[str, str] = {}
        for index, item in enumerate(written):
            if index < len(paths):
                mapping[paths[index]] = str(
                    item.get("snapshot_id") or ""
                )
        return mapping

    def save_many(
        self, items: list[tuple[str, str, bool]]
    ) -> list[str]:
        return [
            self.save(path, original, existed=existed)
            for path, original, existed in items
        ]

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def _entries(self, *, path: str | None = None) -> list[SnapshotEntry]:
        payload: dict[str, Any] = {"limit": 1000}
        if path is not None:
            payload["path"] = _relative(path)
        data = self._call("host.snapshot_list", payload)
        raw = data.get("entries")
        entries: list[SnapshotEntry] = []
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                snapshot_id = str(item.get("snapshot_id") or "")
                relpath = str(item.get("path") or "")
                if not snapshot_id or not relpath:
                    continue
                self._absent[snapshot_id] = bool(item.get("absent"))
                entries.append(
                    SnapshotEntry(snapshot_id=snapshot_id, path=relpath)
                )
        return entries

    def list(self, *, limit: int = 0) -> list[SnapshotEntry]:
        entries = self._entries()
        return entries[:limit] if limit and limit > 0 else entries

    def latest_for(self, path: str) -> SnapshotEntry | None:
        entries = self._entries(path=path)
        return entries[0] if entries else None

    def find(
        self, snapshot_id: str, path: str | None = None
    ) -> SnapshotEntry | None:
        wanted = _relative(path) if path else None
        for entry in self._entries(path=wanted):
            if entry.snapshot_id != snapshot_id:
                continue
            if wanted is not None and entry.path != wanted:
                continue
            return entry
        return None

    def was_absent(self, entry: SnapshotEntry) -> bool:
        return bool(self._absent.get(entry.snapshot_id, False))

    # ------------------------------------------------------------------
    # 恢复
    # ------------------------------------------------------------------
    def restore(
        self, entry: SnapshotEntry, path: str | None = None
    ) -> RestoreResult:
        data = self._call(
            "host.restore",
            {
                "snapshot_id": entry.snapshot_id,
                "path": path or entry.path,
            },
        )
        return RestoreResult(
            ok=True,
            path=str(data.get("path") or entry.path),
            snapshot_id=str(
                data.get("snapshot_id") or entry.snapshot_id
            ),
            undo_snapshot_id=(
                str(data["undo_snapshot_id"])
                if data.get("undo_snapshot_id")
                else None
            ),
            added=int(data.get("added") or 0),
            removed=int(data.get("removed") or 0),
            diff=str(data.get("diff") or ""),
            message=str(data.get("message") or ""),
        )
