"""Shared test fakes and fixtures.

The recurring shapes that used to be re-declared per test file live here once:

- ``FakeGdb``        — transport-level pygdbmi stand-in for ``GdbClientManager.gdb``
- ``FakeGdbClient``  — handler-level ``GdbClientManager`` stand-in with canned decoded values
- ``FakeGdbManager`` — ``GdbServerManager`` stand-in
- ``FakeProfile``    — ``DebugProfileStore`` stand-in
- ``FakeHub``        — ``HubBackend`` stand-in for the optional SmartUSBHub extra

New tests should prefer these; scenario-specific fakes (odd MI payloads, error
injection) stay local to their test by design.
"""

import pytest

from mcp_server.debug_profile import deep_merge


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

    def probe_target(self):
        self.calls.append(("probe_target",))
        return True

    def start_gdb(self):
        self.calls.append(("start_gdb",))

    def stop_gdb(self):
        self.calls.append(("stop_gdb",))

    def connect(self, host, port):
        self.calls.append(("connect", host, port))
        return [{"message": "connected"}]

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

    def update(self, values, merge=False):
        for key, value in values.items():
            if value is None:
                continue
            current = self._profile.get(key)
            if merge and isinstance(current, dict) and isinstance(value, dict):
                self._profile[key] = deep_merge(current, value)
            else:
                self._profile[key] = value
        return self.get()

    def get(self):
        return dict(self._profile)


class FakeHub:
    """HubBackend stand-in: per-channel power/data state and canned measurements.

    Never sleeps and never touches a serial port, so hub tests run at unit speed.
    ``fail_on`` injects a vendor failure per method name; ``ack`` set to False
    reproduces the "device did not acknowledge" path that a dropped link causes.
    """

    def __init__(self, channels=(1, 2, 3, 4), adc=True, interlock=False):
        self.calls = []
        self._channels = tuple(channels)
        self.power = dict.fromkeys(self._channels, 1)
        self.data = dict.fromkeys(self._channels, 1)
        self.names = {}
        self.voltage_mv = 5012
        self.current_ma = 43
        # What the ADC reads on a port whose power is OFF. 0 is the normal case;
        # raise it to model a board held up by a second supply, a coin cell, or
        # bulk capacitance -- the case power_cycle has to detect and report.
        self.residual_voltage_mv = 0
        self.adc = adc
        self.interlock = interlock
        self.fail_on = {}
        # Channels whose samples the device reports as not valid.
        self.invalid_channels: set[int] = set()
        self.ack = True
        self.connected = True
        self.disconnect_callback = None

    def _check(self, method):
        self.calls.append(method)
        exc = self.fail_on.get(method)
        if exc is not None:
            raise exc

    def get_channels(self):
        self._check("get_channels")
        return self._channels

    def get_device_info(self):
        self._check("get_device_info")
        return {
            "id": "fake0", "product_type": "HBP_USB2_4CH", "serial_no": "SUH-FAKE-0001",
            "max_channels": len(self._channels), "firmware_version": 21,
            "operate_mode": "interlock" if self.interlock else "normal",
        }

    def get_operate_mode(self):
        self._check("get_operate_mode")
        return 1 if self.interlock else 0

    def get_serial_no(self):
        self._check("get_serial_no")
        return "SUH-FAKE-0001"

    def get_product_name(self):
        self._check("get_product_name")
        return "HBP_USB2_4CH"

    def get_channel_name(self, channel):
        self._check("get_channel_name")
        return self.names.get(channel)

    def set_channel_power(self, *channels, state):
        self._check("set_channel_power")
        if not self.ack:
            return False
        for channel in channels:
            self.power[channel] = state
        return True

    def get_channel_power_status(self, *channels):
        self._check("get_channel_power_status")
        if len(channels) == 1:
            return self.power.get(channels[0])
        return {ch: self.power.get(ch) for ch in channels}

    def set_channel_usb2_dataline(self, *channels, state):
        self._check("set_channel_usb2_dataline")
        if not self.ack:
            return False
        for channel in channels:
            self.data[channel] = state
        return True

    def get_channel_usb2_dataline_status(self, *channels):
        self._check("get_channel_usb2_dataline_status")
        return {ch: self.data.get(ch) for ch in channels}

    def get_channel_measurements(self, *channels):
        self._check("get_channel_measurements")
        if not self.adc:
            raise RuntimeError("model has no ADC")
        return {
            ch: {
                "voltage": self.voltage_mv if self.power.get(ch) else self.residual_voltage_mv,
                "current": self.current_ma if self.power.get(ch) else 0,
                "fresh": True, "stale": False,
                "valid": ch not in self.invalid_channels,
            }
            for ch in channels
        }

    def register_disconnect_callback(self, callback):
        self.disconnect_callback = callback

    def is_connected(self):
        return self.connected

    def disconnect(self):
        self.calls.append("disconnect")
        self.connected = False


@pytest.fixture(autouse=True)
def _reset_global_hub_manager():
    """Keep the process-wide hub singleton from leaking config between tests.

    ``hub_manager`` is a module global by design (that is how _make_context picks
    it up), so a ``configure`` in one test would otherwise decide the channel in
    the next one.
    """
    from mcp_server.hub import HubPowerGuard, hub_manager

    yield
    hub_manager._spec = {}
    hub_manager._hub = None
    hub_manager._channels = ()
    hub_manager._info = {}
    hub_manager._interlock = False
    hub_manager._link_lost = False
    hub_manager._we_turned_off.clear()
    hub_manager.guard = HubPowerGuard()


@pytest.fixture
def fake_hub():
    return FakeHub()


@pytest.fixture
def hub_manager(fake_hub):
    """A HubManager wired to FakeHub, bypassing the optional vendor import."""
    from mcp_server.hub import HubManager

    return HubManager(backend_factory=lambda **_kwargs: fake_hub)


@pytest.fixture
def hub_session(monkeypatch, fake_hub, hub_manager, default_session):
    """default_session plus mcp_server.server.hub_manager patched to a fake hub.

    Mirrors how every other hardware dependency is injected here: patch the module
    global on mcp_server.server, which _make_context re-reads on every dispatch.
    """
    import types

    import mcp_server.server as server_module

    monkeypatch.setattr(server_module, "hub_manager", hub_manager)
    # No debug session is running by default. FakeGdbManager reports alive=True out
    # of the box, which would trip the live-session interlock on every power write
    # and hide what these tests are actually about; tests that want the interlock
    # set .session.manager.alive back to True explicitly.
    default_session.manager.alive = False
    return types.SimpleNamespace(hub=fake_hub, manager=hub_manager, session=default_session)


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
