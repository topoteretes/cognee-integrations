#!/usr/bin/env python3
"""Cross-platform process helpers for the cognee-memory plugin scripts.

Kept stdlib-only so the detached watchers can import it without pulling in the
rest of the plugin. The plugin decides a session has ended by polling the host
(Claude/Codex) PID; the POSIX ``os.kill(pid, 0)`` liveness probe is unsupported
on Windows, where signal 0 raises ``OSError`` (``WinError 87``), so the check
needs a native Windows path.
"""

import os
import sys

_IS_WINDOWS = sys.platform == "win32"


def pid_alive(pid: int) -> bool:
    """Return ``True`` if a process with ``pid`` is currently running.

    Non-destructive on every platform: it never signals or terminates the
    target. PIDs <= 1 (unknown / init / kernel) are reported not-alive so the
    watchers never bind to them.
    """
    if pid <= 1:
        return False
    if _IS_WINDOWS:
        return _pid_alive_windows(pid)
    return _pid_alive_posix(pid)


def _pid_alive_posix(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists, owned by another user — still alive from our point of view.
        return True
    except Exception:
        # Any other failure (e.g. an out-of-range PID from a corrupt pidfile)
        # counts as not-alive — a liveness probe must never raise into a hook.
        return False
    return True


def _pid_alive_windows(pid: int) -> bool:
    # ``os.kill(pid, 0)`` raises WinError 87 here (signal 0 is unsupported), so
    # probe via a process handle instead. OpenProcess fails with
    # ERROR_ACCESS_DENIED for a live process we may not synchronise on (e.g. a
    # system process); an open handle is non-signalled (WAIT_TIMEOUT) while an
    # exited process signals immediately.
    import ctypes
    from ctypes import wintypes

    SYNCHRONIZE = 0x00100000
    WAIT_TIMEOUT = 0x00000102
    ERROR_ACCESS_DENIED = 5

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        return ctypes.get_last_error() == ERROR_ACCESS_DENIED
    try:
        return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


def find_host_ancestor_windows(start_pid: int, host_stem: str) -> int:
    """Nearest ancestor of ``start_pid`` whose executable base name is ``host_stem``
    (e.g. "claude" / "codex"), found by walking the Windows process tree.

    Returns ``start_pid`` unchanged when the process table cannot be read or no
    matching ancestor is found, so the caller keeps its existing fallback. Used
    where POSIX shells out to ``ps``, which does not exist on Windows.
    """
    return _walk_ancestors(_process_table_windows(), start_pid, host_stem)


def _matches_host_exe(exe: str, host_stem: str) -> bool:
    base = os.path.splitext(exe)[0].casefold()
    stem = host_stem.casefold()
    return base == stem or base.startswith(stem + "-")


def _walk_ancestors(table: dict[int, tuple[int, str]], start_pid: int, host_stem: str) -> int:
    pid = start_pid
    seen: set[int] = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        ppid, exe = table.get(pid, (0, ""))
        if exe and _matches_host_exe(exe, host_stem):
            return pid
        pid = ppid
    return start_pid


def _process_table_windows() -> dict[int, tuple[int, str]]:
    """``{pid: (ppid, exe_basename)}`` for every process via Toolhelp; ``{}`` on failure."""
    import ctypes
    from ctypes import wintypes

    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    MAX_PATH = 260

    class PROCESSENTRY32W(ctypes.Structure):
        # ``th32DefaultHeapID`` is a pointer-sized ULONG_PTR: it must stay
        # pointer-wide so the fields after it keep the right offsets on 64-bit.
        _fields_ = (
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * MAX_PATH),
        )

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
    kernel32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == INVALID_HANDLE_VALUE:
        return {}

    table: dict[int, tuple[int, str]] = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            table[int(entry.th32ProcessID)] = (int(entry.th32ParentProcessID), entry.szExeFile)
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return table
