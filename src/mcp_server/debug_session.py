"""Per-target debug session and a manager for several concurrent sessions.

Phase 3 foundation: the server used module-level singletons (one gdb_manager,
gdb_client, profile, log readers, ...), so only ONE target could be debugged at a
time. A DebugSession bundles all per-target state; SessionManager keeps a named
set of them so a test rack / CI can drive multiple boards at once. Tools select a
session via the ``session`` argument (default ``"default"``).
"""

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
        self.last_session = {"server_type": None, "server_args": []}
        self.board = {"current": None}  # imported BoardDescription (netlist -> BSP model)

    def teardown(self):
        for obj, method in ((self.gdb_client, "stop_gdb"), (self.gdb_manager, "stop"),
                            (self.variable_tracker, "stop")):
            try:
                getattr(obj, method)()
            except Exception:
                pass


class SessionManager:
    def __init__(self):
        self.sessions = {}

    def get(self, session_id: str = "default") -> DebugSession:
        sid = session_id or "default"
        if sid not in self.sessions:
            # Give each named session a distinct default GDB port so concurrent
            # OpenOCD instances don't collide (the default session keeps 3333).
            offset = 0 if sid == "default" else (len(self.sessions) + 1) * 10
            self.sessions[sid] = DebugSession(sid, gdb_port=_BASE_GDB_PORT + offset)
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
