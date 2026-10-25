"""RFC-0001：项目归档安全解包。"""

from __future__ import annotations

import io
import stat
import tarfile
import zipfile

import pytest

from coding_agent.workspace.archive import (
    ArchiveError,
    ExtractLimits,
    detect_format,
    safe_extract,
)


def _zip(path, entries: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)


def _tar(path, entries: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))


def test_detect_format_by_name_and_magic(tmp_path) -> None:
    path = tmp_path / "a.zip"
    _zip(path, {"a.txt": b"a"})
    assert detect_format("a.zip", str(path)) == "zip"
    assert detect_format("", str(path)) == "zip"
    with pytest.raises(ArchiveError):
        detect_format("a.bin", str(tmp_path / "missing"))


def test_valid_zip_extracts(tmp_path) -> None:
    path = tmp_path / "a.zip"
    _zip(path, {"src/a.txt": b"hello", "README": b"x"})
    dest = tmp_path / "out"
    stats = safe_extract(str(path), str(dest), name="a.zip")
    assert stats == {"bytes": 6, "files": 2}
    assert (dest / "src" / "a.txt").read_bytes() == b"hello"


def test_zip_slip_is_rejected(tmp_path) -> None:
    path = tmp_path / "a.zip"
    _zip(path, {"../evil.txt": b"x"})
    with pytest.raises(ArchiveError):
        safe_extract(str(path), str(tmp_path / "out"), name="a.zip")
    assert not (tmp_path / "evil.txt").exists()


def test_absolute_path_is_rejected(tmp_path) -> None:
    path = tmp_path / "a.zip"
    _zip(path, {"/etc/passwd": b"x"})
    with pytest.raises(ArchiveError):
        safe_extract(str(path), str(tmp_path / "out"), name="a.zip")


def test_zip_symlink_is_rejected(tmp_path) -> None:
    path = tmp_path / "a.zip"
    with zipfile.ZipFile(path, "w") as archive:
        info = zipfile.ZipInfo("link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "/etc/passwd")
    with pytest.raises(ArchiveError):
        safe_extract(str(path), str(tmp_path / "out"), name="a.zip")


def test_zip_bomb_ratio_is_rejected(tmp_path) -> None:
    path = tmp_path / "bomb.zip"
    _zip(path, {"zeros.bin": b"\0" * (2 * 1024 * 1024)})
    with pytest.raises(ArchiveError):
        safe_extract(
            str(path),
            str(tmp_path / "out"),
            name="bomb.zip",
            limits=ExtractLimits(max_ratio=2),
        )


def test_file_count_limit(tmp_path) -> None:
    path = tmp_path / "a.zip"
    _zip(path, {f"f{i}.txt": b"x" for i in range(3)})
    with pytest.raises(ArchiveError):
        safe_extract(
            str(path),
            str(tmp_path / "out"),
            name="a.zip",
            limits=ExtractLimits(max_files=2),
        )


def test_single_file_limit(tmp_path) -> None:
    path = tmp_path / "a.zip"
    _zip(path, {"a.txt": b"0123456789"})
    with pytest.raises(ArchiveError):
        safe_extract(
            str(path),
            str(tmp_path / "out"),
            name="a.zip",
            limits=ExtractLimits(max_single_bytes=5),
        )


def test_git_and_agent_entries_are_skipped(tmp_path) -> None:
    path = tmp_path / "a.zip"
    _zip(
        path,
        {
            ".git/config": b"[core]",
            ".agent/audit/x.jsonl": b"{}",
            "keep.txt": b"ok",
        },
    )
    dest = tmp_path / "out"
    safe_extract(str(path), str(dest), name="a.zip")
    assert not (dest / ".git").exists()
    assert not (dest / ".agent").exists()
    assert (dest / "keep.txt").read_bytes() == b"ok"


def test_skip_names_can_be_relaxed_for_restore(tmp_path) -> None:
    path = tmp_path / "a.zip"
    _zip(path, {".git/config": b"[core]", "keep.txt": b"ok"})
    dest = tmp_path / "out"
    safe_extract(
        str(path),
        str(dest),
        name="a.zip",
        skip_names=frozenset(),
    )
    assert (dest / ".git" / "config").read_bytes() == b"[core]"
    assert (dest / "keep.txt").read_bytes() == b"ok"


def test_safe_extract_reports_progress(tmp_path) -> None:
    path = tmp_path / "a.zip"
    _zip(path, {"a.txt": b"hello", "b.txt": b"world!"})
    seen: list[tuple[int, int, int]] = []
    safe_extract(
        str(path),
        str(tmp_path / "out"),
        name="a.zip",
        on_progress=lambda done, total, files: seen.append(
            (done, total, files)
        ),
    )
    assert seen
    assert seen[-1] == (11, 11, 2)
    assert all(total == 11 for _done, total, _files in seen)


def test_valid_tar_extracts(tmp_path) -> None:
    path = tmp_path / "a.tar.gz"
    _tar(path, {"pkg/x.txt": b"1234"})
    dest = tmp_path / "out"
    stats = safe_extract(str(path), str(dest), name="a.tar.gz")
    assert stats == {"bytes": 4, "files": 1}
    assert (dest / "pkg" / "x.txt").read_bytes() == b"1234"


def test_tar_symlink_is_rejected(tmp_path) -> None:
    path = tmp_path / "a.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo("link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        archive.addfile(info)
    with pytest.raises(ArchiveError):
        safe_extract(str(path), str(tmp_path / "out"), name="a.tar.gz")


def test_tar_hardlink_is_rejected(tmp_path) -> None:
    path = tmp_path / "a.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo("hard")
        info.type = tarfile.LNKTYPE
        info.linkname = "target"
        archive.addfile(info)
    with pytest.raises(ArchiveError):
        safe_extract(str(path), str(tmp_path / "out"), name="a.tar.gz")


def test_failure_cleans_destination(tmp_path) -> None:
    path = tmp_path / "a.zip"
    _zip(path, {"ok.txt": b"x", "../evil": b"y"})
    dest = tmp_path / "out"
    with pytest.raises(ArchiveError):
        safe_extract(str(path), str(dest), name="a.zip")
    assert not dest.exists()
