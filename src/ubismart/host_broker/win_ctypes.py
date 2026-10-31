"""Windows 低层 ctypes 声明与薄封装。

这里只放 Win32 调用；策略、grant、ACL 与业务判断都在 `win_isolation.py`。
目的是让 Linux CI 能导入全部策略模块，而真正不可移植的部分尽量薄。
"""

from __future__ import annotations

import ctypes
import os

IS_WINDOWS = os.name == "nt"


class IsolationError(RuntimeError):
    pass


def _kernel32():
    if not IS_WINDOWS:
        raise IsolationError("仅支持 Windows")
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _userenv():
    if not IS_WINDOWS:
        raise IsolationError("仅支持 Windows")
    return ctypes.WinDLL("userenv", use_last_error=True)


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _JOBOBJECT_BASIC_UI_RESTRICTIONS(ctypes.Structure):
    _fields_ = [("UIRestrictionsClass", ctypes.c_uint32)]


JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_UILIMIT_ALL = 0x000000FF
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_BASIC_UI_RESTRICTIONS = 4


class _SECURITY_CAPABILITIES(ctypes.Structure):
    _fields_ = [
        ("AppContainerSid", ctypes.c_void_p),
        ("Capabilities", ctypes.c_void_p),
        ("CapabilityCount", ctypes.c_uint32),
        ("Reserved", ctypes.c_uint32),
    ]


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("lpReserved", ctypes.c_wchar_p),
        ("lpDesktop", ctypes.c_wchar_p),
        ("lpTitle", ctypes.c_wchar_p),
        ("dwX", ctypes.c_uint32),
        ("dwY", ctypes.c_uint32),
        ("dwXSize", ctypes.c_uint32),
        ("dwYSize", ctypes.c_uint32),
        ("dwXCountChars", ctypes.c_uint32),
        ("dwYCountChars", ctypes.c_uint32),
        ("dwFillAttribute", ctypes.c_uint32),
        ("dwFlags", ctypes.c_uint32),
        ("wShowWindow", ctypes.c_uint16),
        ("cbReserved2", ctypes.c_uint16),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", ctypes.c_void_p),
        ("hStdOutput", ctypes.c_void_p),
        ("hStdError", ctypes.c_void_p),
    ]


class _STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [
        ("StartupInfo", _STARTUPINFOW),
        ("lpAttributeList", ctypes.c_void_p),
    ]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", ctypes.c_void_p),
        ("hThread", ctypes.c_void_p),
        ("dwProcessId", ctypes.c_uint32),
        ("dwThreadId", ctypes.c_uint32),
    ]


def create_job(
    *,
    max_processes: int,
    process_memory_mb: int,
    job_memory_mb: int,
    ui_restrictions: bool,
) -> int:
    kernel32 = _kernel32()
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.SetInformationJobObject.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    kernel32.SetInformationJobObject.restype = ctypes.c_bool
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        raise IsolationError(
            f"CreateJobObject 失败：{ctypes.get_last_error()}"
        )
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = (
        JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        | JOB_OBJECT_LIMIT_PROCESS_MEMORY
        | JOB_OBJECT_LIMIT_JOB_MEMORY
        | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    )
    info.BasicLimitInformation.ActiveProcessLimit = max(
        1, int(max_processes)
    )
    info.ProcessMemoryLimit = max(1, int(process_memory_mb)) * 1024 * 1024
    info.JobMemoryLimit = max(1, int(job_memory_mb)) * 1024 * 1024
    if not kernel32.SetInformationJobObject(
        handle,
        JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        kernel32.CloseHandle(handle)
        raise IsolationError(
            f"SetInformationJobObject 失败：{ctypes.get_last_error()}"
        )
    if ui_restrictions:
        ui = _JOBOBJECT_BASIC_UI_RESTRICTIONS(JOB_OBJECT_UILIMIT_ALL)
        if not kernel32.SetInformationJobObject(
            handle,
            JOB_OBJECT_BASIC_UI_RESTRICTIONS,
            ctypes.byref(ui),
            ctypes.sizeof(ui),
        ):
            kernel32.CloseHandle(handle)
            raise IsolationError(
                f"UI 限制设置失败：{ctypes.get_last_error()}"
            )
    return int(handle)


def assign_job(handle: int, process_handle: int | None = None) -> None:
    kernel32 = _kernel32()
    kernel32.AssignProcessToJobObject.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    kernel32.AssignProcessToJobObject.restype = ctypes.c_bool
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    target = process_handle or kernel32.GetCurrentProcess()
    if not kernel32.AssignProcessToJobObject(
        ctypes.c_void_p(handle), ctypes.c_void_p(int(target))
    ):
        raise IsolationError(
            f"AssignProcessToJobObject 失败：{ctypes.get_last_error()}"
        )


def close_job(
    handle: int, *, terminate: bool, self_assigned: bool
) -> None:
    if not handle:
        return
    kernel32 = _kernel32()
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    if terminate or not self_assigned:
        kernel32.TerminateJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        kernel32.TerminateJobObject.restype = ctypes.c_bool
        kernel32.TerminateJobObject(ctypes.c_void_p(handle), 1)
    kernel32.CloseHandle(ctypes.c_void_p(handle))


def low_integrity_supported() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = _kernel32()
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        advapi32.OpenProcessToken.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        advapi32.OpenProcessToken.restype = ctypes.c_bool
        advapi32.ConvertStringSidToSidW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        advapi32.ConvertStringSidToSidW.restype = ctypes.c_bool
        token = ctypes.c_void_p()
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(),
            0x0008,
            ctypes.byref(token),
        ):
            return False
        sid = ctypes.c_void_p()
        try:
            return bool(
                advapi32.ConvertStringSidToSidW(
                    ctypes.c_wchar_p("S-1-16-4096"),
                    ctypes.byref(sid),
                )
            )
        finally:
            if sid:
                kernel32.LocalFree(sid)
            if token:
                kernel32.CloseHandle(token)
    except Exception:  # noqa: BLE001 - 探测失败按不可用处理
        return False


def set_low_integrity() -> bool:
    if not IS_WINDOWS:
        raise IsolationError("仅支持 Windows")
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = _kernel32()
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    advapi32.OpenProcessToken.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.OpenProcessToken.restype = ctypes.c_bool
    advapi32.ConvertStringSidToSidW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.ConvertStringSidToSidW.restype = ctypes.c_bool
    advapi32.GetLengthSid.argtypes = [ctypes.c_void_p]
    advapi32.GetLengthSid.restype = ctypes.c_uint32
    advapi32.SetTokenInformation.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    advapi32.SetTokenInformation.restype = ctypes.c_bool
    token = ctypes.c_void_p()
    TOKEN_ADJUST_DEFAULT = 0x0080
    TOKEN_QUERY = 0x0008
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(),
        TOKEN_ADJUST_DEFAULT | TOKEN_QUERY,
        ctypes.byref(token),
    ):
        return False
    sid = ctypes.c_void_p()
    try:
        if not advapi32.ConvertStringSidToSidW(
            ctypes.c_wchar_p("S-1-16-4096"), ctypes.byref(sid)
        ):
            return False

        class SID_AND_ATTRIBUTES(ctypes.Structure):
            _fields_ = [
                ("Sid", ctypes.c_void_p),
                ("Attributes", ctypes.c_uint32),
            ]

        class TOKEN_MANDATORY_LABEL(ctypes.Structure):
            _fields_ = [("Label", SID_AND_ATTRIBUTES)]

        label = TOKEN_MANDATORY_LABEL()
        label.Label.Sid = sid
        label.Label.Attributes = 0x20
        size = ctypes.sizeof(label) + advapi32.GetLengthSid(sid)
        return bool(
            advapi32.SetTokenInformation(
                ctypes.c_void_p(token.value),
                25,
                ctypes.byref(label),
                size,
            )
        )
    finally:
        if sid:
            kernel32.LocalFree(sid)
        if token:
            kernel32.CloseHandle(token)


def create_appcontainer_profile(name: str) -> tuple[int, str]:
    userenv = _userenv()
    userenv.CreateAppContainerProfile.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    userenv.CreateAppContainerProfile.restype = ctypes.c_long
    sid = ctypes.c_void_p()
    hr = userenv.CreateAppContainerProfile(
        name, name, name, None, 0, ctypes.byref(sid)
    )
    if hr != 0:
        raise IsolationError(f"CreateAppContainerProfile 失败：{hr}")
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.ConvertSidToStringSidW.restype = ctypes.c_bool
    ptr = ctypes.c_void_p()
    if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(ptr)):
        raise IsolationError("SID 转换失败")
    kernel32 = _kernel32()
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    try:
        sid_str = ctypes.wstring_at(ptr)
    finally:
        kernel32.LocalFree(ptr)
    return int(sid.value), sid_str


def delete_appcontainer_profile(name: str) -> None:
    userenv = _userenv()
    userenv.DeleteAppContainerProfile.argtypes = [ctypes.c_wchar_p]
    userenv.DeleteAppContainerProfile.restype = ctypes.c_long
    userenv.DeleteAppContainerProfile(name)


def spawn_in_appcontainer(
    *,
    exe: str,
    command_line: str,
    sid: int,
    timeout: float,
) -> None:
    kernel32 = _kernel32()
    kernel32.InitializeProcThreadAttributeList.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.InitializeProcThreadAttributeList.restype = ctypes.c_bool
    kernel32.UpdateProcThreadAttribute.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    kernel32.UpdateProcThreadAttribute.restype = ctypes.c_bool
    kernel32.CreateProcessW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_bool,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_void_p,
        ctypes.POINTER(_PROCESS_INFORMATION),
    ]
    kernel32.CreateProcessW.restype = ctypes.c_bool
    kernel32.WaitForSingleObject.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.TerminateProcess.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    kernel32.TerminateProcess.restype = ctypes.c_bool
    kernel32.GetExitCodeProcess.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    kernel32.GetExitCodeProcess.restype = ctypes.c_bool
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    size = ctypes.c_size_t(0)
    kernel32.InitializeProcThreadAttributeList(
        None, 1, 0, ctypes.byref(size)
    )
    buffer = ctypes.create_string_buffer(size.value)
    if not kernel32.InitializeProcThreadAttributeList(
        buffer, 1, 0, ctypes.byref(size)
    ):
        raise IsolationError("初始化进程属性失败")
    caps = _SECURITY_CAPABILITIES(
        ctypes.c_void_p(sid), None, 0, 0
    )
    attr = 0x00020009  # PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES
    if not kernel32.UpdateProcThreadAttribute(
        buffer,
        0,
        ctypes.c_void_p(attr),
        ctypes.byref(caps),
        ctypes.sizeof(caps),
        None,
        None,
    ):
        raise IsolationError("设置 AppContainer 能力失败")
    startup = _STARTUPINFOEXW()
    startup.StartupInfo.cb = ctypes.sizeof(_STARTUPINFOEXW)
    startup.lpAttributeList = ctypes.cast(buffer, ctypes.c_void_p)
    info = _PROCESS_INFORMATION()
    command = ctypes.create_unicode_buffer(command_line)
    flags = 0x00080000 | 0x08000000
    if not kernel32.CreateProcessW(
        exe,
        command,
        None,
        None,
        False,
        flags,
        None,
        None,
        ctypes.byref(startup),
        ctypes.byref(info),
    ):
        raise IsolationError(
            f"CreateProcessW 失败：{ctypes.get_last_error()}"
        )
    try:
        wait = kernel32.WaitForSingleObject(
            info.hProcess, int(timeout * 1000)
        )
        if wait != 0:
            kernel32.TerminateProcess(info.hProcess, 1)
            raise IsolationError("AppContainer helper 超时")
        code = ctypes.c_uint32()
        kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
        if code.value != 0:
            raise IsolationError(
                f"AppContainer helper 退出码：{code.value}"
            )
    finally:
        kernel32.CloseHandle(info.hThread)
        kernel32.CloseHandle(info.hProcess)


__all__: list[str] = [
    "IS_WINDOWS",
    "IsolationError",
    "assign_job",
    "close_job",
    "create_appcontainer_profile",
    "create_job",
    "delete_appcontainer_profile",
    "low_integrity_supported",
    "set_low_integrity",
    "spawn_in_appcontainer",
]
