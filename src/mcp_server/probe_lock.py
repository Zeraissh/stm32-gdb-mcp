"""Cross-process probe lock: one probe, one GDB server at a time.

A file-based lock in <tempdir>/stm32-gdb-mcp/probe-locks/ prevents two processes
from starting a GDB server for the same debug probe concurrently.  Each lock file
records the locker PID, the spawned server child PID, and the probe key so a
later process can report *who* holds the probe.

The probe key is derived from the GDB-server startup arguments:
  1. ``adapter serial <S>``  →  key = serial value
  2. ``-f interface/<name>.cfg``  →  key = <name> (stlink, cmsis-dap, ...)
  3. fallback → key = server_type (openocd / stlink / jlink)
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time

# ---------------------------------------------------------------------------
# Lock directory & key derivation
# ---------------------------------------------------------------------------

def _lock_dir() -> str:
    return os.path.join(tempfile.gettempdir(), "stm32-gdb-mcp", "probe-locks")


def _sanitize(key: str) -> str:
    """Turn an arbitrary probe key into a safe filename stem."""
    return re.sub(r"[^a-zA-Z0-9_.\-]", "_", key)


def derive_probe_key(server_type: str, args: list[str] | None) -> str:
    """Extract the canonical probe key from *server_type* and *args*.

    Priority (first match wins):
      1. ``adapter serial <S>`` in the args
      2. ``-f interface/<name>.cfg``
      3. *server_type* itself
    """
    if args:
        # 1) adapter serial …
        for i, token in enumerate(args):
            if token == "-c" and i + 1 < len(args):
                m = re.search(r"adapter serial\s+(\S+)", args[i + 1])
                if m:
                    return m.group(1)

        # 2) -f interface/<name>.cfg
        for i, token in enumerate(args):
            if token == "-f" and i + 1 < len(args):
                m = re.match(r"interface/([^/]+)\.cfg", args[i + 1])
                if m:
                    return m.group(1)

    # 3) fallback
    return server_type.lower()


# ---------------------------------------------------------------------------
# Cross-platform PID liveness
# ---------------------------------------------------------------------------

def _pid_alive(pid: int | None) -> bool:
    """Return True when a process with *pid* exists (Windows / POSIX)."""
    if pid is None:
        return False
    if sys.platform == "win32":
        return _pid_alive_windows(pid)
    return _pid_alive_posix(pid)


def _pid_alive_posix(pid: int) -> bool:
    """os.kill(pid, 0) on POSIX: signal 0 = existence check, no signal delivered."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # EPERM means the process EXISTS but belongs to another user — a lock
        # held by it is genuinely held, not stale.
        return True
    except OSError:
        return False
    return True


def _pid_alive_windows(pid: int) -> bool:
    """OpenProcess + GetExitCodeProcess — ``os.kill(pid, 0)`` is a no-op on Windows."""
    # The guard is a statement, not a caller-side condition, so mypy narrows
    # sys.platform and skips the Windows-only ctypes attributes when checking
    # for other platforms (same idiom as process_guard / gdb_manager).
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259

        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False

        exit_code = wintypes.DWORD()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        kernel32.CloseHandle(handle)
        if not ok:
            return False
        return exit_code.value == STILL_ACTIVE
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Lock file I/O
# ---------------------------------------------------------------------------

_LOCK_PAYLOAD_KEYS = {"locker_pid", "child_pid", "probe_key", "created_at"}


def _read_lock_payload(path: str) -> dict | None:
    """Read and validate a lock-file payload.  Returns None when unreadable/corrupt."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return None
        if not _LOCK_PAYLOAD_KEYS.issubset(data.keys()):
            return None
        return data
    except (OSError, json.JSONDecodeError):
        return None


def _write_lock_payload(path: str, payload: dict) -> None:
    """Atomically write *payload* to *path* (best-effort; rename is more atomic
    but the file is already exclusively owned at this point)."""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)  # atomic on most platforms
    except OSError:
        # If rename fails, write directly — we already hold the exclusive fd.
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)


# ---------------------------------------------------------------------------
# ProbeLockError
# ---------------------------------------------------------------------------


class ProbeLockError(RuntimeError):
    """A probe is already locked by another (live) process.

    The message always contains the sub-string ``held by PID`` so
    ``error_taxonomy.classify_error`` can detect it.
    """

    def __init__(self, probe_key: str, holder_pid: int) -> None:
        self.probe_key = probe_key
        self.holder_pid = holder_pid
        super().__init__(
            f"probe '{probe_key}' held by PID {holder_pid}. "
            f"Close the debug session that owns this probe, or kill the zombie "
            f"process (PID {holder_pid}) if it is an orphaned GDB server."
        )


# ---------------------------------------------------------------------------
# ProbeLock – a single acquired lock handle
# ---------------------------------------------------------------------------


class ProbeLock:
    """Opaque handle returned by ``ProbeLockManager.acquire()``.

    Call ``release()`` or use ``ProbeLockManager.release(lock)`` to drop it.
    """

    __slots__ = ("probe_key", "lock_path", "_owned")

    def __init__(self, probe_key: str, lock_path: str) -> None:
        self.probe_key = probe_key
        self.lock_path = lock_path
        self._owned = True

    def release(self) -> None:
        """Delete the lock file (idempotent)."""
        if self._owned:
            try:
                os.unlink(self.lock_path)
            except OSError:
                pass
            self._owned = False


# ---------------------------------------------------------------------------
# ProbeLockManager – global singleton
# ---------------------------------------------------------------------------


class ProbeLockManager:
    """Cross-process lock manager for debug probes.

    One instance per process; typically the module-level ``probe_lock_manager``.
    """

    def __init__(self) -> None:
        # In-process tracking: key → ProbeLock so we can detect same-process
        # double-acquire and also release on stop().
        self._held: dict[str, ProbeLock] = {}

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def acquire(self, server_type: str, args: list[str] | None) -> ProbeLock:
        """Acquire the probe lock for *server_type* + *args*.

        Raises ``ProbeLockError`` when another live process (or this same
        process!) already holds the lock for the same probe key.
        """
        key = derive_probe_key(server_type, args)

        # --- same-process double-acquire -------------------------------------------------
        existing = self._held.get(key)
        if existing is not None and existing._owned:
            # Re-read the payload so the error message uses fresh data.
            existing_payload = _read_lock_payload(existing.lock_path)
            holder = existing_payload["locker_pid"] if existing_payload else os.getpid()
            raise ProbeLockError(key, holder)

        # --- cross-process atomic file creation -----------------------------------------
        os.makedirs(_lock_dir(), exist_ok=True)
        lock_path = os.path.join(_lock_dir(), _sanitize(key) + ".lock")

        fd = self._atomic_create(lock_path, key)
        if fd is None:
            # _atomic_create handles stale lock cleanup internally and only
            # returns None when a live holder exists (it raised already).
            # If we get here the second attempt also failed — re-raise.
            fd = self._atomic_create(lock_path, key)
            if fd is None:
                raise ProbeLockError(key, -1)  # should never reach here

        # Write initial payload (child_pid = null until spawn).
        payload: dict = {
            "locker_pid": os.getpid(),
            "child_pid": None,
            "probe_key": key,
            "created_at": time.time(),
        }
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
        except OSError:
            os.close(fd)
            try:
                os.unlink(lock_path)
            except OSError:
                pass
            raise

        lock = ProbeLock(key, lock_path)
        self._held[key] = lock
        return lock

    def backfill_child_pid(self, lock: ProbeLock, child_pid: int) -> None:
        """Write *child_pid* into an already-held lock file (post-spawn)."""
        if lock is None or not lock._owned:
            return
        try:
            new_payload: dict = {
                "locker_pid": os.getpid(),
                "child_pid": child_pid,
                "probe_key": lock.probe_key,
                "created_at": time.time(),
            }
            _write_lock_payload(lock.lock_path, new_payload)
        except OSError:
            pass

    def release(self, lock: ProbeLock | None) -> None:
        """Release a previously acquired lock (idempotent, safe with None)."""
        if lock is None:
            return
        lock.release()
        self._held.pop(lock.probe_key, None)

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------

    def _atomic_create(self, lock_path: str, key: str) -> int | None:
        """Try O_CREAT|O_EXCL.  On collision check staleness, clean up if dead,
        and return None so the caller retries.  Raises ProbeLockError on
        a live conflict."""
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            return fd
        except FileExistsError:
            self._resolve_conflict(lock_path, key)
            return None

    def _resolve_conflict(self, lock_path: str, key: str) -> None:
        """Read the existing lock file and decide: stale → delete, live → raise."""
        conflict_payload = _read_lock_payload(lock_path)
        if conflict_payload is None:
            # Corrupt / unreadable → treat as stale.
            _remove_lock_file(lock_path)
            return

        locker_pid: int | None = conflict_payload.get("locker_pid")
        child_pid: int | None = conflict_payload.get("child_pid")

        locker_alive = _pid_alive(locker_pid)
        child_alive = _pid_alive(child_pid)

        if locker_alive or child_alive:
            # At least one of the two is still running → genuinely occupied.
            alive_pid: int = locker_pid if locker_alive else child_pid  # type: ignore[assignment]
            raise ProbeLockError(key, alive_pid)

        # Both dead → stale lock, clean it up so the caller can retry.
        _remove_lock_file(lock_path)


def _remove_lock_file(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Module-level singleton — imported by gdb_manager.py
# ---------------------------------------------------------------------------

probe_lock_manager = ProbeLockManager()
