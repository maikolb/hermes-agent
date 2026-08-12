"""Process-wide zero-UI subprocess boundary for native Windows Hermes.

Normal children are delegated to a real base ``pythonw.exe`` runner. The
runner creates the requested process suspended on a private desktop, assigns
it to a kill-on-close Job Object, resumes it, and proxies its exit code. The
single interactive-desktop subprocess capability (cua-driver) uses the same
no-window + pre-resume Job path directly.
"""

from __future__ import annotations

import copy
import ctypes
import inspect
import json
import os
import subprocess
import sys
import tempfile
import threading
from ctypes import wintypes
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"

_SUSPENDED = 0x00000004
_DETACHED = 0x00000008
_NEW_CONSOLE = 0x00000010
_NO_WINDOW = 0x08000000
_BREAKAWAY = 0x01000000
_KILL_ON_CLOSE = 0x00002000
_EXTENDED_LIMIT_INFO = 9
_DESKTOP_ALL_ACCESS = 0x01FF
_INTERACTIVE_CHILD_ENV = "HERMES_INTERNAL_INTERACTIVE_DESKTOP_CHILD"

_install_lock = threading.Lock()
_installed = False
_original_popen = subprocess.Popen
_popen_signature = inspect.signature(_original_popen)
_hidden_desktop_handle = None
_hidden_desktop_name = ""
_runner_path = Path(__file__).with_name("windows_process_runner.py")


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


def _configure_api():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    ntdll.NtResumeProcess.restype = ctypes.c_long
    user32.CreateDesktopW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    ]
    user32.CreateDesktopW.restype = wintypes.HANDLE
    user32.CloseDesktop.argtypes = [wintypes.HANDLE]
    user32.CloseDesktop.restype = wintypes.BOOL
    return kernel32, ntdll, user32


if IS_WINDOWS:
    _kernel32, _ntdll, _user32 = _configure_api()
else:
    _kernel32 = _ntdll = _user32 = None


def hidden_spawn_policy(
    creationflags: int = 0,
    startupinfo: object | None = None,
    *,
    suspend: bool = True,
) -> tuple[int, object | None]:
    """Merge canonical invisible flags without destroying supplied handles."""
    if not IS_WINDOWS:
        return int(creationflags or 0), startupinfo
    flags = int(creationflags or 0)
    flags &= ~(_NEW_CONSOLE | _DETACHED | _BREAKAWAY)
    flags |= _NO_WINDOW
    if suspend:
        flags |= _SUSPENDED
    else:
        flags &= ~_SUSPENDED
    info = copy.copy(startupinfo) if startupinfo is not None else subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = subprocess.SW_HIDE
    return flags, info


def activate_hidden_desktop() -> bool:
    global _hidden_desktop_handle, _hidden_desktop_name
    if not IS_WINDOWS:
        return False
    if _hidden_desktop_handle:
        return False
    name = f"HermesZeroUI_{os.getpid()}"
    handle = _user32.CreateDesktopW(
        name, None, None, 0, _DESKTOP_ALL_ACCESS, None,
    )
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    _hidden_desktop_handle = handle
    _hidden_desktop_name = name
    return True


def hidden_desktop_ready() -> bool:
    return bool(IS_WINDOWS and _hidden_desktop_handle and _hidden_desktop_name)


def interactive_desktop_child_env(
    base_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Mark the capability-owned cua-driver child for interactive inspection."""
    env = dict(base_env if base_env is not None else os.environ)
    env[_INTERACTIVE_CHILD_ENV] = "1"
    return env


def _base_pythonw() -> str:
    candidate = Path(sys.base_prefix) / "pythonw.exe"
    if not candidate.is_file():
        raise RuntimeError(
            f"Hermes zero-UI broker requires the real base pythonw.exe: {candidate}"
        )
    return str(candidate)


def _new_job():
    handle = _kernel32.CreateJobObjectW(None, None)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    info = _ExtendedLimitInfo()
    info.BasicLimitInformation.LimitFlags = _KILL_ON_CLOSE
    if not _kernel32.SetInformationJobObject(
        handle, _EXTENDED_LIMIT_INFO, ctypes.byref(info), ctypes.sizeof(info),
    ):
        error = ctypes.get_last_error()
        _kernel32.CloseHandle(handle)
        raise ctypes.WinError(error)
    return handle


def _command_line(args) -> str:
    if isinstance(args, (str, bytes, os.PathLike)):
        return os.fsdecode(args)
    return subprocess.list2cmdline([os.fsdecode(part) for part in args])


def _shell_command(command_line: str, env: dict[str, str] | None) -> tuple[str, str]:
    source_env = env if env is not None else os.environ
    comspec = source_env.get("ComSpec") or source_env.get("COMSPEC")
    if not comspec:
        system_root = source_env.get("SystemRoot") or os.environ.get("SystemRoot")
        if not system_root:
            raise RuntimeError("Cannot resolve the Windows command processor")
        comspec = str(Path(system_root) / "System32" / "cmd.exe")
    return comspec, f'{subprocess.list2cmdline([comspec])} /c "{command_line}"'


def _write_payload(payload: dict) -> str:
    cache_dir = Path(tempfile.gettempdir()) / "hermes-zero-ui"
    cache_dir.mkdir(parents=True, exist_ok=True)
    fd, raw_path = tempfile.mkstemp(prefix="spawn-", suffix=".json", dir=cache_dir)
    path = Path(raw_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False)
        return str(path)
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise


class WindowsHiddenPopen(_original_popen):
    """Canonical Windows Popen: brokered by default, direct only for cua-driver."""

    def __init__(self, *args, **kwargs):
        bound = _popen_signature.bind(*args, **kwargs)
        params = dict(bound.arguments)
        target_args = params.pop("args")
        target_executable = params.pop("executable", None)
        target_shell = bool(params.pop("shell", False))
        target_flags = int(params.pop("creationflags", 0) or 0)
        target_startup = params.pop("startupinfo", None)

        child_env = params.get("env")
        interactive_child = bool(
            isinstance(child_env, dict)
            and child_env.get(_INTERACTIVE_CHILD_ENV) == "1"
        )
        if interactive_child:
            child_env = dict(child_env)
            child_env.pop(_INTERACTIVE_CHILD_ENV, None)
            params["env"] = child_env
            flags, startup = hidden_spawn_policy(target_flags, target_startup, suspend=True)
            self._hermes_job_handle = None
            _original_popen.__init__(
                self,
                target_args,
                executable=target_executable,
                shell=target_shell,
                creationflags=flags,
                startupinfo=startup,
                **params,
            )
            job = None
            process_handle = wintypes.HANDLE(int(self._handle))
            try:
                job = _new_job()
                if not _kernel32.AssignProcessToJobObject(job, process_handle):
                    raise ctypes.WinError(ctypes.get_last_error())
                self._hermes_job_handle = job
                status = int(_ntdll.NtResumeProcess(process_handle))
                if status < 0:
                    raise OSError(
                        f"NtResumeProcess failed with NTSTATUS 0x{status & 0xFFFFFFFF:08X}"
                    )
            except BaseException:
                try:
                    if job:
                        _kernel32.TerminateJobObject(job, 1)
                    else:
                        _original_popen.kill(self)
                finally:
                    if job:
                        _kernel32.CloseHandle(job)
                raise
            self.args = target_args
            return

        command_line = _command_line(target_args)
        if target_shell:
            target_executable, command_line = _shell_command(command_line, child_env)
        elif target_executable is not None:
            target_executable = os.fsdecode(target_executable)

        payload_path = _write_payload({
            "command_line": command_line,
            "executable": target_executable,
            "creationflags": target_flags,
            "desktop": _hidden_desktop_name,
        })
        runner_flags, runner_startup = hidden_spawn_policy(0, target_startup, suspend=False)
        self._hermes_job_handle = None
        try:
            _original_popen.__init__(
                self,
                [_base_pythonw(), str(_runner_path), payload_path],
                executable=_base_pythonw(),
                shell=False,
                creationflags=runner_flags,
                startupinfo=runner_startup,
                **params,
            )
        except BaseException:
            try:
                Path(payload_path).unlink()
            except OSError:
                pass
            raise
        self.args = target_args

    def _close_job(self) -> None:
        job = getattr(self, "_hermes_job_handle", None)
        if job:
            self._hermes_job_handle = None
            _kernel32.CloseHandle(job)

    def poll(self):
        result = _original_popen.poll(self)
        if result is not None:
            self._close_job()
        return result

    def wait(self, timeout=None):
        result = _original_popen.wait(self, timeout=timeout)
        self._close_job()
        return result

    def terminate(self):
        job = getattr(self, "_hermes_job_handle", None)
        if job:
            if not _kernel32.TerminateJobObject(job, 1):
                raise ctypes.WinError(ctypes.get_last_error())
            return
        return _original_popen.terminate(self)

    def kill(self):
        return self.terminate()

    def __del__(self):
        try:
            _original_popen.__del__(self)
        finally:
            self._close_job()


def install_windows_process_broker() -> bool:
    global _installed
    if not IS_WINDOWS:
        return False
    with _install_lock:
        activate_hidden_desktop()
        _base_pythonw()
        if _installed or subprocess.Popen is WindowsHiddenPopen:
            _installed = True
            return False
        subprocess.Popen = WindowsHiddenPopen
        _installed = True
        return True


def broker_installed() -> bool:
    return bool(
        IS_WINDOWS
        and hidden_desktop_ready()
        and subprocess.Popen is WindowsHiddenPopen
    )
