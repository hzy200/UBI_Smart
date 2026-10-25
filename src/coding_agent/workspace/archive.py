"""项目归档的安全解包（RFC-0001，M3）。

只接受 zip / tar / tar.gz；`.tar.zst` 不在这里处理（stdlib 3.11 不支持
zstd，上传入口会明确拒绝）。

安全规则（全部 fail-closed）：

- 拒绝绝对路径、`..`、盘符、NUL；
- 拒绝符号链接、硬链接、设备/FIFO 与加密 zip；
- 限制文件数、单文件大小、解压后总量与解压比（zip bomb）；
- 解包目标必须落在 dest 内，重复条目拒绝；
- 任一步失败都会清理 dest，避免半成品被激活。
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import tarfile
import zipfile
from dataclasses import dataclass

_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_CHUNK = 1024 * 1024


class ArchiveError(ValueError):
    """归档格式不安全或不支持。"""


@dataclass(frozen=True, slots=True)
class ExtractLimits:
    max_files: int = 200_000
    max_total_bytes: int = 0
    max_single_bytes: int = 0
    max_ratio: int = 200


def detect_format(name: str, path: str) -> str:
    """按文件名与魔数判断格式；返回 `zip` 或 `tar`。"""
    lower = str(name or "").lower()
    if lower.endswith(".zip"):
        return "zip"
    if lower.endswith((".tar", ".tar.gz", ".tgz")):
        return "tar"
    try:
        with open(path, "rb") as handle:
            head = handle.read(4)
    except OSError as exc:
        raise ArchiveError(f"无法读取归档：{exc}") from exc
    if head.startswith(b"PK\x03\x04"):
        return "zip"
    if head.startswith(b"\x1f\x8b") or head.startswith(b"ustar"):
        return "tar"
    raise ArchiveError("只支持 zip / tar / tar.gz 归档")


def _safe_rel(
    name: str, skip_names: frozenset[str] = frozenset({".git", ".agent"})
) -> str:
    text = str(name or "").replace("\\", "/")
    if "\x00" in text or _DRIVE_RE.match(text) or text.startswith("/"):
        raise ArchiveError(f"归档包含非法绝对路径：{name!r}")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ArchiveError(f"归档包含路径穿越：{name!r}")
    # 默认不信任上传包里的 .git（hooks/config 可执行）与 .agent（审计/快照）；
    # 从自家 revision 归档恢复时由调用方显式放宽。
    if any(part in skip_names for part in parts):
        return ""
    return "/".join(parts)


def _target(dest: str, rel: str) -> str:
    if not rel:
        raise ArchiveError("归档包含空条目")
    root = os.path.realpath(dest)
    target = os.path.realpath(os.path.join(root, rel))
    if target != root and not target.startswith(root + os.sep):
        raise ArchiveError(f"归档条目越界：{rel!r}")
    return target


def _check_size(
    limits: ExtractLimits,
    *,
    size: int,
    total: int,
    files: int,
    archive_size: int,
) -> None:
    if limits.max_single_bytes and size > limits.max_single_bytes:
        raise ArchiveError(f"单文件超过上限：{size} 字节")
    if limits.max_total_bytes and total > limits.max_total_bytes:
        raise ArchiveError(f"解压后总量超过上限：{total} 字节")
    if limits.max_ratio and archive_size > 0:
        if total > archive_size * limits.max_ratio:
            raise ArchiveError(
                f"解压比超过上限：{total}/{archive_size}"
            )
    if limits.max_files and files > limits.max_files:
        raise ArchiveError(f"文件数超过上限：{files}")


def _extract_zip(
    path: str,
    dest: str,
    limits: ExtractLimits,
    archive_size: int,
    skip_names: frozenset[str],
) -> tuple[int, int]:
    total = 0
    files = 0
    written_total = 0
    seen: set[str] = set()
    try:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                rel = _safe_rel(info.filename, skip_names)
                if not rel:
                    continue
                if info.flag_bits & 0x1:
                    raise ArchiveError("不支持加密 zip")
                mode = (info.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode):
                    raise ArchiveError(f"归档包含符号链接：{info.filename!r}")
                target = _target(dest, rel)
                if rel in seen:
                    raise ArchiveError(f"归档包含重复条目：{rel!r}")
                seen.add(rel)
                if info.is_dir():
                    os.makedirs(target, exist_ok=True)
                    continue
                files += 1
                total += int(info.file_size)
                _check_size(
                    limits,
                    size=int(info.file_size),
                    total=total,
                    files=files,
                    archive_size=archive_size,
                )
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with archive.open(info) as source, open(
                    target, "wb"
                ) as output:
                    written = 0
                    while True:
                        chunk = source.read(_CHUNK)
                        if not chunk:
                            break
                        written += len(chunk)
                        written_total += len(chunk)
                        if (
                            limits.max_single_bytes
                            and written > limits.max_single_bytes
                        ):
                            raise ArchiveError(
                                f"单文件超过上限：{rel!r}"
                            )
                        if (
                            limits.max_total_bytes
                            and written_total > limits.max_total_bytes
                        ):
                            raise ArchiveError(
                                "解压后总量超过上限"
                            )
                        if (
                            limits.max_ratio
                            and archive_size > 0
                            and written_total
                            > archive_size * limits.max_ratio
                        ):
                            raise ArchiveError("解压比超过上限")
                        output.write(chunk)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ArchiveError(f"zip 解包失败：{exc}") from exc
    return total, files


def _extract_tar(
    path: str,
    dest: str,
    limits: ExtractLimits,
    archive_size: int,
    skip_names: frozenset[str],
) -> tuple[int, int]:
    total = 0
    files = 0
    written_total = 0
    seen: set[str] = set()
    try:
        with tarfile.open(path, "r:*") as archive:
            for member in archive:
                rel = _safe_rel(member.name, skip_names)
                if not rel:
                    continue
                if member.isdir():
                    target = _target(dest, rel)
                    if rel in seen:
                        raise ArchiveError(
                            f"归档包含重复条目：{rel!r}"
                        )
                    seen.add(rel)
                    os.makedirs(target, exist_ok=True)
                    continue
                if not member.isreg():
                    raise ArchiveError(
                        f"归档包含非普通文件：{member.name!r}"
                    )
                target = _target(dest, rel)
                if rel in seen:
                    raise ArchiveError(f"归档包含重复条目：{rel!r}")
                seen.add(rel)
                files += 1
                total += int(member.size)
                _check_size(
                    limits,
                    size=int(member.size),
                    total=total,
                    files=files,
                    archive_size=archive_size,
                )
                os.makedirs(os.path.dirname(target), exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise ArchiveError(f"无法读取条目：{member.name!r}")
                with source, open(target, "wb") as output:
                    while True:
                        chunk = source.read(_CHUNK)
                        if not chunk:
                            break
                        written_total += len(chunk)
                        if (
                            limits.max_total_bytes
                            and written_total > limits.max_total_bytes
                        ):
                            raise ArchiveError(
                                "解压后总量超过上限"
                            )
                        if (
                            limits.max_ratio
                            and archive_size > 0
                            and written_total
                            > archive_size * limits.max_ratio
                        ):
                            raise ArchiveError("解压比超过上限")
                        output.write(chunk)
    except (tarfile.TarError, OSError) as exc:
        raise ArchiveError(f"tar 解包失败：{exc}") from exc
    return total, files


def actual_size(root: str) -> tuple[int, int]:
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


def safe_extract(
    archive_path: str,
    dest: str,
    *,
    name: str = "",
    limits: ExtractLimits | None = None,
    skip_names: frozenset[str] = frozenset({".git", ".agent"}),
) -> dict[str, int]:
    """解包到 dest；失败时清理 dest 并抛 ArchiveError。"""
    limits = limits or ExtractLimits()
    archive_size = os.path.getsize(archive_path)
    os.makedirs(dest, exist_ok=True)
    try:
        fmt = detect_format(name or archive_path, archive_path)
        if fmt == "zip":
            total, files = _extract_zip(
                archive_path,
                dest,
                limits,
                archive_size,
                skip_names,
            )
        else:
            total, files = _extract_tar(
                archive_path,
                dest,
                limits,
                archive_size,
                skip_names,
            )
        actual_total, actual_files = actual_size(dest)
        _check_size(
            limits,
            size=0,
            total=actual_total,
            files=actual_files,
            archive_size=archive_size,
        )
        return {"bytes": actual_total, "files": actual_files}
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise
