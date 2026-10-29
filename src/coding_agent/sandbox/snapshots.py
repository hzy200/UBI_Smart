"""文件快照：改写前的留底，回滚的依据。

布局（工作区内）：

    <工作区>/.agent/backups/<snapshot_id>/<工作区相对路径>

`snapshot_id` 形如 `20261003T024018123456-a1b2c3`（时间戳精确到微秒），
前缀让目录名天然按时间排序，因此**不需要额外的索引文件** ——
列目录就能按新旧找到某个文件的历史版本。

恢复语义是「先留底再覆盖」：回滚前把当前内容也存一份，
所以回滚本身也是可回滚的，不会把用户当下的改动直接抹掉。
"""

from __future__ import annotations

import hashlib
import itertools
import json
import logging
import os
import posixpath
import re
import shlex
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from coding_agent.diffing import count_changes, unified_diff
from coding_agent.sandbox.fs import SandboxFs, SandboxFsError
from coding_agent.sandbox.pathguard import SandboxPathError
from coding_agent.sandbox.wsl_exec import WslSandbox

AGENT_STATE_DIRNAME = ".agent"
BACKUP_DIRNAME = f"{AGENT_STATE_DIRNAME}/backups"

ACTION_RESTORE = "restore"

# 「留底时这个文件还不存在」的标记后缀。与内容文件同目录存放（`<path>.absent`），
# 列目录时按后缀过滤掉，所以不会混进快照列表里。
# 没有它就无法区分「原来是个空文件」和「原来根本没有这个文件」—— 而两者的
# 回滚结果不同：前者还原成空文件，后者删掉。
ABSENT_SUFFIX = ".absent"

# 不进模型上下文的操作（快照留底、改前读出再写回）所用的体积上限。
# 与 `settings.max_file_read_bytes` 不是一个口径：后者约束的是**喂给模型**的
# 内容量，用在这里会把 2MB 以上的文件变成「改不了」，逼模型退回 shell。
MAX_OPERATIONAL_BYTES = 10_000_000

# 同一时钟刻度内生成多个 id 时，用它保证严格递增（进程内有效）
_SNAPSHOT_SEQ = itertools.count()

# 只接受本模块生成的 id；磁盘上的目录名可能被外部改动，不能直接拼进 rm。
_SNAPSHOT_ID_RE = re.compile(r"^\d{8}T\d{12}\d{3}-[0-9a-f]{6}$")


def _snapshot_time(snapshot_id: str) -> datetime | None:
    try:
        return datetime.strptime(
            snapshot_id[:21], "%Y%m%dT%H%M%S%f"
        ).replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def _new_snapshot_id() -> str:
    """按时间排序的快照 id：`YYYYmmddTHHMMSSffffff<seq3>-<uuid6>`。

    必须**严格可排序**：`SnapshotStore.list` 靠 id 的字典序判断新旧。只用秒级
    时间戳时，同一秒内的多次留底会退化成按随机 uuid 排序，`latest_for` /
    "最老一次" 会选错版本。因此时间戳精确到微秒，并再追加一个进程内递增序号
    —— 光靠时间戳不够：系统时钟粒度可能让同一刻度内生成多个相同时间戳。
    """
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    seq = next(_SNAPSHOT_SEQ) % 1000
    return f"{stamp}{seq:03d}-{uuid4().hex[:6]}"


@dataclass(frozen=True, slots=True)
class SnapshotEntry:
    snapshot_id: str
    path: str  # 工作区相对路径


@dataclass(slots=True)
class RestoreResult:
    ok: bool
    path: str = ""
    snapshot_id: str = ""
    # 回滚前为当前内容留下的新快照，可用于「撤销这次撤销」
    undo_snapshot_id: str | None = None
    added: int = 0
    removed: int = 0
    diff: str = ""
    message: str = ""


class SnapshotTamperedError(SandboxFsError):
    """快照内容与宿主侧校验和不一致，可能已被修改。"""


def _relpath(path: str, root: str) -> str:
    prefix = root.rstrip("/") + "/"
    return path[len(prefix) :] if path.startswith(prefix) else path


class SnapshotStore:
    """工作区内的文件快照，可选宿主侧校验和清单。"""

    def __init__(
        self,
        sandbox: WslSandbox,
        fs: SandboxFs,
        root: str,
        *,
        manifest_root: str | Path | None = None,
    ) -> None:
        self._sandbox = sandbox
        self._fs = fs
        self.root = root
        self._manifest_root = Path(manifest_root) if manifest_root else None
        self._workspace_key = hashlib.sha256(
            str(root).encode("utf-8")
        ).hexdigest()[:16]

    @property
    def backup_root(self) -> str:
        return posixpath.join(self.root, BACKUP_DIRNAME)

    def _manifest_path(self, snapshot_id: str) -> Path | None:
        if self._manifest_root is None:
            return None
        return self._manifest_root / self._workspace_key / f"{snapshot_id}.json"

    def _write_manifest(
        self,
        snapshot_id: str,
        files: dict[str, tuple[str, bool]],
    ) -> None:
        """把每个快照文件的 sha256 写到工作区外的清单。

        清单写入失败时保持 best-effort：快照本身已经落盘，不能因为
        宿主目录只读就让文件写入失败。没有清单时恢复会退化为旧行为。
        """
        path = self._manifest_path(snapshot_id)
        if path is None:
            return
        payload = {
            "snapshot_id": snapshot_id,
            "root": self.root,
            "files": {
                relpath: {
                    "sha256": hashlib.sha256(
                        content.encode("utf-8")
                    ).hexdigest(),
                    "absent": absent,
                }
                for relpath, (content, absent) in files.items()
            },
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(".tmp")
            temp.write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(temp, path)
        except Exception as exc:  # noqa: BLE001 - 清单写不出来不该让写入失败
            # 回滚仍可用（backups 已落盘），但这个回滚点**无法被索引与校验** ——
            # 必须留痕：否则会出现"看起来能回滚、实际找不到/校验不了"的假象。
            from coding_agent.platform.observability import note_silent_failure

            note_silent_failure("snapshot_manifest_write")
            logging.getLogger(__name__).warning(
                "回滚点 %s 的清单写入失败，该点仍可回滚但无法索引与校验：%s",
                snapshot_id,
                exc,
            )

    def _load_manifest(self, snapshot_id: str) -> dict[str, Any] | None:
        path = self._manifest_path(snapshot_id)
        if path is None:
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        if not isinstance(payload.get("files"), dict):
            return None
        return payload

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def save(self, path: str, original: str, *, existed: bool = True) -> str:
        """把 path 当前的内容留底，返回 snapshot_id。

        `existed=False` 表示**这次写入是新建**：光存一份空内容不够 —— 回滚时无法
        区分「原来就是空文件」和「原来不存在」，于是只能还原成空文件，在工作区里
        留下一堆删不掉的垃圾。所以额外落一个标记，让回滚可以真的删掉它。
        """
        snapshot_id = _new_snapshot_id()
        relpath = _relpath(path, self.root)
        self._write(snapshot_id, relpath, original)
        if not existed:
            self._write(snapshot_id, f"{relpath}{ABSENT_SUFFIX}", "")
        self._write_manifest(
            snapshot_id, {relpath: (original, not existed)}
        )
        return snapshot_id

    def write_with_snapshot(
        self,
        path: str,
        content: str,
        original: str,
        *,
        existed: bool,
    ) -> str:
        """一次沙箱调用里同时写留底和目标文件。

        文件工具原先要 4 次 `wsl.exe`（stat、读原文件、写留底、写目标）；
        把留底与目标合并到 `write_many` 后只剩读、写两次。留底命中「原来不存在」
        时与 `save` 一样写 `.absent` 标记，回滚才能真的删除新建文件。
        """
        snapshot_id = _new_snapshot_id()
        relpath = _relpath(path, self.root)
        files: dict[str, str] = {
            posixpath.join(self.backup_root, snapshot_id, relpath): (
                original if existed else ""
            ),
            path: content,
        }
        if not existed:
            files[
                posixpath.join(
                    self.backup_root,
                    snapshot_id,
                    f"{relpath}{ABSENT_SUFFIX}",
                )
            ] = ""
        self._fs.write_many(files)
        self._write_manifest(
            snapshot_id,
            {relpath: (original if existed else "", not existed)},
        )
        return snapshot_id

    def move_with_snapshot(
        self,
        source: str,
        destination: str,
        content: str,
        dest_original: str,
        *,
        dest_existed: bool,
    ) -> tuple[str, str]:
        """移动文件：一次 write_many 完成源留底、目标留底与目标写入。

        返回 `(source_snapshot_id, destination_snapshot_id)`。源文件的实际删除
        由调用方随后执行。
        """
        source_id = _new_snapshot_id()
        dest_id = _new_snapshot_id()
        source_rel = _relpath(source, self.root)
        dest_rel = _relpath(destination, self.root)
        files: dict[str, str] = {
            posixpath.join(
                self.backup_root, source_id, source_rel
            ): content,
            posixpath.join(
                self.backup_root, dest_id, dest_rel
            ): dest_original if dest_existed else "",
            destination: content,
        }
        if not dest_existed:
            files[
                posixpath.join(
                    self.backup_root, dest_id, f"{dest_rel}{ABSENT_SUFFIX}"
                )
            ] = ""
        self._fs.write_many(files)
        self._write_manifest(source_id, {source_rel: (content, False)})
        self._write_manifest(
            dest_id,
            {dest_rel: (dest_original if dest_existed else "", not dest_existed)},
        )
        return source_id, dest_id

    def write_many_with_snapshots(
        self,
        entries: list[tuple[str, str, str, bool]],
    ) -> dict[str, str]:
        """批量写多个文件并为每个文件留底；一次 write_many 落盘。

        `entries` 是 `(path, content, original, existed)`。返回 path → snapshot_id。
        所有路径由 `write_many` 先全量校验，任一越界则一个字节都不写。
        """
        if not entries:
            return {}
        files: dict[str, str] = {}
        ids: dict[str, str] = {}
        manifests: dict[str, dict[str, tuple[str, bool]]] = {}
        for path, content, original, existed in entries:
            snapshot_id = _new_snapshot_id()
            relpath = _relpath(path, self.root)
            files[
                posixpath.join(self.backup_root, snapshot_id, relpath)
            ] = original if existed else ""
            if not existed:
                files[
                    posixpath.join(
                        self.backup_root,
                        snapshot_id,
                        f"{relpath}{ABSENT_SUFFIX}",
                    )
                ] = ""
            files[path] = content
            ids[path] = snapshot_id
            manifests.setdefault(snapshot_id, {})[relpath] = (
                original if existed else "",
                not existed,
            )
        self._fs.write_many(files)
        for snapshot_id, entries_map in manifests.items():
            self._write_manifest(snapshot_id, entries_map)
        return ids

    def save_many(
        self, items: list[tuple[str, str, bool]]
    ) -> list[str]:
        """批量留底：所有快照文件一次 `write_many` 落盘。

        `items` 是 `(path, original, existed)`。返回与输入顺序对齐的
        snapshot_id 列表；某个文件写失败时整体抛错，不返回半套 ID。
        """
        if not items:
            return []
        files: dict[str, str] = {}
        snapshot_ids: list[str] = []
        manifests: dict[str, dict[str, tuple[str, bool]]] = {}
        for path, original, existed in items:
            snapshot_id = _new_snapshot_id()
            relpath = _relpath(path, self.root)
            files[
                posixpath.join(self.backup_root, snapshot_id, relpath)
            ] = original
            if not existed:
                files[
                    posixpath.join(
                        self.backup_root,
                        snapshot_id,
                        f"{relpath}{ABSENT_SUFFIX}",
                    )
                ] = ""
            snapshot_ids.append(snapshot_id)
            manifests[snapshot_id] = {relpath: (original, not existed)}
        self._fs.write_many(files)
        for snapshot_id, entries_map in manifests.items():
            self._write_manifest(snapshot_id, entries_map)
        return snapshot_ids

    def was_absent(self, entry: SnapshotEntry) -> bool:
        """这份留底是否代表「当时文件不存在」。

        有校验清单时以清单为准：删掉 `.absent` 标记不能把「新建」伪装成
        「原来就是空文件」。
        """
        manifest = self._load_manifest(entry.snapshot_id)
        if manifest:
            record = manifest["files"].get(entry.path)
            if isinstance(record, dict):
                return bool(record.get("absent"))
        marker = posixpath.join(
            self.backup_root, entry.snapshot_id, f"{entry.path}{ABSENT_SUFFIX}"
        )
        try:
            return self._fs.stat(marker).exists
        except (SandboxFsError, SandboxPathError):
            return False

    def _write(self, snapshot_id: str, relpath: str, content: str) -> None:
        target = posixpath.join(self.backup_root, snapshot_id, relpath.lstrip("/"))
        self._fs.write_text(target, content)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def list(self, *, limit: int = 0) -> list[SnapshotEntry]:
        """按时间倒序列出快照（每个快照里的每个文件算一条）。"""
        script = (
            f"if [ -d {shlex.quote(self.backup_root)} ]; then "
            f"find {shlex.quote(self.backup_root)} -type f -printf '%P\\n'; fi"
        )
        result = self._sandbox.run(script)
        if not result.ok:
            return []

        entries: list[SnapshotEntry] = []
        for line in result.stdout.splitlines():
            name = line.strip()
            snapshot_id, sep, relpath = name.partition("/")
            # 标记文件不是一份留底，只是「当时不存在」的附注
            if not sep or not snapshot_id or not relpath:
                continue
            if relpath.endswith(ABSENT_SUFFIX):
                continue
            entries.append(SnapshotEntry(snapshot_id=snapshot_id, path=relpath))

        entries.sort(key=lambda e: e.snapshot_id, reverse=True)
        return entries[:limit] if limit > 0 else entries

    def latest_for(self, path: str) -> SnapshotEntry | None:
        """某个文件最近一次的留底。"""
        try:
            relpath = _relpath(self._fs.resolve(path), self.root).lstrip("/")
        except (SandboxPathError, SandboxFsError):
            relpath = path.lstrip("/")

        for entry in self.list():
            if entry.path == relpath:
                return entry
        return None

    def find(self, snapshot_id: str, path: str | None = None) -> SnapshotEntry | None:
        for entry in self.list():
            if entry.snapshot_id != snapshot_id:
                continue
            if path is None or entry.path == path.lstrip("/"):
                return entry
        return None

    def prune(
        self, *, keep: int = 200, max_age_days: int = 30
    ) -> list[str]:
        """删除超出保留数量或超过天数的快照，返回被删除的 id。

        只删本模块生成的 id（时间戳+序号+uuid），不会碰工作区其它文件；
        对应宿主清单也一并删除。删除失败只跳过，不影响运行。
        """
        ids = sorted(
            {entry.snapshot_id for entry in self.list()}, reverse=True
        )
        ids = [sid for sid in ids if _SNAPSHOT_ID_RE.fullmatch(sid)]
        keep = max(0, int(keep))
        cutoff = (
            datetime.now(UTC) - timedelta(days=int(max_age_days))
            if max_age_days and int(max_age_days) > 0
            else None
        )
        removed: list[str] = []
        for index, snapshot_id in enumerate(ids):
            created = _snapshot_time(snapshot_id)
            too_old = (
                cutoff is not None
                and created is not None
                and created < cutoff
            )
            if index < keep and not too_old:
                continue
            target = posixpath.join(self.backup_root, snapshot_id)
            result = self._sandbox.run(
                f"rm -rf -- {shlex.quote(target)}"
            )
            if not result.ok:
                continue
            manifest = self._manifest_path(snapshot_id)
            if manifest is not None:
                try:
                    manifest.unlink(missing_ok=True)
                except OSError:
                    pass
            removed.append(snapshot_id)
        return removed

    def read(self, entry: SnapshotEntry) -> str:
        target = posixpath.join(self.backup_root, entry.snapshot_id, entry.path)
        content = self._fs.read_text(
            target, max_bytes=MAX_OPERATIONAL_BYTES
        )
        manifest = self._load_manifest(entry.snapshot_id)
        if manifest:
            record = manifest["files"].get(entry.path)
            expected = str((record or {}).get("sha256") or "")
            actual = hashlib.sha256(content.encode("utf-8")).hexdigest()
            if expected and actual != expected:
                raise SnapshotTamperedError(
                    f"快照 {entry.snapshot_id} 内容校验失败，可能已被修改"
                )
        return content

    # ------------------------------------------------------------------
    # 恢复
    # ------------------------------------------------------------------

    def restore(self, entry: SnapshotEntry) -> RestoreResult:
        """把文件还原到该快照的状态；还原前先把当前内容留底。

        留底记的是「当时不存在」时，还原结果是**删掉这个文件**。还原成空文件不算
        回到原状 —— 那会在工作区里留下一堆删不掉的垃圾文件。
        """
        target = posixpath.join(self.root, entry.path)
        absent = self.was_absent(entry)

        original = ""
        if not absent:
            try:
                original = self.read(entry)
            except (SandboxFsError, SandboxPathError) as exc:
                return RestoreResult(
                    ok=False, path=entry.path, message=f"快照内容读取失败：{exc}"
                )

        current: str | None = None
        try:
            info = self._fs.stat(target)
            if info.exists and info.is_file:
                current = self._fs.read_text(target, max_bytes=MAX_OPERATIONAL_BYTES)
        except (SandboxFsError, SandboxPathError):
            current = None

        if absent and current is None:
            return RestoreResult(
                ok=True,
                path=entry.path,
                snapshot_id=entry.snapshot_id,
                message=f"{entry.path} 当前已不存在，无需回滚。",
            )

        if not absent and current == original:
            return RestoreResult(
                ok=True,
                path=entry.path,
                snapshot_id=entry.snapshot_id,
                message="当前内容与该快照一致，无需回滚。",
            )

        # 先给「当前状态」留底，让回滚本身也可以被回滚
        undo_snapshot_id: str | None = None
        if current is not None:
            try:
                undo_snapshot_id = self.save(target, current)
            except (SandboxFsError, SandboxPathError) as exc:
                return RestoreResult(
                    ok=False,
                    path=entry.path,
                    message=f"回滚前的留底失败，已中止：{exc}",
                )

        # 回滚一次新建 = 删除。删之前已经留了底，所以这次删除本身也能被回滚
        if absent:
            try:
                self._fs.remove(target)
            except (SandboxFsError, SandboxPathError) as exc:
                return RestoreResult(
                    ok=False, path=entry.path, message=f"回滚删除失败：{exc}"
                )
            diff = unified_diff(current or "", "", entry.path)
            added, removed = count_changes(diff)
            return RestoreResult(
                ok=True,
                path=entry.path,
                snapshot_id=entry.snapshot_id,
                undo_snapshot_id=undo_snapshot_id,
                added=added,
                removed=removed,
                diff=diff,
                message=(
                    f"已删除 {entry.path}（回滚新建：留底时这个文件不存在，"
                    f"当前内容已留底为 {undo_snapshot_id or '无'}）"
                ),
            )

        try:
            self._fs.write_text(target, original)
        except (SandboxFsError, SandboxPathError) as exc:
            return RestoreResult(ok=False, path=entry.path, message=f"回滚写入失败：{exc}")

        diff = unified_diff(current or "", original, entry.path)
        added, removed = count_changes(diff)
        return RestoreResult(
            ok=True,
            path=entry.path,
            snapshot_id=entry.snapshot_id,
            undo_snapshot_id=undo_snapshot_id,
            added=added,
            removed=removed,
            diff=diff,
            message=f"已回滚 {entry.path} 到快照 {entry.snapshot_id}",
        )
