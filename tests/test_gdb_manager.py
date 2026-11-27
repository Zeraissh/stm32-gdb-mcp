import os
import socket
import threading
import time

import pytest

import mcp_server.gdb_manager as gdb_manager_module
from mcp_server.gdb_manager import (
    GdbServerManager,
    PortInUseError,
    compare_openocd_identity,
    control_ports,
    openocd_config_files,
)


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
    # This exercises the spawn path, so the default port must read as free
    # regardless of what the developer's machine happens to be running.
    monkeypatch.setattr(gdb_manager_module, "_port_accepts", lambda port, timeout=0.2: False)

    with pytest.raises(RuntimeError, match="target examination failed"):
        manager.start("openocd", ["-f", "target/stm32u5x.cfg"])

    assert process.stopped is True
    assert manager.process is None
    assert manager.port is None


def _listening_socket():
    """A stand-in for an already-running GDB server on an ephemeral port.

    The backlog has to be generous: nothing ever accept()s these connections, so
    every liveness probe leaves one queued, and a backlog of 1 makes the second
    probe hang instead of connecting.
    """
    import socket as _s
    srv = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
    srv.setsockopt(_s.SOL_SOCKET, _s.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(50)
    return srv, srv.getsockname()[1]


def test_start_adopts_a_server_already_listening_instead_of_spawning(monkeypatch):
    # Spawning a second server on a taken port cannot work, and the port probe can
    # see the OLD server answer and call the start a success.
    from mcp_server.gdb_manager import GdbServerManager

    srv, port = _listening_socket()
    try:
        manager = GdbServerManager()
        spawned = []
        monkeypatch.setattr("subprocess.Popen", lambda *a, **k: spawned.append(a) or None)

        got = manager.start("openocd", ["-c", f"gdb_port {port}"])

        assert got == port
        assert manager.adopted is True
        assert manager.process is None
        assert spawned == [], "must not spawn a second server"
        assert manager.is_alive() is True
    finally:
        srv.close()


def test_stop_never_kills_an_adopted_server(monkeypatch):
    from mcp_server.gdb_manager import GdbServerManager

    srv, port = _listening_socket()
    try:
        manager = GdbServerManager()
        monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("must not spawn"))
        manager.start("openocd", ["-c", f"gdb_port {port}"])

        manager.stop()

        # The foreign server is still up: we only dropped our reference to it.
        assert manager.adopted is False
        import socket as _s
        with _s.create_connection(("localhost", port), timeout=1.0):
            pass
    finally:
        srv.close()


def test_adopted_server_reports_dead_once_its_port_closes(monkeypatch):
    from mcp_server.gdb_manager import GdbServerManager

    srv, port = _listening_socket()
    manager = GdbServerManager()
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("must not spawn"))
    manager.start("openocd", ["-c", f"gdb_port {port}"])
    assert manager.is_alive() is True

    srv.close()

    assert manager.is_alive() is False


def test_start_refuses_to_adopt_when_told_not_to(monkeypatch):
    # Issue #47: explicit server_args mean "start it this way"; adopting a server
    # already on the port would silently discard them.
    from mcp_server.gdb_manager import GdbServerManager, PortInUseError

    srv, port = _listening_socket()
    try:
        manager = GdbServerManager()
        monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("must not spawn"))

        with pytest.raises(PortInUseError, match=f"port {port}"):
            manager.start("openocd", ["-c", f"gdb_port {port}"], adopt=False)

        assert manager.adopted is False
        assert manager.port is None
        assert manager.is_alive() is False
    finally:
        srv.close()


def test_stop_records_the_port_of_the_server_it_spawned(monkeypatch):
    from mcp_server.gdb_manager import GdbServerManager

    class Proc:
        def poll(self):
            return 0

    manager = GdbServerManager()
    manager.process, manager.port = Proc(), 3353
    manager.stop()

    assert manager.port is None
    assert manager.last_stopped_port == 3353


def test_openocd_config_files_normalize_interface_and_target():
    args = ["-f", "/opt/openocd/scripts/interface/stlink.cfg", "-c", "gdb_port 3333",
            "-f", r"target\stm32l4x.cfg"]
    assert openocd_config_files(args) == {
        "interface": "interface/stlink.cfg",
        "target": "target/stm32l4x.cfg",
    }
    same = compare_openocd_identity(args, ["openocd", "-f", "interface/stlink.cfg", "-f", "target/stm32l4x.cfg"])
    assert same["match"] is True
    different = compare_openocd_identity(
        args, ["openocd", "-f", "interface/cmsis-dap.cfg", "-f", "target/stm32l1.cfg"],
    )
    assert different["match"] is False
    assert compare_openocd_identity(args, None)["match"] is None
    assert compare_openocd_identity(["-c", "gdb_port 3333"], ["openocd"])["match"] is None


def test_control_ports_do_not_guess_defaults_for_a_custom_gdb_port():
    assert control_ports(["-c", "gdb_port 3353"], 3353) == []
    assert control_ports(["-c", "tcl_port 7777", "-c", "gdb_port 3353"], 3353) == [("tcl", 7777)]
    assert ("tcl", 6666) in control_ports([], 3333)
    assert ("telnet", 4444) in control_ports([], 3333)
    disabled = control_ports(["-c", "telnet_port disabled", "-c", "tcl_port disabled"], 3333)
    assert disabled == []


def test_spawned_server_whose_process_died_reports_the_port_mismatch(monkeypatch):
    # Issue #77: is_alive() used to trust only the process handle, so a live
    # OpenOCD on the session port (a wrapper that exited, the dap case) was
    # reported dead.
    class Dead:
        def poll(self):
            return 0

    srv, port = _listening_socket()
    try:
        manager = GdbServerManager()
        manager.process = Dead()
        manager.port = port
        manager.server_type = "openocd"

        info = manager.liveness()
        assert info["alive"] is True
        assert info["process_alive"] is False
        assert info["port_listening"] is True
        assert info["mismatch"] == "process_gone_port_listening"
        assert manager.is_alive() is True
    finally:
        srv.close()


def test_a_live_process_is_not_probed_on_its_gdb_port(monkeypatch):
    class Live:
        def poll(self):
            return None

    manager = GdbServerManager()
    manager.process = Live()
    manager.port = 3333

    def explode(port, timeout=0.2):
        raise AssertionError("must not connect to a live server's gdb port")

    monkeypatch.setattr(gdb_manager_module, "_port_accepts", explode)
    info = manager.liveness()
    assert info == {
        "alive": True,
        "process_alive": True,
        "port_listening": None,
        "mismatch": None,
    }


def test_retry_refuses_its_own_leftover_instead_of_adopting_it(monkeypatch):
    # Issue #77: attempt 2 used to forget the port attempt 1 spawned. With no
    # explicit args it then adopted that leftover; with explicit args it claimed
    # "this session did not start it", and either way the port memory was gone.
    srv, port = _listening_socket()
    try:
        monkeypatch.setattr(gdb_manager_module, "listener_argv", lambda _port: None)
        monkeypatch.setattr(gdb_manager_module.subprocess, "Popen",
                            lambda *a, **k: pytest.fail("must not spawn"))
        args = ["-c", f"gdb_port {port}"]
        for adopt in (True, False):
            manager = GdbServerManager()
            manager.preserve_stopped_port = True
            manager.refuse_own_leftover = True
            manager.last_stopped_port = port
            with pytest.raises(PortInUseError, match="this session started") as exc:
                manager.start("openocd", args, adopt=adopt)
            assert "did not start it" not in str(exc.value)
            assert manager.adopted is False
            assert manager.last_stopped_port == port
    finally:
        srv.close()


def test_a_stranger_that_reused_the_port_is_not_called_our_leftover(monkeypatch):
    srv, port = _listening_socket()
    try:
        manager = GdbServerManager()
        manager.preserve_stopped_port = True
        manager.refuse_own_leftover = True
        manager.last_stopped_port = port
        manager.adopt_refuse_reason = "debug-profile server_args"
        monkeypatch.setattr(gdb_manager_module, "listener_argv", lambda _port: [
            "openocd", "-f", "interface/cmsis-dap.cfg", "-f", "target/stm32l1.cfg",
        ])
        monkeypatch.setattr(gdb_manager_module.subprocess, "Popen",
                            lambda *a, **k: pytest.fail("must not spawn"))
        args = ["-f", "interface/stlink.cfg", "-f", "target/stm32l4x.cfg", "-c", f"gdb_port {port}"]
        with pytest.raises(PortInUseError, match="did not start it") as exc:
            manager.start("openocd", args, adopt=False)
        text = str(exc.value)
        assert "this session started" not in text
        assert "debug-profile server_args" in text
        assert "interface/cmsis-dap.cfg" in text
        assert "target/stm32l4x.cfg" in text
        assert "explicit server_args" not in text
    finally:
        srv.close()


def test_adopt_refuses_a_listener_whose_interface_does_not_match(monkeypatch):
    srv, port = _listening_socket()
    try:
        monkeypatch.setattr(gdb_manager_module, "listener_argv", lambda _port: [
            "openocd", "-f", "interface/cmsis-dap.cfg", "-f", "target/stm32l1.cfg",
        ])
        monkeypatch.setattr(gdb_manager_module.subprocess, "Popen",
                            lambda *a, **k: pytest.fail("must not spawn"))
        manager = GdbServerManager()
        args = ["-f", "interface/stlink.cfg", "-f", "target/stm32l4x.cfg", "-c", f"gdb_port {port}"]
        with pytest.raises(PortInUseError, match="do not match"):
            manager.start("openocd", args)  # adopt=True, the recover path
        assert manager.adopted is False
    finally:
        srv.close()


def test_adopt_still_attaches_when_the_listener_matches_or_cannot_be_identified(monkeypatch):
    srv, port = _listening_socket()
    try:
        monkeypatch.setattr(gdb_manager_module.subprocess, "Popen",
                            lambda *a, **k: pytest.fail("must not spawn"))
        args = ["-f", "interface/stlink.cfg", "-f", "target/stm32l4x.cfg", "-c", f"gdb_port {port}"]

        monkeypatch.setattr(gdb_manager_module, "listener_argv", lambda _port: [
            "openocd", "-f", "interface/stlink.cfg", "-f", "target/stm32l4x.cfg",
        ])
        matched = GdbServerManager()
        assert matched.start("openocd", args) == port
        assert matched.adopted is True

        monkeypatch.setattr(gdb_manager_module, "listener_argv", lambda _port: None)
        unknown = GdbServerManager()
        assert unknown.start("openocd", args) == port
        assert unknown.adopted is True
    finally:
        srv.close()


def test_stop_adopted_shuts_the_server_down_over_tcl_and_checks_the_port(monkeypatch):
    gdb_srv, gdb_port = _listening_socket()
    tcl_srv, tcl_port = _listening_socket()
    got = {}

    def serve():
        conn, _ = tcl_srv.accept()
        with conn:
            header = b""
            while len(header) < 4:
                chunk = conn.recv(4 - len(header))
                if not chunk:
                    return
                header += chunk
            length = int.from_bytes(header, "big")
            body = b""
            while len(body) < length:
                chunk = conn.recv(length - len(body))
                if not chunk:
                    return
                body += chunk
            got["cmd"] = body
            gdb_srv.close()
        tcl_srv.close()

    threading.Thread(target=serve, daemon=True).start()
    # Same process owns both sockets, which is what the safety check requires.
    monkeypatch.setattr(gdb_manager_module, "_listening_pids", lambda port: [os.getpid()])
    try:
        manager = GdbServerManager()
        monkeypatch.setattr(gdb_manager_module.subprocess, "Popen",
                            lambda *a, **k: pytest.fail("must not spawn"))
        manager.start("openocd", ["-c", f"gdb_port {gdb_port}", "-c", f"tcl_port {tcl_port}"])
        assert manager.adopted is True

        manager.stop(stop_adopted=True)

        assert got["cmd"] == b"shutdown"
        result = manager.last_stop_result
        assert result["shutdown_via"] == "tcl"
        assert result["port_free"] is True
        assert result["was_adopted"] is True
        assert manager.adopted is False
        assert gdb_manager_module._port_accepts(gdb_port) is False
    finally:
        gdb_srv.close()
        tcl_srv.close()


def test_stop_adopted_uses_telnet_when_that_is_the_servers_control_port(monkeypatch):
    gdb_srv, gdb_port = _listening_socket()
    telnet_srv, telnet_port = _listening_socket()
    got = {}

    def serve():
        conn, _ = telnet_srv.accept()
        with conn:
            conn.sendall(b"Open On-Chip Debugger\r\n> ")
            data = b""
            while b"\n" not in data:
                chunk = conn.recv(64)
                if not chunk:
                    break
                data += chunk
            got["cmd"] = data
            gdb_srv.close()
        telnet_srv.close()

    threading.Thread(target=serve, daemon=True).start()

    def pids(port):
        if port in (gdb_port, telnet_port):
            return [os.getpid()]
        return []

    monkeypatch.setattr(gdb_manager_module, "_listening_pids", pids)
    try:
        manager = GdbServerManager()
        monkeypatch.setattr(gdb_manager_module.subprocess, "Popen",
                            lambda *a, **k: pytest.fail("must not spawn"))
        manager.start("openocd", ["-c", f"gdb_port {gdb_port}", "-c", f"telnet_port {telnet_port}"])
        manager.stop(stop_adopted=True)
        assert got["cmd"].startswith(b"shutdown")
        assert manager.last_stop_result["shutdown_via"] == "telnet"
        assert manager.last_stop_result["port_free"] is True
        assert manager.adopted is False
    finally:
        gdb_srv.close()
        telnet_srv.close()


def test_stop_adopted_has_no_control_port_for_a_non_openocd_server():
    srv, port = _listening_socket()
    try:
        manager = GdbServerManager()
        manager.adopted = True
        manager.port = port
        manager.server_type = "stlink"
        manager.stop(stop_adopted=True)
        assert manager.adopted is True
        assert manager.last_stop_result["shutdown_sent"] is False
        assert manager.last_stop_result["port_free"] is False
    finally:
        srv.close()


def test_stop_adopted_does_not_signal_a_control_port_it_cannot_tie_to_the_server(monkeypatch):
    srv, gdb_port = _listening_socket()
    try:
        sent = []
        monkeypatch.setattr(gdb_manager_module, "_listening_pids", lambda port: [] if port == gdb_port else [9])
        monkeypatch.setattr(gdb_manager_module, "_tcl_shutdown",
                            lambda port, timeout=1.0: sent.append(port) or True)
        monkeypatch.setattr(gdb_manager_module, "_telnet_shutdown",
                            lambda port, timeout=1.0: sent.append(port) or True)
        manager = GdbServerManager()
        manager.adopted = True
        manager.port = gdb_port
        manager.server_type = "openocd"
        manager.server_args = ["-c", f"gdb_port {gdb_port}", "-c", "tcl_port 6666"]

        manager.stop(stop_adopted=True)

        assert sent == []
        assert manager.adopted is True  # still ours, so the caller can retry
        assert manager.port == gdb_port
        assert manager.last_stop_result["port_free"] is False
        assert manager.last_stop_result["shutdown_sent"] is False
    finally:
        srv.close()


def test_listening_pids_sees_a_socket_this_process_holds():
    if not os.path.exists("/proc/net/tcp"):
        pytest.skip("no /proc/net/tcp")
    srv, port = _listening_socket()
    try:
        assert os.getpid() in gdb_manager_module._listening_pids(port)
    finally:
        srv.close()
