import socket
import time

import pytest

import mcp_server.gdb_manager as gdb_manager_module
from mcp_server.gdb_manager import GdbServerManager


def test_wait_for_port_returns_true_quickly_when_listening():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("localhost", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        mgr = GdbServerManager()
        start = time.monotonic()
        assert mgr._wait_for_port(port, timeout=2.0) is True
        assert time.monotonic() - start < 1.0  # returns as soon as it's ready, not a fixed wait
    finally:
        srv.close()


def test_wait_for_port_times_out_for_closed_port():
    mgr = GdbServerManager()
    start = time.monotonic()
    assert mgr._wait_for_port(59412, timeout=0.4) is False
    assert time.monotonic() - start >= 0.4


def test_extract_openocd_gdb_port_from_args():
    mgr = GdbServerManager()
    assert mgr._extract_openocd_gdb_port(["-f", "x.cfg", "-c", "gdb_port 3343"], default=3333) == 3343
    assert mgr._extract_openocd_gdb_port(["-f", "x.cfg"], default=3333) == 3333


def test_wait_for_port_returns_false_if_process_died():
    class DeadProc:
        def poll(self):
            return 1  # already exited

    mgr = GdbServerManager()
    mgr.process = DeadProc()
    assert mgr._wait_for_port(59413, timeout=2.0) is False


def test_start_cleans_up_process_when_port_never_opens(monkeypatch):
    class FakeProcess:
        stdout = None

        def __init__(self):
            self.stopped = False

        def poll(self):
            return 0 if self.stopped else None

        def send_signal(self, sig):
            self.stopped = True

        def terminate(self):
            self.stopped = True

        def wait(self, timeout=None):
            self.stopped = True
            return 0

        def kill(self):
            self.stopped = True

    class FakeThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    process = FakeProcess()
    manager = GdbServerManager()

    monkeypatch.setattr(gdb_manager_module.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(gdb_manager_module.threading, "Thread", FakeThread)

    def fail_to_open(port, timeout):
        manager.log_buffer.append("Error: target examination failed")
        return False

    monkeypatch.setattr(manager, "_wait_for_port", fail_to_open)

    with pytest.raises(RuntimeError, match="target examination failed"):
        manager.start("openocd", ["-f", "target/stm32u5x.cfg"])

    assert process.stopped is True
    assert manager.process is None
    assert manager.port is None
