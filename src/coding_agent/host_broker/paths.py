"""宿主路径守卫：只接受本地固定 NTFS，拒绝 UNC/设备/云盘/重解析点。

Windows 侧尽量用 Win32 handle 级 API 取最终路径；字符串 `realpath`
无法可靠处理 junction、8.3 短名、ADS 与云占位文件。
"""

from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass

FILE_ATTRIBUTE_DIRECTORY = 0x10
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF
DRIVE_FIXED = 3
INVALID_HANDLE_VALUE = 0xFFFFFFFFFFFFFFFF

_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class HostPathError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class RootInfo:
    root: str
    filesystem: str


def _within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath(
            [os.path.normcase(path), os.path.normcase(root)]
        ) == os.path.normcase(root)
    except ValueError:
        return False


def _reject_lexical(path: str) -> None:
    text = str(path or "").strip()
    if not text or "\x00" in text:
        raise HostPathError("INVALID_PATH", "路径为空或含 NUL")
    if text.startswith(("\\\\", "//")):
        raise HostPathError("INVALID_PATH", "不允许 UNC/网络路径")
    if text.startswith(("\\\\.\\", "//./", "\\\\?\\", "//?/")):
        raise HostPathError("INVALID_PATH", "不允许设备/扩展路径")
    if not os.path.isabs(text):
        raise HostPathError("INVALID_PATH", "必须是绝对路径")
    if os.name == "nt":
        drive, rest = os.path.splitdrive(text)
        if not drive or not drive[0].isalpha():
            raise HostPathError("INVALID_PATH", "必须是带盘符的本地路径")
        if rest and rest[0] not in ("\\", "/"):
            raise HostPathError("INVALID_PATH", "不允许盘符相对路径")
        parts = [part for part in rest.replace("/", "\\").split("\\") if part]
        for part in parts:
            _reject_component(part)
    else:
        parts = [part for part in text.split("/") if part]
        for part in parts:
            _reject_component(part, windows=False)


def _reject_component(part: str, *, windows: bool = True) -> None:
    if part in (".", ".."):
        raise HostPathError("INVALID_PATH", "不允许 . 或 .. 路径段")
    if "\x00" in part:
        raise HostPathError("INVALID_PATH", "路径段含 NUL")
    if windows:
        if ":" in part:
            raise HostPathError("INVALID_PATH", "不允许 ADS/冒号路径段")
        cleaned = part.rstrip(" .")
        base = cleaned.split(".", 1)[0].upper()
        if base in _RESERVED_NAMES:
            raise HostPathError("INVALID_PATH", f"不允许 Windows 保留名：{part}")


def _win_kernel32():
    return ctypes.windll.kernel32


def _win_attributes(path: str) -> int:
    kernel32 = _win_kernel32()
    kernel32.GetFileAttributesW.argtypes = [ctypes.c_wchar_p]
    kernel32.GetFileAttributesW.restype = ctypes.c_uint32
    attrs = int(kernel32.GetFileAttributesW(str(path)))
    if attrs == INVALID_FILE_ATTRIBUTES:
        raise HostPathError("WORKSPACE_UNAVAILABLE", "无法读取路径属性")
    return attrs


def _win_drive_type(path: str) -> int:
    kernel32 = _win_kernel32()
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    if not drive:
        return 0
    kernel32.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
    kernel32.GetDriveTypeW.restype = ctypes.c_uint32
    return int(kernel32.GetDriveTypeW(drive + "\\"))


def _win_filesystem(path: str) -> str:
    kernel32 = _win_kernel32()
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    if not drive:
        return ""
    name = ctypes.create_unicode_buffer(256)
    kernel32.GetVolumeInformationW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_uint32,
    ]
    kernel32.GetVolumeInformationW.restype = ctypes.c_int
    ok = kernel32.GetVolumeInformationW(
        drive + "\\", None, 0, None, None, None, name, 256
    )
    return name.value if ok else ""


def _win_final_path(path: str) -> str:
    """用 CreateFileW + GetFinalPathNameByHandleW 取最终路径。"""
    kernel32 = _win_kernel32()
    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.GetFinalPathNameByHandleW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
    ]
    kernel32.GetFinalPathNameByHandleW.restype = ctypes.c_uint32
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int

    handle = kernel32.CreateFileW(
        str(path),
        0,
        0x1 | 0x2 | 0x4,  # share read/write/delete
        None,
        3,  # OPEN_EXISTING
        0x02000000,  # FILE_FLAG_BACKUP_SEMANTICS
        None,
    )
    if handle is None or int(handle) == INVALID_HANDLE_VALUE:
        raise HostPathError("WORKSPACE_UNAVAILABLE", "无法打开路径句柄")
    try:
        size = 32768
        buf = ctypes.create_unicode_buffer(size)
        written = int(
            kernel32.GetFinalPathNameByHandleW(handle, buf, size, 0)
        )
        if written == 0:
            raise HostPathError("WORKSPACE_UNAVAILABLE", "无法解析最终路径")
        result = buf.value
    finally:
        kernel32.CloseHandle(handle)
    if result.startswith("\\\\?\\UNC\\"):
        raise HostPathError("INVALID_PATH", "最终路径是 UNC")
    if result.startswith("\\\\?\\"):
        result = result[4:]
    return result


def _onedrive_roots() -> list[str]:
    roots: list[str] = []
    for name in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        value = os.environ.get(name)
        if value:
            roots.append(value)
    return roots


class HostPathPolicy:
    def __init__(self, *, root_policy: str = "local_ntfs_only") -> None:
        self.root_policy = str(root_policy or "local_ntfs_only")

    def validate_root(self, path: str) -> RootInfo:
        text = str(path or "").strip()
        _reject_lexical(text)
        expanded = os.path.expandvars(os.path.expanduser(text))
        if not os.path.exists(expanded):
            raise HostPathError("WORKSPACE_UNAVAILABLE", "根目录不存在")
        if not os.path.isdir(expanded):
            raise HostPathError("INVALID_PATH", "根路径不是目录")
        normalized = os.path.normpath(expanded)
        if os.path.dirname(normalized) == normalized:
            raise HostPathError("INVALID_PATH", "不允许文件系统根")
        if os.path.normcase(normalized) == os.path.normcase(
            os.path.normpath(os.path.expanduser("~"))
        ):
            raise HostPathError("INVALID_PATH", "不允许用户 home 根")
        attrs = _win_attributes(expanded) if os.name == "nt" else 0
        if os.name == "nt":
            if attrs & FILE_ATTRIBUTE_REPARSE_POINT:
                raise HostPathError("PATH_ESCAPE", "根目录是重解析点/链接")
            if attrs & (
                FILE_ATTRIBUTE_OFFLINE
                | FILE_ATTRIBUTE_RECALL_ON_OPEN
                | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
            ):
                raise HostPathError(
                    "UNSUPPORTED_FILESYSTEM", "根目录是云占位/离线文件"
                )
            if _win_drive_type(expanded) != DRIVE_FIXED:
                raise HostPathError(
                    "UNSUPPORTED_FILESYSTEM", "只接受本地固定盘"
                )
            if _win_filesystem(expanded).upper() != "NTFS":
                raise HostPathError(
                    "UNSUPPORTED_FILESYSTEM", "只接受 NTFS"
                )
            for onedrive in _onedrive_roots():
                if _within(expanded, onedrive):
                    raise HostPathError(
                        "UNSUPPORTED_FILESYSTEM", "不接受 OneDrive 根"
                    )
            final = _win_final_path(expanded)
            filesystem = "ntfs"
        else:
            final = os.path.realpath(expanded)
            filesystem = "posix"
        if os.path.normcase(final) != os.path.normcase(
            os.path.normpath(expanded)
        ):
            raise HostPathError("PATH_ESCAPE", "根目录经链接解析后发生变化")
        return RootInfo(root=final, filesystem=filesystem)

    def resolve_child(self, root: str, relative: str) -> str:
        root = os.path.normpath(str(root or ""))
        if not root or not os.path.isabs(root):
            raise HostPathError("INVALID_PATH", "根路径必须是绝对路径")
        rel = str(relative or ".").strip().replace("\\", "/")
        if rel in ("", "."):
            return root
        if rel.startswith("/"):
            raise HostPathError("INVALID_PATH", "子路径必须是相对路径")
        parts = [part for part in rel.split("/") if part not in ("", ".")]
        for part in parts:
            _reject_component(part, windows=os.name == "nt")
        if any(part == ".." for part in parts):
            raise HostPathError("INVALID_PATH", "不允许 .. 逃逸")
        candidate = os.path.join(root, *parts) if parts else root
        if os.name == "nt" and os.path.exists(candidate):
            real = _win_final_path(candidate)
        else:
            real = os.path.realpath(candidate)
        if not _within(real, root):
            raise HostPathError("PATH_ESCAPE", "子路径经链接逃出工作区")
        return real

    def relative_to_root(self, root: str, path: str) -> str:
        if not _within(path, root):
            raise HostPathError("PATH_ESCAPE", "路径不在工作区内")
        rel = os.path.relpath(path, root)
        return "." if rel == "." else rel.replace(os.sep, "/")


def validate_local_root(path: str) -> RootInfo:
    return HostPathPolicy().validate_root(path)


def ensure_child_path(root: str, relative: str) -> str:
    return HostPathPolicy().resolve_child(root, relative)
