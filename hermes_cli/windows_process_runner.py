"""Native Windows runner used by the process-wide Hermes subprocess broker.

This module is launched with the real base ``pythonw.exe``. It reads one
user-private payload, deletes it, creates the requested process suspended and
windowless on a private desktop, assigns it to a kill-on-close Job Object,
resumes it, atomically publishes the real target PID, waits, and proxies its
exit code. Standard handles are inherited from the runner, so the parent Popen
keeps normal stdin/stdout/stderr semantics.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
from ctypes import wintypes
from pathlib import Path

_CREATE_SUSPENDED = 0x00000004
_DETACHED = 0x00000008
_NEW_CONSOLE = 0x00000010
_NO_WINDOW = 0x08000000
_KILL_ON_CLOSE = 0x00002000
_EXTENDED_LIMIT_INFO = 9
_STARTF_USESHOWWINDOW = 0x00000001
_STARTF_USESTDHANDLES = 0x00000100
_SW_HIDE = 0
_INFINITE = 0xFFFFFFFF


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wintypes.BOOL),
    ]


class _StartupInfo(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_ubyte)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class _ProcessInformation(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


class _IOCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _BasicLimitInfo(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _ExtendedLimitInfo(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInfo),
        ("IoInfo", _IOCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _api():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateProcessW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.POINTER(_SecurityAttributes),
        ctypes.POINTER(_SecurityAttributes),
        wintypes.BOOL,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(_StartupInfo),
        ctypes.POINTER(_ProcessInformation),
    ]
    k.CreateProcessW.restype = wintypes.BOOL
    k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k.CreateJobObjectW.restype = wintypes.HANDLE
    k.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    k.SetInformationJobObject.restype = wintypes.BOOL
    k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k.AssignProcessToJobObject.restype = wintypes.BOOL
    k.ResumeThread.argtypes = [wintypes.HANDLE]
    k.ResumeThread.restype = wintypes.DWORD
    k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k.WaitForSingleObject.restype = wintypes.DWORD
    k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k.GetExitCodeProcess.restype = wintypes.BOOL
    k.GetStdHandle.argtypes = [wintypes.DWORD]
    k.GetStdHandle.restype = wintypes.HANDLE
    k.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k.TerminateJobObject.restype = wintypes.BOOL
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.CloseHandle.restype = wintypes.BOOL
    k.ExitProcess.argtypes = [wintypes.UINT]
    k.ExitProcess.restype = None
    return k


def _new_job(k):
    job = k.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    info = _ExtendedLimitInfo()
    info.BasicLimitInformation.LimitFlags = _KILL_ON_CLOSE
    if not k.SetInformationJobObject(
        job, _EXTENDED_LIMIT_INFO, ctypes.byref(info), ctypes.sizeof(info),
    ):
        error = ctypes.get_last_error()
        k.CloseHandle(job)
        raise ctypes.WinError(error)
    return job


def _write_handshake(path: Path, payload: dict) -> None:
    temp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temp_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(temp_path, path)


def _run(payload_path: str) -> int:
    payload_file = Path(payload_path)
    try:
        payload = json.loads(payload_file.read_text(encoding="utf-8"))
    finally:
        try:
            payload_file.unlink()
        except OSError:
            pass

    k = _api()
    startup = _StartupInfo()
    startup.cb = ctypes.sizeof(startup)
    startup.lpDesktop = payload["desktop"]
    startup.dwFlags = _STARTF_USESHOWWINDOW | _STARTF_USESTDHANDLES
    startup.wShowWindow = _SW_HIDE
    startup.hStdInput = k.GetStdHandle(-10)
    startup.hStdOutput = k.GetStdHandle(-11)
    startup.hStdError = k.GetStdHandle(-12)

    flags = int(payload.get("creationflags", 0))
    flags &= ~(_NEW_CONSOLE | _DETACHED)
    flags |= _NO_WINDOW | _CREATE_SUSPENDED

    command_line = ctypes.create_unicode_buffer(payload["command_line"])
    process = _ProcessInformation()
    job = None
    try:
        if not k.CreateProcessW(
            payload.get("executable"),
            command_line,
            None,
            None,
            True,
            flags,
            None,
            None,
            ctypes.byref(startup),
            ctypes.byref(process),
        ):
            raise ctypes.WinError(ctypes.get_last_error())

        job = _new_job(k)
        if not k.AssignProcessToJobObject(job, process.hProcess):
            raise ctypes.WinError(ctypes.get_last_error())
        if k.ResumeThread(process.hThread) == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        k.CloseHandle(process.hThread)
        process.hThread = None
        _write_handshake(
            Path(payload["status_path"]),
            {"child_pid": int(process.dwProcessId)},
        )

        wait_result = k.WaitForSingleObject(process.hProcess, _INFINITE)
        if wait_result != 0:
            raise ctypes.WinError(ctypes.get_last_error())
        exit_code = wintypes.DWORD()
        if not k.GetExitCodeProcess(process.hProcess, ctypes.byref(exit_code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(exit_code.value)
    except BaseException:
        if job:
            k.TerminateJobObject(job, 1)
        raise
    finally:
        if process.hThread:
            k.CloseHandle(process.hThread)
        if process.hProcess:
            k.CloseHandle(process.hProcess)
        if job:
            k.CloseHandle(job)


def main() -> None:
    exit_code = 1
    try:
        if len(sys.argv) != 2:
            raise ValueError("expected one broker payload path")
        exit_code = _run(sys.argv[1])
    finally:
        _api().ExitProcess(exit_code)


if __name__ == "__main__":
    main()
