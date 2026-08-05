"""Tests for cross-process probe locking (probe_lock.py).

No real hardware, no real OpenOCD — all scenarios use subprocess.Popen with a
Python sleep stub, manual lock-file fabrication, and monkeypatching.
"""

import json
import os
import subprocess
import sys
import tempfile
import time

import pytest

from mcp_server.error_taxonomy import classify_error
from mcp_server.gdb_manager import GdbServerManager
from mcp_server.probe_lock import (
    ProbeLockError,
    ProbeLockManager,
    _sanitize,
    derive_probe_key,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _sleep_child():
    """Spawn a short-lived Python child that just sleeps.  Returns the Popen object."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _dead_pid() -> int:
    """Spawn, kill, wait — return a PID that is guaranteed dead."""
    p = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pid = p.pid
    p.terminate()
    p.wait()
    return pid


def _unique_key(suffix: str = "") -> str:
    """Return a probe key unique to this test run (based on PID + suffix)."""
    return f"test-{os.getpid()}-{suffix}"


def _lock_path_for_key(key: str) -> str:
    return os.path.join(
        tempfile.gettempdir(), "stm32-gdb-mcp", "probe-locks",
        _sanitize(key) + ".lock",
    )


def _write_lock_file(key: str, locker_pid: int, child_pid: int | None = None) -> str:
    """Write a lock file by hand and return its path."""
    lock_dir = os.path.join(tempfile.gettempdir(), "stm32-gdb-mcp", "probe-locks")
    os.makedirs(lock_dir, exist_ok=True)
    path = os.path.join(lock_dir, _sanitize(key) + ".lock")
    payload = {
        "locker_pid": locker_pid,
        "child_pid": child_pid,
        "probe_key": key,
        "created_at": time.time(),
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    return path


def _cleanup_lock(key: str) -> None:
    """Delete the lock file for *key* if it exists."""
    path = _lock_path_for_key(key)
    try:
        os.unlink(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Fixture: fresh ProbeLockManager per test so in-process state is isolated.
# ---------------------------------------------------------------------------


@pytest.fixture
def lock_mgr() -> ProbeLockManager:
    mgr = ProbeLockManager()
    yield mgr
    # Release any locks still held by this manager.
    for key in list(mgr._held):
        mgr.release(mgr._held[key])


# ---------------------------------------------------------------------------
# (a) acquire / release lifecycle — release then re-acquire
# ---------------------------------------------------------------------------


def test_acquire_release_reacquire(lock_mgr: ProbeLockManager):
    key_suffix = "lifecycle"
    key = _unique_key(key_suffix)
    try:
        lock1 = lock_mgr.acquire("openocd", ["-f", f"interface/{key}.cfg"])
        assert lock1._owned
        assert os.path.exists(lock1.lock_path)

        lock_mgr.release(lock1)
        assert not lock1._owned
        assert not os.path.exists(lock1.lock_path)

        # Re-acquire after release must succeed.
        lock2 = lock_mgr.acquire("openocd", ["-f", f"interface/{key}.cfg"])
        assert lock2._owned
        lock_mgr.release(lock2)
    finally:
        _cleanup_lock(key)


# ---------------------------------------------------------------------------
# (b) live-PID conflict — a real child process is the holder
# ---------------------------------------------------------------------------


def test_live_pid_conflict_with_real_child(lock_mgr: ProbeLockManager):
    child = _sleep_child()
    try:
        key = _unique_key("live-conflict")
        _write_lock_file(key, locker_pid=child.pid)

        with pytest.raises(ProbeLockError) as exc_info:
            lock_mgr.acquire("openocd", ["-f", f"interface/{key}.cfg"])

        msg = str(exc_info.value)
        assert f"held by PID {child.pid}" in msg
        assert f"probe '{key}'" in msg
    finally:
        child.terminate()
        child.wait()
        _cleanup_lock(key)


# ---------------------------------------------------------------------------
# (c) locker and child both dead → stale lock cleared, acquire succeeds
# ---------------------------------------------------------------------------


def test_both_dead_stale_lock_cleared(lock_mgr: ProbeLockManager):
    dead = _dead_pid()
    key = _unique_key("both-dead")
    _write_lock_file(key, locker_pid=dead, child_pid=dead)

    try:
        lock = lock_mgr.acquire("openocd", ["-f", f"interface/{key}.cfg"])
        assert lock._owned
        lock_mgr.release(lock)
    finally:
        _cleanup_lock(key)


# ---------------------------------------------------------------------------
# (d) locker dead, child alive → still occupied (zombie openocd scenario)
# ---------------------------------------------------------------------------


def test_locker_dead_child_alive_still_occupied(lock_mgr: ProbeLockManager):
    child = _sleep_child()
    dead = _dead_pid()
    try:
        key = _unique_key("zombie")
        _write_lock_file(key, locker_pid=dead, child_pid=child.pid)

        with pytest.raises(ProbeLockError) as exc_info:
            lock_mgr.acquire("openocd", ["-f", f"interface/{key}.cfg"])

        msg = str(exc_info.value)
        # The error must reference the ALIVE PID (the child).
        assert f"held by PID {child.pid}" in msg
    finally:
        child.terminate()
        child.wait()
        _cleanup_lock(key)


# ---------------------------------------------------------------------------
# (e) same-process double-acquire → conflict
# ---------------------------------------------------------------------------


def test_same_process_double_acquire_conflict(lock_mgr: ProbeLockManager):
    key = _unique_key("double")
    try:
        lock1 = lock_mgr.acquire("openocd", ["-f", f"interface/{key}.cfg"])
        with pytest.raises(ProbeLockError) as exc_info:
            lock_mgr.acquire("openocd", ["-f", f"interface/{key}.cfg"])

        msg = str(exc_info.value)
        assert "held by PID" in msg
        lock_mgr.release(lock1)
    finally:
        _cleanup_lock(key)


# ---------------------------------------------------------------------------
# (f) probe-key derivation: serial > interface cfg > server_type
# ---------------------------------------------------------------------------


def test_derive_key_serial_priority():
    key = derive_probe_key("openocd", ["-f", "interface/stlink.cfg", "-c", "adapter serial 066DFF575051777267123456"])
    assert key == "066DFF575051777267123456"


def test_derive_key_interface_cfg_fallback():
    key = derive_probe_key("openocd", ["-f", "interface/cmsis-dap.cfg", "-f", "target/stm32l4x.cfg"])
    assert key == "cmsis-dap"


def test_derive_key_server_type_fallback():
    key = derive_probe_key("stlink", ["-p", "4242"])
    assert key == "stlink"

    key2 = derive_probe_key("JLiNK", None)
    assert key2 == "jlink"


def test_derive_key_no_args():
    key = derive_probe_key("openocd", None)
    assert key == "openocd"


# ---------------------------------------------------------------------------
# (g) start() failure path releases the lock
# ---------------------------------------------------------------------------


def test_start_failure_releases_lock(monkeypatch):
    """When the spawned GDB server exits immediately, start() must release the lock."""
    key = _unique_key("fail-release")

    class FakeProcess:
        stdout = None

        def __init__(self):
            self._exited = False

        def poll(self):
            return 0 if self._exited else None

        def send_signal(self, sig):
            pass

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    class FakeThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    import mcp_server.gdb_manager as gdb_mod

    process = FakeProcess()

    monkeypatch.setattr(gdb_mod.subprocess, "Popen", lambda *a, **kw: process)
    monkeypatch.setattr(gdb_mod.threading, "Thread", FakeThread)
    monkeypatch.setattr(gdb_mod, "_port_accepts", lambda port, timeout=0.2: False)

    manager = GdbServerManager()

    def fail_port(port, timeout):
        process._exited = True
        manager.log_buffer.append("Error: init mode failed")
        return False

    monkeypatch.setattr(manager, "_wait_for_port", fail_port)

    try:
        with pytest.raises(RuntimeError, match="init mode failed"):
            manager.start("openocd", ["-f", f"interface/{key}.cfg"])
    finally:
        _cleanup_lock(key)

    # After failure the lock file must NOT exist.
    lock_path = _lock_path_for_key(key)
    assert not os.path.exists(lock_path), f"Lock file {lock_path} leaked after start failure"


# ---------------------------------------------------------------------------
# (h) error message contains exact substring "held by PID"
# ---------------------------------------------------------------------------


def test_error_message_contains_held_by_pid_exact_substring(lock_mgr: ProbeLockManager):
    child = _sleep_child()
    try:
        key = _unique_key("msg-check")
        _write_lock_file(key, locker_pid=child.pid)

        with pytest.raises(ProbeLockError) as exc_info:
            lock_mgr.acquire("openocd", ["-f", f"interface/{key}.cfg"])

        # The exact substring must be present in the raw exception.
        assert "held by PID" in str(exc_info.value)
    finally:
        child.terminate()
        child.wait()
        _cleanup_lock(key)


# ---------------------------------------------------------------------------
# (i) probe_locked classification is retryable=false and matches "held by PID"
# ---------------------------------------------------------------------------


def test_probe_locked_classification_retryable_false():
    result = classify_error("probe 'stlink' held by PID 12345. Close the debug session...")
    assert result["code"] == "probe_locked"
    assert result["retryable"] is False
    assert "list_sessions" in result["suggested_next_actions"]
    assert "stop_debug_session" in result["suggested_next_actions"]


def test_probe_locked_matches_lowercase_variation():
    """classify_error lowercases the message, so 'held by pid' must match."""
    result = classify_error("Probe 'JLINK' HELD BY PID 99999")
    assert result["code"] == "probe_locked"
    assert result["retryable"] is False


# ---------------------------------------------------------------------------
# Additional: stop() releases the lock (integration with GdbServerManager)
# ---------------------------------------------------------------------------


def test_stop_releases_lock(monkeypatch):
    """A successful start + stop cycle must release the probe lock."""
    key = _unique_key("stop-release")

    class FakeProcess:
        stdout = None

        def __init__(self):
            self._exited = False

        def poll(self):
            return 0 if self._exited else None

        def send_signal(self, sig):
            pass

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    class FakeThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    import mcp_server.gdb_manager as gdb_mod

    process = FakeProcess()

    monkeypatch.setattr(gdb_mod.subprocess, "Popen", lambda *a, **kw: process)
    monkeypatch.setattr(gdb_mod.threading, "Thread", FakeThread)
    monkeypatch.setattr(gdb_mod, "_port_accepts", lambda port, timeout=0.2: False)

    manager = GdbServerManager()

    def port_ready(port, timeout):
        return True

    monkeypatch.setattr(manager, "_wait_for_port", port_ready)

    try:
        manager.start("openocd", ["-f", f"interface/{key}.cfg"])
        lock_path = _lock_path_for_key(key)
        assert os.path.exists(lock_path), "Lock file must exist after successful start"

        manager.stop()
        assert not os.path.exists(lock_path), "Lock file must be removed after stop()"
    finally:
        _cleanup_lock(key)


# ---------------------------------------------------------------------------
# Additional: adopted path does NOT take a lock
# ---------------------------------------------------------------------------


def test_adopted_path_does_not_take_lock(monkeypatch):
    """When a server is already listening (adopted), no probe lock is acquired."""
    import socket

    import mcp_server.gdb_manager as gdb_mod

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(50)
    port = srv.getsockname()[1]

    spawned = []
    monkeypatch.setattr(gdb_mod.subprocess, "Popen", lambda *a, **k: spawned.append(a) or None)

    try:
        manager = GdbServerManager()
        got = manager.start("openocd", ["-c", f"gdb_port {port}"])
        assert got == port
        assert manager.adopted is True
        assert manager._probe_lock is None, "adopted path must not acquire a probe lock"
        assert spawned == []
    finally:
        srv.close()
