"""Three defects found reviewing the hub work against the real rig.

Each one is a "confident wrong answer" — the failure class this server exists to
avoid — rather than a crash.
"""

import asyncio
import json

import pytest

from mcp_server.hub import HubManager, HubUnavailableError
from mcp_server.server import handle_call_tool


def _payload(result):
    return json.loads(result[0].text)


def _call(name, arguments):
    return _payload(asyncio.run(handle_call_tool(name, arguments)))


def _set_profile(hub_block):
    asyncio.run(handle_call_tool("debug_profile", {"action": "set", "hub": hub_block}))


# ---------------------------------------------------------------------------
# 1. check_session_health said target_responsive=true about a dead link.
#    Measured after a hub power cycle dropped the probe: reads returned
#    0x00000000 and only self_check noticed.
# ---------------------------------------------------------------------------


class _Client:
    """GdbClientManager stand-in whose CPUID read and run state are controllable."""

    def __init__(self, cpuid=0x412FC230, raises=None, running=False):
        self.cpuid = cpuid
        self.raises = raises
        self.running = running

    def is_alive(self):
        return True

    def is_running(self):
        return self.running

    def read_word(self, address):
        if self.raises:
            raise self.raises
        return self.cpuid

    def probe_target(self):  # must no longer be consulted
        raise AssertionError("health must judge on evidence, not on probe_target")


def _health(monkeypatch, client):
    import mcp_server.server as server_module

    monkeypatch.setattr(server_module, "gdb_client", client)
    return _call("session_diagnostics", {"what": "health"})["data"]


def test_a_live_target_reports_responsive_with_its_cpuid(monkeypatch, default_session):
    health = _health(monkeypatch, _Client(cpuid=0x412FC230))

    assert health["target_responsive"] is True
    assert health["identity"] == {"cpuid": "0x412fc230", "verified": True}


def test_an_all_zero_read_is_not_reported_as_responsive(monkeypatch, default_session):
    # The exact wedged-session signature seen on hardware.
    health = _health(monkeypatch, _Client(cpuid=0x00000000))

    assert health["target_responsive"] is False
    assert health["identity"]["cpuid"] == "0x00000000"
    assert "not a Cortex-M signature" in health["identity"]["reason"]
    assert "recover_session" in _call(
        "session_diagnostics", {"what": "health"})["suggested_next_actions"]


def test_a_running_core_is_not_reported_as_a_broken_link(monkeypatch, default_session):
    # Measured on hardware: a RUNNING core also reads CPUID back as zeros. That is
    # expected, not a dead probe -- and sending an agent to recover_session here
    # would tear down a perfectly good session.
    import mcp_server.server as server_module

    client = _Client(cpuid=0x00000000, running=True)
    monkeypatch.setattr(server_module, "gdb_client", client)
    payload = _call("session_diagnostics", {"what": "health"})

    assert payload["core_state"] == "running"
    assert "core is RUNNING" in payload["data"]["identity"]["reason"]
    assert "NOT evidence of a dead link" in payload["data"]["identity"]["reason"]
    assert payload["suggested_next_actions"][0] == "halt_execution"
    assert "recover_session" not in payload["suggested_next_actions"]


def test_a_byte_swapped_read_is_also_caught(monkeypatch, default_session):
    # 0x412fc230 byte-reversed: the implementer byte is gone and so is the 0xF.
    health = _health(monkeypatch, _Client(cpuid=0x30C22F41))

    assert health["target_responsive"] is False


def test_an_unanswered_read_is_reported_rather_than_raised(monkeypatch, default_session):
    health = _health(monkeypatch, _Client(raises=RuntimeError("timed out")))

    assert health["target_responsive"] is False
    assert "CPUID read failed" in health["identity"]["reason"]


# ---------------------------------------------------------------------------
# 2. channel_for leaked one session's channel into another's.
#    HubManager is a process-wide singleton; on a rack that means power-cycling
#    a neighbouring board.
# ---------------------------------------------------------------------------


def test_a_session_without_a_hub_block_does_not_inherit_another_ones_channel(fake_hub):
    manager = HubManager(backend_factory=lambda **_kwargs: fake_hub)
    manager.configure({"channel": 4, "guard": "allow"})   # session A binds channel 4

    # Session B has a profile, but no hub block of its own.
    with pytest.raises(HubUnavailableError, match="hub channel unmapped"):
        manager.channel_for(profile={"mcu": "STM32L151"}, session_id="other")


def test_a_direct_caller_with_no_profile_still_uses_the_configured_spec(fake_hub):
    # hil_rack.py and other scripts drive HubManager without any profile at all;
    # that path must keep working.
    manager = HubManager(backend_factory=lambda **_kwargs: fake_hub)
    manager.configure({"channel": 4, "guard": "allow"})

    assert manager.channel_for() == (4, "profile")


def test_an_explicit_channel_still_wins_everywhere(fake_hub):
    manager = HubManager(backend_factory=lambda **_kwargs: fake_hub)
    manager.configure({"channel": 4})

    assert manager.channel_for(explicit=2, profile={"mcu": "x"}) == (2, "argument")


# ---------------------------------------------------------------------------
# 3. The recovery ladder ignored the profile's power_cycle timings while
#    reset_target(strategy="cold") honoured them.
# ---------------------------------------------------------------------------


class _WedgedManager:
    def __init__(self, fail_times):
        self.server_type = "openocd"
        self.port = 3333
        self.alive = False
        self.started = []
        self.stopped = False
        self._left = fail_times

    def is_alive(self):
        return self.alive

    def start(self, server_type, args):
        self.started.append((server_type, list(args or [])))
        if self._left > 0:
            self._left -= 1
            raise RuntimeError("open failed")
        self.alive = True
        return self.port

    def stop(self):
        self.stopped = True

    def get_logs(self, lines=50):
        return []


class _NoSleep:
    @staticmethod
    def sleep(_seconds):
        return None


def test_the_ladder_uses_the_profile_power_cycle_timings(monkeypatch, hub_session):
    import mcp_server.server as server_module

    monkeypatch.setattr(server_module, "gdb_manager", _WedgedManager(fail_times=5))
    monkeypatch.setattr(server_module, "_last_session",
                        {"server_type": "openocd", "server_args": []})
    monkeypatch.setattr("mcp_server.tools._helpers.time", _NoSleep)
    _set_profile({"channel": 2, "guard": "allow",
                  "power_cycle": {"off_ms": 800, "settle_ms": 0}})

    payload = _call("recover_session", {})

    cycle = [s for s in payload["data"]["recovery_steps"] if s["rung"] == "power_cycle"][0]
    assert cycle["off_ms"] == 800, "the ladder must not silently use its own default"


def test_the_ladder_falls_back_to_its_default_without_a_configured_timing(monkeypatch, hub_session):
    import mcp_server.server as server_module

    monkeypatch.setattr(server_module, "gdb_manager", _WedgedManager(fail_times=5))
    monkeypatch.setattr(server_module, "_last_session",
                        {"server_type": "openocd", "server_args": []})
    monkeypatch.setattr("mcp_server.tools._helpers.time", _NoSleep)
    _set_profile({"channel": 2, "guard": "allow"})

    payload = _call("recover_session", {})

    cycle = [s for s in payload["data"]["recovery_steps"] if s["rung"] == "power_cycle"][0]
    assert cycle["off_ms"] == 400
