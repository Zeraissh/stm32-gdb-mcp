"""detect_probe is cached briefly, and any hub switch drops that cache.

Measured on this bench: 7.8-8.5 s per call, every call, because it spawns
PowerShell and walks the whole USB device tree. A single "flash the board on CH4"
request calls it three times -- look, confirm the isolation took, confirm the
restore -- which was ~24 s of the ~32 s of tool time.

The cache is only safe because it is invalidated by the thing that moves USB
topology. detect_probe is how a caller CONFIRMS a hub isolation took effect, so a
stale answer would report a bench state that no longer exists.
"""
import time

import pytest

from mcp_server import openocd_config
from mcp_server.openocd_config import detect_probe, invalidate_probe_cache


@pytest.fixture(autouse=True)
def _clean_cache():
    invalidate_probe_cache()
    yield
    invalidate_probe_cache()


class CountingRunner:
    """Stands in for subprocess.run and counts how often the OS was really asked."""

    def __init__(self, payload='[{"InstanceId": "USB\\\\VID_0483&PID_3748\\\\X&1", '
                              '"FriendlyName": "STM32 STLink", "Manufacturer": "STMicroelectronics"}]'):
        self.calls = 0
        self._payload = payload

    def __call__(self, *args, **kwargs):
        self.calls += 1

        class Result:
            returncode = 0
            stdout = self._payload
            stderr = ""
        return Result()


def _detect(runner=None):
    # platform_name pins Windows so the test exercises the same branch everywhere.
    return detect_probe(platform_name="Windows", runner=runner)


def test_a_repeat_call_inside_the_window_does_not_re_enumerate(monkeypatch):
    runner = CountingRunner()
    monkeypatch.setattr(openocd_config.subprocess, "run", runner)

    first = _detect()
    second = _detect()
    third = _detect()

    assert runner.calls == 1, "the OS should have been asked exactly once"
    assert first == second == third


def test_a_hub_switch_invalidates_it(monkeypatch):
    # The correctness requirement, not the optimisation: detect_probe is how a
    # caller confirms an isolation took effect.
    runner = CountingRunner()
    monkeypatch.setattr(openocd_config.subprocess, "run", runner)

    _detect()
    invalidate_probe_cache()
    _detect()

    assert runner.calls == 2, "a switch must force a fresh enumeration"


def test_switching_a_hub_channel_calls_the_invalidation(monkeypatch):
    # Wire-level: the hub manager must actually reach for it, not merely be
    # documented as doing so.
    from conftest import FakeHub

    from mcp_server.hub import HubManager

    dropped = []
    monkeypatch.setattr(openocd_config, "invalidate_probe_cache",
                        lambda: dropped.append(True))

    hub = FakeHub()
    manager = HubManager(backend_factory=lambda exclude_ports=None: hub)
    manager._idle_release_sec = 0
    manager.configure({"port": "COM7"})

    manager.data(1, "off", confirm=True)

    assert dropped, "hub._switch must invalidate the probe cache"


def test_the_window_expires(monkeypatch):
    runner = CountingRunner()
    monkeypatch.setattr(openocd_config.subprocess, "run", runner)
    monkeypatch.setattr(openocd_config, "PROBE_CACHE_TTL_S", 0.05)

    _detect()
    time.sleep(0.08)
    _detect()

    assert runner.calls == 2, "a stale entry must not outlive its TTL"


def test_a_caller_supplied_runner_is_never_cached(monkeypatch):
    # Tests inject their own runner; serving them a real-hardware entry (or storing
    # theirs for real callers) would make results depend on test order.
    real = CountingRunner()
    monkeypatch.setattr(openocd_config.subprocess, "run", real)
    _detect()                       # populates the cache from "hardware"

    injected = CountingRunner(payload="[]")
    result = _detect(runner=injected)

    assert injected.calls == 1, "an injected runner must always be used"
    assert result["count"] == 0, "and its answer must not come from the cache"
    assert real.calls == 1, "nor should it disturb the cached hardware entry"


def test_a_failed_enumeration_is_not_cached(monkeypatch):
    def exploding(*args, **kwargs):
        raise OSError("powershell missing")

    monkeypatch.setattr(openocd_config.subprocess, "run", exploding)
    first = _detect()
    assert "error" in first

    runner = CountingRunner()
    monkeypatch.setattr(openocd_config.subprocess, "run", runner)
    second = _detect()

    assert runner.calls == 1, "a failure must not be served for the rest of the TTL"
    assert second["count"] == 1


def test_the_multiple_probes_guidance_prescribes_calls_that_actually_work():
    """A remedy the guard rejects is worse than no remedy: it sends the agent down a
    dead end with a plausible-looking command.

    The first version of this message told the caller to restore the other channels
    with hub(action=data, state="on", channel=M) -- which the default 'confirm'
    guard mode refuses, because data_on is a mutating action. Verified against the
    real guard, not read from the source.
    """
    import inspect

    from conftest import FakeHub

    from mcp_server.hub import HubGuardError, HubManager
    from mcp_server.tools import session_tools

    # The message is built inline in the handler, so assert against the module text.
    assert "channel=M, confirm=true" in inspect.getsource(session_tools), \
        "the restore guidance must carry confirm=true"

    # And prove the guard really does refuse it without confirm, so this test fails
    # if the requirement is ever relaxed and the guidance silently becomes stale.
    manager = HubManager(backend_factory=lambda exclude_ports=None: FakeHub())
    manager._idle_release_sec = 0
    manager.configure({"port": "COM7"})
    with pytest.raises(HubGuardError):
        manager.data(2, "on")
    assert manager.data(2, "on", confirm=True)["applied"] is True


# --- selecting a probe by USB position instead of isolating -------------------

def _session_args(monkeypatch, arguments, profile):
    """Run start_debug_session far enough to see the server_args it assembled."""
    import asyncio
    import json

    from conftest import FakeGdbClient, FakeGdbManager, FakeProfile

    import mcp_server.server as server_module
    from mcp_server.server import handle_call_tool

    manager = FakeGdbManager()
    monkeypatch.setattr(server_module, "gdb_manager", manager)
    monkeypatch.setattr(server_module, "gdb_client", FakeGdbClient())
    monkeypatch.setattr(server_module, "debug_profile", FakeProfile(profile))
    result = asyncio.run(handle_call_tool("start_debug_session", arguments))
    payload = json.loads(result.content[0].text)
    assert payload["ok"] is True, payload
    # FakeGdbManager records (server_type, args) per start.
    assert manager.started, "the session never started"
    return manager.started[-1]


def test_a_channel_with_a_recorded_location_pins_openocd_to_it(monkeypatch):
    # The point: no isolation, nobody disconnected, two boards can run at once.
    profile = {"mcu": "STM32L151", "probe": "stlink", "server_type": "openocd",
               "hub": {"map": {"4": {"label": "AGS", "usb_location": "1-1.4"}}}}

    args = _session_args(monkeypatch, {"hub_channel": 4}, profile)

    flat = " ".join(str(a) for a in (args or []))
    assert "adapter usb location 1-1.4" in flat


def test_an_explicit_location_wins_over_the_map(monkeypatch):
    profile = {"mcu": "STM32L151", "probe": "stlink", "server_type": "openocd",
               "hub": {"map": {"4": {"usb_location": "1-1.4"}}}}

    args = _session_args(monkeypatch, {"hub_channel": 4, "usb_location": "9-9.9"}, profile)

    flat = " ".join(str(a) for a in (args or []))
    assert "adapter usb location 9-9.9" in flat


def test_no_location_is_invented_when_none_is_recorded(monkeypatch):
    # The prefix ("1-1.") is the hub's own position on the bus and cannot be derived
    # from a channel number. A guess would either fail to open or open the wrong probe.
    profile = {"mcu": "STM32L151", "probe": "stlink", "server_type": "openocd",
               "hub": {"map": {"4": {"label": "AGS"}}}}

    args = _session_args(monkeypatch, {"hub_channel": 4}, profile)

    flat = " ".join(str(a) for a in (args or []))
    assert "adapter usb location" not in flat
