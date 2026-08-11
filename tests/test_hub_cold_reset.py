"""strategy="cold" and hub(action=measure): a real power-on reset, and power draw."""

import asyncio
import json

import pytest

from mcp_server.hub import HubManager, HubUnavailableError
from mcp_server.reset_strategy import resolve_reset_command
from mcp_server.server import handle_call_tool


def _payload(result):
    return json.loads(result[0].text)


def _call(name, arguments):
    return _payload(asyncio.run(handle_call_tool(name, arguments)))


def _set_profile(hub_block):
    asyncio.run(handle_call_tool("debug_profile", {"action": "set", "hub": hub_block}))


# ------------------------------------------------------------ reset_strategy


@pytest.mark.parametrize("server_type", ["openocd", "stlink", "jlink"])
def test_cold_resolves_on_every_backend_and_flags_the_power_cycle(server_type):
    resolved = resolve_reset_command(server_type, halt=True, strategy="cold")

    assert resolved["strategy"] == "cold"
    assert resolved["requires_power_cycle"] is True


def test_cold_is_not_reported_as_an_alias():
    # It maps to the default's command on purpose -- what makes it cold is the
    # power sequence, so an "alias" note here would be actively misleading.
    resolved = resolve_reset_command("openocd", halt=True, strategy="cold")
    assert "note" not in resolved


def test_under_reset_is_still_reported_as_an_alias_and_now_points_at_cold():
    resolved = resolve_reset_command("openocd", halt=True, strategy="under_reset")

    assert "resolves to the same command" in resolved["note"]
    assert "cold" not in resolved["note"].split("as [")[1].split("]")[0]  # not listed as an alias
    assert 'strategy="cold"' in resolved["note"]


def test_default_and_software_are_unchanged():
    assert resolve_reset_command("openocd", halt=True)["command"] == "monitor reset halt"
    assert resolve_reset_command("openocd", halt=True, strategy="software")["command"] == \
        "monitor soft_reset_halt"
    assert "requires_power_cycle" not in resolve_reset_command("openocd", halt=True)


# ------------------------------------------------------------- reset_target


def test_cold_reset_refuses_when_no_hub_channel_is_mapped(hub_session):
    payload = _call("reset_target", {"halt": True, "strategy": "cold"})

    assert payload["ok"] is False
    assert payload["error"]["code"] == "cold_reset_unavailable"
    assert 'reset_target(strategy="default")' in payload["suggested_next_actions"]
    # Nothing was reset: a silent warm reset here would be a false success.
    assert ("reset_halt", "monitor reset halt") not in hub_session.session.client.calls


def test_cold_reset_power_cycles_then_resets(hub_session):
    _set_profile({"channel": 2, "guard": "allow", "power_cycle": {"off_ms": 0, "settle_ms": 0}})

    payload = _call("reset_target", {"halt": True, "strategy": "cold"})

    assert payload["ok"] is True
    assert payload["data"]["message"] == "Target power-cycled and reset"
    assert payload["data"]["power_cycle"]["channel"] == 2
    assert payload["data"]["power_cycle"]["browned_out"] is True
    # Power really was removed and restored, and the reset still ran.
    assert hub_session.hub.calls.count("set_channel_power") == 2
    assert hub_session.hub.power[2] == 1
    assert ("reset_halt", "monitor reset halt") in hub_session.session.client.calls


def test_cold_reset_needs_no_confirm_because_the_caller_named_it(hub_session):
    # Guard is left at its default "confirm": asking for a cold reset by name IS
    # the confirmation, otherwise the strategy would be unusable.
    _set_profile({"channel": 2, "power_cycle": {"off_ms": 0, "settle_ms": 0}})

    assert _call("reset_target", {"halt": True, "strategy": "cold"})["ok"] is True


def test_cold_reset_reports_a_rail_that_never_collapsed(hub_session):
    _set_profile({"channel": 2, "guard": "allow", "power_cycle": {"off_ms": 0, "settle_ms": 0}})
    hub_session.hub.residual_voltage_mv = 3300  # a second supply keeps the board alive

    payload = _call("reset_target", {"halt": True, "strategy": "cold"})

    assert payload["data"]["power_cycle"]["browned_out"] is False
    assert "did NOT cold-boot" in payload["data"]["power_cycle"]["warning"]


def test_a_failing_power_cycle_does_not_fall_back_to_a_warm_reset(hub_session):
    _set_profile({"channel": 2, "guard": "allow", "power_cycle": {"off_ms": 0, "settle_ms": 0}})
    hub_session.hub.ack = False

    payload = _call("reset_target", {"halt": True, "strategy": "cold"})

    assert payload["ok"] is False
    assert payload["error"]["code"] == "cold_reset_failed"
    assert ("reset_halt", "monitor reset halt") not in hub_session.session.client.calls


def test_a_normal_reset_never_touches_the_hub(hub_session):
    _set_profile({"channel": 2, "guard": "allow"})

    assert _call("reset_target", {"halt": True})["ok"] is True
    assert "set_channel_power" not in hub_session.hub.calls


def test_cold_reset_can_come_from_the_reset_config_block(hub_session):
    _set_profile({"channel": 2, "guard": "allow", "power_cycle": {"off_ms": 0, "settle_ms": 0}})
    asyncio.run(handle_call_tool("debug_profile", {
        "action": "set", "reset": {"strategy": "cold"}}))

    payload = _call("reset_target", {"halt": True})
    assert payload["data"]["reset"]["strategy"] == "cold"
    assert "power_cycle" in payload["data"]


# ----------------------------------------------------------------- measure


def _manager(hub):
    manager = HubManager(backend_factory=lambda **_kwargs: hub)
    manager.configure({"guard": "allow"})
    return manager


def test_a_zero_duration_window_takes_exactly_one_snapshot(fake_hub):
    result = _manager(fake_hub).measure((1,), duration_sec=0.0,
                                        sleep=lambda _s: None, monotonic=lambda: 0.0)

    assert result["timing"]["samples"] == 1
    assert len(result["series"]["1"]) == 1
    assert result["series"]["1"][0]["current_ma"] == 43


def test_a_window_samples_until_the_deadline(fake_hub):
    clock = {"t": 0.0}
    slept = []

    def monotonic():
        return clock["t"]

    def sleep(seconds):
        slept.append(seconds)
        clock["t"] += seconds

    result = _manager(fake_hub).measure((1,), duration_sec=0.2, interval_sec=0.05,
                                        sleep=sleep, monotonic=monotonic)

    assert result["timing"]["samples"] == 5  # t=0, .05, .10, .15, .20
    assert slept == [0.05] * 4


def test_summary_reports_min_max_mean_per_channel(fake_hub):
    result = _manager(fake_hub).measure((1, 2), duration_sec=0.0,
                                        sleep=lambda _s: None, monotonic=lambda: 0.0)

    for channel in ("1", "2"):
        summary = result["summary"][channel]
        assert summary["samples"] == 1
        assert summary["current_ma"] == {"min": 43, "max": 43, "mean": 43.0}
        assert summary["voltage_mv"]["mean"] == 5012.0


def test_measure_without_an_adc_says_so(fake_hub):
    fake_hub.adc = False
    with pytest.raises(HubUnavailableError, match="no ADC"):
        _manager(fake_hub).measure((1,), sleep=lambda _s: None, monotonic=lambda: 0.0)


def test_measure_tool_defaults_to_the_session_channel(hub_session):
    _set_profile({"channel": 3, "guard": "allow"})

    payload = _call("hub", {"action": "measure"})

    assert payload["ok"] is True
    assert list(payload["data"]["series"]) == ["3"]
    assert payload["data"]["channel_source"] == "profile"


def test_measure_tool_can_sweep_every_channel(hub_session):
    payload = _call("hub", {"action": "measure", "all_channels": True})

    assert sorted(payload["data"]["series"]) == ["1", "2", "3", "4"]
    assert payload["data"]["channel_source"] == "all_channels"


def test_measure_tool_reports_no_adc_cleanly(hub_session):
    _set_profile({"channel": 1, "guard": "allow"})
    hub_session.hub.adc = False

    payload = _call("hub", {"action": "measure"})
    assert payload["ok"] is False
    assert payload["error"]["code"] == "hub_unavailable"


def test_measure_is_reachable_through_call_read():
    from mcp_server.tool_surface import read_only_tool_names

    assert "measure_hub_channel" in read_only_tool_names()
