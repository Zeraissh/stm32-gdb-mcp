"""recover_session + power_cycle: the ladder as wired into real tool dispatch.

The single most important property here is NEGATIVE: with no hub configured,
recover_current_session must do exactly what it did before this feature existed.
"""

import asyncio
import json

import pytest

from mcp_server.hub import BROWNOUT_MV, HubGuardError, HubManager
from mcp_server.server import handle_call_tool
from mcp_server.tools._helpers import recover_current_session


class _NoSleep:
    """Stand-in for the `time` module so retry backoff costs no wall clock."""

    @staticmethod
    def sleep(_seconds):
        return None


def _payload(result):
    return json.loads(result[0].text)


def _call(name, arguments):
    return _payload(asyncio.run(handle_call_tool(name, arguments)))


def _set_profile(hub_block):
    asyncio.run(handle_call_tool("debug_profile", {"action": "set", "hub": hub_block}))


class _Client:
    def __init__(self):
        self.calls = []

    def stop_gdb(self):
        self.calls.append("stop_gdb")

    def start_gdb(self):
        self.calls.append("start_gdb")

    def connect(self, host, port):
        self.calls.append(("connect", host, port))
        return [{"message": "connected"}]


class _Session:
    def __init__(self):
        self.debug_profile = type("P", (), {"get": staticmethod(lambda: {})})()


# ------------------------------------------------- no hub == unchanged behaviour


def test_no_hub_reproduces_the_exact_pre_hub_sequence(fake_manager):
    client = _Client()
    last = {"server_type": "openocd", "server_args": ["-f", "interface/stlink.cfg"]}

    recovered = recover_current_session(client, fake_manager, last, _Session())

    assert fake_manager.stopped is True
    assert fake_manager.started == [("openocd", ["-f", "interface/stlink.cfg"])]
    assert client.calls == ["stop_gdb", "start_gdb", ("connect", "localhost", 3333)]
    assert recovered["port"] == 3333
    # No hub was involved, so no hub noise in the response.
    assert "recovery_steps" not in recovered


def test_no_prior_session_still_raises(fake_manager):
    with pytest.raises(RuntimeError, match="No prior session"):
        recover_current_session(_Client(), fake_manager, {"server_type": None}, _Session())


def test_recover_session_without_a_hub_reports_no_recovery_steps(monkeypatch, hub_session):
    import mcp_server.server as server_module

    monkeypatch.setattr(server_module, "_last_session",
                        {"server_type": "openocd", "server_args": []})
    # No hub block in the profile -> no binding -> the plain retry path.
    payload = _call("recover_session", {})

    assert payload["ok"] is True
    assert "recovery_steps" not in payload["data"]
    assert "set_channel_usb2_dataline" not in hub_session.hub.calls


# -------------------------------------------------------------- with a hub


class _WedgedManager:
    """Fails `fail_times` starts with a wedged-probe error, then succeeds."""

    def __init__(self, fail_times=3, message="open failed"):
        self.server_type = "openocd"
        self.port = 3333
        self.alive = False
        self.started = []
        self.stopped = False
        self._left = fail_times
        self._message = message

    def is_alive(self):
        return self.alive

    def start(self, server_type, args):
        self.started.append((server_type, list(args or [])))
        if self._left > 0:
            self._left -= 1
            raise RuntimeError(self._message)
        self.alive = True
        return self.port

    def stop(self):
        self.stopped = True
        self.alive = False

    def get_logs(self, lines=50):
        return []


def test_recover_session_escalates_to_the_hub_and_reports_it(monkeypatch, hub_session):
    import mcp_server.server as server_module

    wedged = _WedgedManager(fail_times=3)
    monkeypatch.setattr(server_module, "gdb_manager", wedged)
    monkeypatch.setattr(server_module, "_last_session",
                        {"server_type": "openocd", "server_args": ["-f", "interface/stlink.cfg"]})
    monkeypatch.setattr("mcp_server.tools._helpers.time", _NoSleep)
    _set_profile({"channel": 2, "guard": "allow"})

    payload = _call("recover_session", {})

    assert payload["ok"] is True
    rungs = [step["rung"] for step in payload["data"]["recovery_steps"]]
    assert rungs == ["soft", "data_toggle"]
    # The data toggle really moved the hardware.
    assert hub_session.hub.calls.count("set_channel_usb2_dataline") == 2


def test_escalate_false_keeps_the_hub_out_of_it(monkeypatch, hub_session):
    import mcp_server.server as server_module

    wedged = _WedgedManager(fail_times=3)
    monkeypatch.setattr(server_module, "gdb_manager", wedged)
    monkeypatch.setattr(server_module, "_last_session",
                        {"server_type": "openocd", "server_args": []})
    monkeypatch.setattr("mcp_server.tools._helpers.time", _NoSleep)
    _set_profile({"channel": 2, "guard": "allow"})

    payload = _call("recover_session", {"escalate": False})

    assert payload["ok"] is False
    assert "set_channel_usb2_dataline" not in hub_session.hub.calls


def test_run_for_duration_recovery_never_cuts_power(hub_session):
    # A mid-measurement power cycle would destroy the run being measured, so this
    # call site passes escalate=False. Assert the wiring, not just the intent.
    import inspect

    from mcp_server.tools import execution_tools

    source = inspect.getsource(execution_tools.run_for_duration)
    assert "escalate=False" in source


# ---------------------------------------------------------------- power cycle


def _manager(hub, spec):
    manager = HubManager(backend_factory=lambda **_kwargs: hub)
    manager.configure(spec)
    return manager


def test_power_cycle_switches_off_then_on_and_measures(fake_hub):
    slept = []
    manager = _manager(fake_hub, {"channel": 1, "guard": "allow"})
    fake_hub.residual_voltage_mv = 8

    result = manager.power_cycle(1, off_ms=400, settle_ms=100,
                                 sleep=slept.append, monotonic=lambda: 0.0)

    assert result["measured_off_voltage_mv"] == 8
    assert result["browned_out"] is True
    assert "warning" not in result
    assert slept == [0.4, 0.1]
    assert fake_hub.power[1] == 1  # power is restored


def test_power_cycle_flags_a_rail_that_never_collapsed(fake_hub):
    manager = _manager(fake_hub, {"channel": 1, "guard": "allow"})
    fake_hub.residual_voltage_mv = 3300  # a second supply keeps the board alive

    result = manager.power_cycle(1, settle_ms=0, sleep=lambda _s: None, monotonic=lambda: 0.0)

    assert result["browned_out"] is False
    assert "did NOT cold-boot" in result["warning"]
    assert BROWNOUT_MV == 500


def test_power_cycle_on_a_model_without_an_adc_omits_the_claim(fake_hub):
    manager = _manager(fake_hub, {"channel": 1, "guard": "allow"})
    fake_hub.adc = False

    result = manager.power_cycle(1, settle_ms=0, sleep=lambda _s: None, monotonic=lambda: 0.0)

    # No ADC means no evidence; asserting a cold boot anyway would be a lie.
    assert "measured_off_voltage_mv" not in result
    assert "browned_out" not in result


def test_power_cycle_honours_the_guard(fake_hub):
    manager = _manager(fake_hub, {"channel": 1})  # default confirm mode
    with pytest.raises(HubGuardError):
        manager.power_cycle(1, sleep=lambda _s: None, monotonic=lambda: 0.0)
    assert fake_hub.power[1] == 1


def test_power_cycle_tool_reports_a_failed_brownout_with_a_longer_retry(hub_session):
    _set_profile({"channel": 1, "guard": "allow"})
    hub_session.hub.residual_voltage_mv = 3300

    payload = _call("hub", {"action": "cycle", "settle_ms": 0, "off_ms": 0})

    assert payload["ok"] is True
    assert payload["data"]["browned_out"] is False
    assert "hub(action=cycle, off_ms=2000)" in payload["suggested_next_actions"]


def test_power_cycle_tool_takes_defaults_from_the_profile(hub_session):
    _set_profile({"channel": 1, "guard": "allow", "power_cycle": {"off_ms": 0, "settle_ms": 0}})

    payload = _call("hub", {"action": "cycle"})

    assert payload["data"]["off_ms"] == 0
    assert payload["data"]["settle_ms"] == 0


def test_check_session_health_reports_the_hub_port(hub_session):
    _set_profile({"channel": 3, "guard": "allow"})
    hub_session.hub.power[3] = 0

    payload = _call("session_diagnostics", {"what": "health"})

    assert payload["data"]["hub"]["channel"] == 3
    assert payload["data"]["hub"]["power"] == "off"


def test_check_session_health_omits_hub_when_none_is_configured(hub_session):
    assert "hub" not in _call("session_diagnostics", {"what": "health"})["data"]
