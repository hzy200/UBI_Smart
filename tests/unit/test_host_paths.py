"""宿主路径守卫：根限制、子路径逃逸、UNC/设备/ADS。"""

from __future__ import annotations

import os
import subprocess

import pytest

from ubismart.host_broker.paths import HostPathError, HostPathPolicy
from ubismart.host_broker.protocol import INVALID_PATH, PATH_ESCAPE


def _make_link(link, target) -> bool:
    if os.name == "nt":
        proc = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
        )
        return proc.returncode == 0
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except OSError:
        return False


def test_validate_root_accepts_local_directory(tmp_path) -> None:
    info = HostPathPolicy().validate_root(str(tmp_path))
    assert info.root
    assert info.filesystem in {"ntfs", "posix"}


def test_validate_root_rejects_file_missing_and_home(tmp_path) -> None:
    policy = HostPathPolicy()
    file_path = tmp_path / "a.txt"
    file_path.write_text("x", encoding="utf-8")
    with pytest.raises(HostPathError):
        policy.validate_root(str(file_path))
    with pytest.raises(HostPathError):
        policy.validate_root(str(tmp_path / "missing"))
    with pytest.raises(HostPathError):
        policy.validate_root(os.path.expanduser("~"))
    with pytest.raises(HostPathError):
        policy.validate_root(os.path.abspath(os.sep))


def test_lexical_rejects_unc_device_and_relative() -> None:
    policy = HostPathPolicy()
    for bad in (
        r"\\server\share",
        r"\\.\PhysicalDrive0",
        r"\\?\C:\work",
        "relative/path",
        "C:relative",
    ):
        with pytest.raises(HostPathError):
            policy.validate_root(bad)


def test_child_rejects_absolute_and_parent_escape(tmp_path) -> None:
    policy = HostPathPolicy()
    root = str(tmp_path)
    with pytest.raises(HostPathError):
        policy.resolve_child(root, os.path.abspath(os.sep))
    with pytest.raises(HostPathError):
        policy.resolve_child(root, "../outside.txt")
    with pytest.raises(HostPathError):
        policy.resolve_child(root, "a/../../outside.txt")


def test_child_rejects_windows_ads_and_reserved_names(tmp_path) -> None:
    if os.name != "nt":
        pytest.skip("Windows-only path syntax")
    policy = HostPathPolicy()
    root = str(tmp_path)
    for bad in ("a.txt:stream", "CON", "NUL.txt", "aux.log"):
        with pytest.raises(HostPathError) as exc:
            policy.resolve_child(root, bad)
        assert exc.value.code == INVALID_PATH


def test_child_and_root_reject_link_escape(tmp_path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    link = root / "link"
    if not _make_link(link, outside):
        pytest.skip("cannot create junction/symlink in this environment")

    policy = HostPathPolicy()
    with pytest.raises(HostPathError) as exc:
        policy.resolve_child(str(root), "link/secret.txt")
    assert exc.value.code == PATH_ESCAPE

    with pytest.raises(HostPathError) as exc:
        policy.validate_root(str(link))
    assert exc.value.code == PATH_ESCAPE


def test_relative_to_root_uses_posix_separator(tmp_path) -> None:
    root = tmp_path / "root"
    child = root / "src" / "a.py"
    child.parent.mkdir(parents=True)
    child.write_text("x", encoding="utf-8")
    policy = HostPathPolicy()
    assert policy.relative_to_root(str(root), str(child)) == "src/a.py"
    with pytest.raises(HostPathError):
        policy.relative_to_root(str(root), str(tmp_path / "elsewhere"))
