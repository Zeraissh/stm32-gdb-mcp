"""Keep spawned debug processes from outliving the MCP server.

The server spawns a GDB server (OpenOCD / ST-LINK_gdbserver / JLinkGDBServer) and
a GDB, and it is the client -- Claude Code, Codex, an IDE -- that decides when the
server dies. A normal shutdown can be cleaned up in Python; being killed cannot.
When that happens the children are orphaned, keep holding the probe, and the next
session fails with "ST-Link in use" until the probe is physically unplugged.

Two layers, because neither is sufficient alone:

* ``install()`` asks the OS to tie the children's lifetime to ours. On Windows a
  Job Object with KILL_ON_JOB_CLOSE does this even for TerminateProcess, which no
  in-process handler can intercept. On Linux the equivalent per-child guard is
  PR_SET_PDEATHSIG (see ``child_preexec``).
* ``register_shutdown()`` runs a normal Python teardown on exit and on SIGTERM /
  SIGINT, which lets OpenOCD close the SWD link cleanly instead of being killed
  mid-transaction -- a hard kill has been observed to wedge the ST-Link endpoint.
"""

from __future__ import annotations

import atexit
import logging
import os
import signal
import sys
from collections.abc import Callable

logger = logging.getLogger("stm32-gdb-mcp")

# Keeping the job handle alive matters: closing it is what kills the job, so it is
# stored for the process lifetime rather than being garbage-collected.
_job_handle = None
_installed = ""
_shutdown_hooks: list[Callable[[], None]] = []
_shutdown_done = False


def install() -> str:
    """Tie child-process lifetime to this process. Returns what was achieved."""
    global _installed
    if _installed:
        return _installed
    _installed = _install_windows_job() if sys.platform == "win32" else _install_posix()
    logger.info("process guard: %s", _installed)
    return _installed


def _install_windows_job() -> str:
    global _job_handle
    # The guard is a statement, not a caller-side condition, so mypy narrows
    # sys.platform and skips the Windows-only ctypes attributes when checking
    # for other platforms (same idiom as gdb_manager's creationflags).
    if sys.platform != "win32":
        return "unavailable (not windows)"
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        # Declaring these is not optional on 64-bit: ctypes defaults a return
        # value to C int, which truncates a HANDLE and makes
        # AssignProcessToJobObject fail with ERROR_INVALID_HANDLE (6).
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD
        ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                         "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),  # ULONG_PTR
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        JobObjectExtendedLimitInformation = 9

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return f"unavailable (CreateJobObject failed: {ctypes.get_last_error()})"

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            job, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)
        ):
            return f"unavailable (SetInformationJobObject failed: {ctypes.get_last_error()})"

        # Assigning THIS process makes every descendant a job member automatically,
        # so OpenOCD and the GDB that pygdbmi spawns are both covered.
        if not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
            # Already inside a job that forbids nesting: not fatal, the atexit
            # path still covers ordinary shutdowns.
            return f"unavailable (AssignProcessToJobObject failed: {ctypes.get_last_error()})"

        _job_handle = job
        return "windows job object (children die with this process)"
    except Exception as exc:  # noqa: BLE001 - never block startup over cleanup
        return f"unavailable ({type(exc).__name__}: {exc})"


def _install_posix() -> str:
    if hasattr(os, "setsid") or sys.platform.startswith("linux"):
        return "posix (per-child PR_SET_PDEATHSIG via child_preexec)"
    return "unavailable (no supported mechanism)"


def child_preexec():
    """preexec_fn for spawned children: die when the parent dies (Linux only).

    Windows uses the job object instead, and preexec_fn is not supported there.
    """
    if not sys.platform.startswith("linux"):
        return None

    def _set_pdeathsig():
        try:
            import ctypes

            PR_SET_PDEATHSIG = 1
            ctypes.CDLL("libc.so.6").prctl(PR_SET_PDEATHSIG, signal.SIGTERM)
        except Exception:
            pass

    return _set_pdeathsig


def register_shutdown(cleanup: Callable[[], None]) -> None:
    """Run ``cleanup`` at interpreter exit and on SIGTERM/SIGINT.

    Graceful teardown is worth having even with the job object: it lets OpenOCD
    release the SWD link instead of being killed mid-transaction.
    """
    _shutdown_hooks.append(cleanup)
    if len(_shutdown_hooks) > 1:
        return

    atexit.register(_run_shutdown)
    for signame in ("SIGTERM", "SIGINT", "SIGBREAK"):
        sig = getattr(signal, signame, None)
        if sig is None:
            continue
        try:
            previous = signal.getsignal(sig)
            signal.signal(sig, _make_signal_handler(sig, previous))
        except (ValueError, OSError):
            # Not on the main thread, or unsupported on this platform.
            pass


def _make_signal_handler(sig, previous):
    def handler(signum, frame):
        _run_shutdown()
        if callable(previous) and previous not in (signal.SIG_IGN, signal.SIG_DFL):
            previous(signum, frame)
        else:
            signal.signal(sig, signal.SIG_DFL)
            os.kill(os.getpid(), signum)

    return handler


def _run_shutdown():
    global _shutdown_done
    if _shutdown_done:
        return
    _shutdown_done = True
    for cleanup in _shutdown_hooks:
        try:
            cleanup()
        except Exception as exc:  # noqa: BLE001 - shutdown must not raise
            logger.warning("shutdown hook failed: %s", exc)
