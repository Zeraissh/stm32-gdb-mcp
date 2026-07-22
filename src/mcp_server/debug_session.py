"""Per-target debug session and a manager for several concurrent sessions.

Phase 3 foundation: the server used module-level singletons (one gdb_manager,
gdb_client, profile, log readers, ...), so only ONE target could be debugged at a
time. A DebugSession bundles all per-target state; SessionManager keeps a named
set of them so a test rack / CI can drive multiple boards at once. Tools select a
session via the ``session`` argument (default ``"default"``).
"""

import socket
from typing import Any

from .debug_profile import DebugProfileStore
from .freertos_inspector import FreeRTOSInspector
from .gdb_client import GdbClientManager
from .gdb_manager import GdbServerManager
from .log_reader import FileLogReader, ProcessLogReader, SerialLogReader
from .memory_guard import MemoryWriteGuard
from .svd_parser import SVDParser
from .tracker import VariableTracker

# Default GDB-server port; per-session offset keeps concurrent OpenOCD instances apart.
_BASE_GDB_PORT = 3333
# Named sessions scan _BASE_GDB_PORT+10, +20, ... — bounded so a pathological
# environment (e.g. a firewall swallowing every bind) fails loudly, not forever.
_MAX_PORT_SLOTS = 100


def _port_is_free(port: int) -> bool:
    """True if nothing is listening on the port (catches zombie OpenOCD/foreign servers)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


def teardown_debug_session(session):
    resources = (
        (session.gdb_client, "stop_gdb"),
        (session.gdb_manager, "stop"),
        (session.variable_tracker, "stop"),
        (session.rtt_log_reader, "stop"),
        (session.swo_log_reader, "stop"),
        (session.swo_file_reader, "stop"),
        (session.uart_log_reader, "stop"),
    )
    for obj, method in resources:
        try:
            getattr(obj, method)()
        except Exception:
            pass


class DebugSession:
    """All per-target objects for one debug session."""

    def __init__(self, session_id: str = "default", gdb_port: int | None = None):
        self.id = session_id
        self.gdb_port = gdb_port
        self.serial = None  # ST-Link/probe serial, for selecting a specific board
        self.gdb_manager = GdbServerManager()
        self.gdb_client = GdbClientManager()
        self.svd_parser = SVDParser()
        self.variable_tracker = VariableTracker(self.gdb_client)
        self.debug_profile = DebugProfileStore()
        self.freertos_inspector = FreeRTOSInspector(self.gdb_client)
        self.rtt_log_reader = ProcessLogReader("rtt")
        self.swo_log_reader = ProcessLogReader("swo")
        self.swo_file_reader = FileLogReader("swo")  # OpenOCD-internal ITM decode (no external tool)
        self.uart_log_reader = SerialLogReader()
        self.memory_guard = MemoryWriteGuard()
        self.last_session: dict[str, Any] = {"server_type": None, "server_args": []}
        self.board = {"current": None}  # imported BoardDescription (netlist -> BSP model)
        self.acceptance = {"current": None, "last_result": None}  # loaded AcceptanceSpec + last verdict
        self.loop = {"current": None}  # bounded acceptance-loop state (Pillar C)
        self.design = {"current": None, "last_render": None}  # FrameworkPlan + last render (Pillar D)
        self.spec = {"current": None}  # translated product spec (spec_model, upstream of Pillar D)

    def teardown(self):
        teardown_debug_session(self)


class SessionManager:
    def __init__(self):
        self.sessions = {}

    def get(self, session_id: str = "default") -> DebugSession:
        sid = session_id or "default"
        if sid not in self.sessions:
            if sid == "default":
                port = _BASE_GDB_PORT
            else:
                used = {session.gdb_port for session in self.sessions.values()}
                port = None
                for slot in range(1, _MAX_PORT_SLOTS + 1):
                    candidate = _BASE_GDB_PORT + 10 * slot
                    if candidate not in used and _port_is_free(candidate):
                        port = candidate
                        break
                if port is None:
                    raise RuntimeError(
                        f"No free GDB-server port in {_BASE_GDB_PORT + 10}.."
                        f"{_BASE_GDB_PORT + 10 * _MAX_PORT_SLOTS} (step 10); "
                        "close unused sessions or stray OpenOCD/gdb-server processes."
                    )
            self.sessions[sid] = DebugSession(sid, gdb_port=port)
        return self.sessions[sid]

    def list(self) -> list:
        out = []
        for sid, s in self.sessions.items():
            out.append({
                "session": sid,
                "server_alive": s.gdb_manager.is_alive(),
                "gdb_alive": s.gdb_client.is_alive(),
                "server_type": s.gdb_manager.server_type,
                "port": s.gdb_manager.port,
            })
        return out

    def close(self, session_id: str) -> bool:
        s = self.sessions.pop(session_id, None)
        if s is None:
            return False
        s.teardown()
        return True
