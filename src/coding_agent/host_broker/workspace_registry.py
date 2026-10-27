"""本机 host 工作区注册：目录选择与 handle 生成都在客户端。

A3/A2 只拿到 workspace_id + root_handle + a2l_id；绝对路径留在 A2L。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import uuid
from collections.abc import Callable, MutableMapping
from pathlib import Path
from typing import Protocol

from coding_agent.host_broker.errors import BrokerOpError
from coding_agent.host_broker.paths import HostPathError, HostPathPolicy
from coding_agent.host_broker.protocol import CANCELLED
from coding_agent.host_broker.store import WorkspaceRootStore


class WorkspaceRegistrar(Protocol):
    async def register(self) -> dict[str, str]: ...


class TkWorkspaceRegistrar:
    def __init__(
        self,
        roots: MutableMapping[str, str],
        *,
        a2l_id: str,
        picker: Callable[[], str] | None = None,
        path_policy: HostPathPolicy | None = None,
        store: WorkspaceRootStore | None = None,
    ) -> None:
        self.roots = roots
        self.a2l_id = str(a2l_id)
        self.picker = picker
        self.path_policy = path_policy or HostPathPolicy()
        self.store = store

    async def register(self) -> dict[str, str]:
        return await asyncio.to_thread(self._register_sync)

    def _register_sync(self) -> dict[str, str]:
        path = self.picker() if self.picker else self._pick()
        if not path:
            raise BrokerOpError(CANCELLED, "用户取消选择目录")
        try:
            root = self.path_policy.validate_root(path).root
        except HostPathError as exc:
            raise BrokerOpError(exc.code, exc.message) from exc
        for workspace_id, existing in self.roots.items():
            if os.path.normcase(os.path.normpath(existing)) == os.path.normcase(
                os.path.normpath(root)
            ):
                root_handle = "h_" + hashlib.sha256(
                    root.encode("utf-8")
                ).hexdigest()[:16]
                self._remember(workspace_id, root, root_handle)
                return {
                    "workspace_id": workspace_id,
                    "root_handle": root_handle,
                    "display_name": Path(root).name or "host",
                    "a2l_id": self.a2l_id,
                }
        workspace_id = f"ws_{uuid.uuid4().hex[:12]}"
        root_handle = "h_" + hashlib.sha256(root.encode("utf-8")).hexdigest()[:16]
        self.roots[workspace_id] = root
        self._remember(workspace_id, root, root_handle)
        return {
            "workspace_id": workspace_id,
            "root_handle": root_handle,
            "display_name": Path(root).name or "host",
            "a2l_id": self.a2l_id,
        }

    def _remember(
        self, workspace_id: str, root: str, root_handle: str
    ) -> None:
        if self.store is None:
            return
        try:
            self.store.upsert(
                workspace_id,
                root,
                display_name=Path(root).name or "host",
                root_handle=root_handle,
                a2l_id=self.a2l_id,
            )
        except Exception as exc:  # noqa: BLE001 - 本次会话仍可用
            print(f"A2L 工作区映射保存失败：{exc}", file=sys.stderr)

    def _pick(self) -> str:
        if os.name == "nt":
            native = _pick_windows_folder()
            if native is not None:
                return native
        return _pick_tk_folder()


def _pick_windows_folder() -> str | None:
    """用 shell32 原生目录选择器；不依赖 Tk 的主线程/可见窗口。

    A2L 的注册请求在 `asyncio.to_thread` 里执行，Tk 的隐藏根窗口在
    工作线程里经常不显示对话框；SHBrowseForFolderW 只要求当前线程
    做过 COM 初始化。
    """
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:  # noqa: BLE001 - 非 Windows/精简环境
        return None

    class _BrowseInfo(ctypes.Structure):
        _fields_ = [
            ("hwndOwner", wintypes.HWND),
            ("pidlRoot", ctypes.c_void_p),
            ("pszDisplayName", wintypes.LPWSTR),
            ("lpszTitle", wintypes.LPCWSTR),
            ("ulFlags", wintypes.UINT),
            ("lpfn", ctypes.c_void_p),
            ("lParam", wintypes.LPARAM),
            ("iImage", ctypes.c_int),
        ]

    try:
        shell32 = ctypes.windll.shell32
        ole32 = ctypes.windll.ole32
        user32 = ctypes.windll.user32
        shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
        shell32.SHBrowseForFolderW.argtypes = [
            ctypes.POINTER(_BrowseInfo)
        ]
        shell32.SHGetPathFromIDListW.restype = wintypes.BOOL
        shell32.SHGetPathFromIDListW.argtypes = [
            ctypes.c_void_p,
            wintypes.LPWSTR,
        ]
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.SetWindowPos.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        user32.SetWindowPos.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.BringWindowToTop.argtypes = [wintypes.HWND]
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        # COINIT_APARTMENTTHREADED；RPC_E_CHANGED_MODE 表示线程已初始化。
        hr = int(ole32.CoInitializeEx(None, 0x2))
        release_com = hr in (0, 1)
        try:
            callback_type = ctypes.WINFUNCTYPE(
                ctypes.c_int,
                wintypes.HWND,
                wintypes.UINT,
                wintypes.LPARAM,
                wintypes.LPARAM,
            )

            def _on_event(hwnd, message, _lparam, _data):
                if message == 1:  # BFFM_INITIALIZED
                    # 后台进程的 SetForegroundWindow 常被系统拒绝；保持
                    # TOPMOST 直到对话框关闭，确保不会被主窗口挡住。
                    flags = 0x0002 | 0x0001 | 0x0040
                    user32.ShowWindow(hwnd, 5)  # SW_SHOW
                    user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, flags)
                    user32.SetForegroundWindow(hwnd)
                    user32.BringWindowToTop(hwnd)
                return 0

            callback = callback_type(_on_event)
            display = ctypes.create_unicode_buffer(260)
            info = _BrowseInfo()
            info.hwndOwner = user32.GetForegroundWindow()
            info.pszDisplayName = ctypes.cast(
                display, wintypes.LPWSTR
            )
            info.lpszTitle = "选择 CodingAgent 本机工作区"
            # BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE | BIF_EDITBOX
            info.ulFlags = 0x01 | 0x40 | 0x10
            info.lpfn = ctypes.cast(callback, ctypes.c_void_p)
            pidl = shell32.SHBrowseForFolderW(ctypes.byref(info))
            if not pidl:
                return ""
            try:
                path = ctypes.create_unicode_buffer(32768)
                if not shell32.SHGetPathFromIDListW(pidl, path):
                    return ""
                return str(path.value or "")
            finally:
                ole32.CoTaskMemFree(ctypes.c_void_p(pidl))
        finally:
            if release_com:
                ole32.CoUninitialize()
    except Exception as exc:  # noqa: BLE001 - 退回 Tk
        print(f"原生目录选择器不可用：{exc}", file=sys.stderr)
        return None


def _pick_tk_folder() -> str:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception:  # noqa: BLE001 - headless
        return ""
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askdirectory(
            title="选择 CodingAgent 本机工作区"
        )
        root.destroy()
        return str(selected or "")
    except Exception:  # noqa: BLE001
        return ""
