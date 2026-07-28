"""Shared test fakes and fixtures.

The recurring shapes that used to be re-declared per test file live here once:

- ``FakeGdb``        — transport-level pygdbmi stand-in for ``GdbClientManager.gdb``
- ``FakeGdbClient``  — handler-level ``GdbClientManager`` stand-in with canned decoded values
- ``FakeGdbManager`` — ``GdbServerManager`` stand-in
- ``FakeProfile``    — ``DebugProfileStore`` stand-in

New tests should prefer these; scenario-specific fakes (odd MI payloads, error
injection) stay local to their test by design.
"""

import pytest


class FakeGdb:
    """Records every (command, timeout_sec) write and answers with a canned response."""

    def __init__(self, response=None):
        self.commands = []
        # Shaped like a real pygdbmi terminal result record (type matters:
        # mi_guard.has_terminal_result keys off it).
        self._response = response if response is not None else [
            {"type": "result", "message": "done", "payload": None}
        ]

    def write(self, command, timeout_sec=1.0):
        self.commands.append((command, timeout_sec))
        return list(self._response)

    def get_gdb_response(self, timeout_sec=0.1, raise_error_on_timeout=False):
        return []


class FakeGdbClient:
    """Records calls and returns canned, already-decoded values."""

    def __init__(self, stop_reason="breakpoint-hit"):
        self.calls = []
        self._stop_reason = stop_reason
        self.expressions = {"rx_count": "42"}

    def is_alive(self):
        return True

    def set_breakpoint(self, location, condition=None, temporary=False, ignore_count=None):
        self.calls.append(("set_breakpoint", location, condition, temporary, ignore_count))
        return [{"message": "bp"}]

    def run_and_wait(self, timeout_sec):
        self.calls.append(("run_and_wait", timeout_sec))
        return {
            "stopped": self._stop_reason != "timeout",
            "reason": self._stop_reason,
            "frame": {"func": "trigger_divzero", "file": "main.c", "line": 21, "addr": "0x08000046"},
            "raw_response": [],
        }

    def read_call_stack_decoded(self):
        self.calls.append(("read_call_stack_decoded",))
        return [{"level": 0, "func": "trigger_divzero", "file": "main.c", "line": 21, "addr": "0x08000046"}]

    def read_frame_variables_decoded(self, level=None):
        self.calls.append(("read_frame_variables_decoded", level))
        return {"g_divisor": "0"}

    def read_core_registers_decoded(self):
        self.calls.append(("read_core_registers_decoded",))
        return {"pc": "0x08000046", "lr": "0xfffffff9", "sp": "0x200040b0"}

    def load_firmware(self, path):
        self.calls.append(("load_firmware", path))
        return [{"message": "flashed"}]

    def reset_halt(self, command="monitor reset halt"):
        self.calls.append(("reset_halt", command))
        return [{"message": "reset"}]

    def continue_execution(self):
        self.calls.append(("continue_execution",))
        return [{"message": "running"}]

    def halt_execution(self):
        self.calls.append(("halt_execution",))
        return [{"message": "stopped"}]

    def read_variable(self, expression):
        self.calls.append(("read_variable", expression))
        return [{"payload": {"value": self.expressions[expression]}}]


class FakeGdbManager:
    """GdbServerManager stand-in: alive flag, recorded start args, canned logs."""

    def __init__(self, server_type="openocd", port=3333, alive=True):
        self.server_type = server_type
        self.port = port
        self.alive = alive
        self.started = []
        self.stopped = False

    def is_alive(self):
        return self.alive

    def start(self, server_type, args):
        self.started.append((server_type, list(args or [])))
        self.server_type = server_type
        self.alive = True
        return self.port

    def stop(self):
        self.stopped = True
        self.alive = False

    def get_logs(self, lines=50):
        return []


class FakeProfile:
    """DebugProfileStore stand-in over a plain dict (no field validation)."""

    def __init__(self, initial=None):
        self._profile = dict(initial or {})

    def update(self, values):
        for key, value in values.items():
            if value is not None:
                self._profile[key] = value
        return self.get()

    def get(self):
        return dict(self._profile)


@pytest.fixture
def fake_gdb():
    return FakeGdb()


@pytest.fixture
def fake_client():
    return FakeGdbClient()


@pytest.fixture
def fake_manager():
    return FakeGdbManager()


@pytest.fixture
def default_session(monkeypatch):
    """Patch the default session's core objects on mcp_server.server in one shot.

    Yields a namespace with .client/.manager/.profile so tests can assert on
    recorded calls after driving handle_call_tool.
    """
    import types

    import mcp_server.server as server_module

    bundle = types.SimpleNamespace(
        client=FakeGdbClient(),
        manager=FakeGdbManager(),
        profile=FakeProfile(),
    )
    monkeypatch.setattr(server_module, "gdb_client", bundle.client)
    monkeypatch.setattr(server_module, "gdb_manager", bundle.manager)
    monkeypatch.setattr(server_module, "debug_profile", bundle.profile)
    return bundle
