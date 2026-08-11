"""hub(action=...) dispatch: routing, error envelopes, and the confirmation guard."""

import asyncio
import json

import pytest

from mcp_server.server import handle_call_tool, handle_list_tools
from mcp_server.tool_surface import CORE_TOOLS, HARDWARE_WRITE_TOOLS, read_only_tool_names


def _payload(result):
    return json.loads(result[0].text)


def _call(name, arguments):
    return _payload(asyncio.run(handle_call_tool(name, arguments)))


def _set_profile(hub_block):
    asyncio.run(handle_call_tool("debug_profile", {"action": "set", "hub": hub_block}))


# ------------------------------------------------------------------ surface


def test_hub_family_is_advertised_and_singles_are_merged_away():
    names = {tool.name for tool in asyncio.run(handle_list_tools())}

    assert "hub" in names
    for merged in ("describe_hub", "set_hub_power", "set_hub_data"):
        assert merged not in names


def test_hub_is_a_core_tool_and_a_hardware_write():
    assert "hub" in CORE_TOOLS
    assert "hub" in HARDWARE_WRITE_TOOLS


def test_describe_hub_is_reachable_through_call_read():
    # The family is destructive (it can cut VBUS), so the read-only action needs
    # its own route or agents pay an approval prompt just to look at the hub.
    assert "describe_hub" in read_only_tool_names()
    assert "set_hub_power" not in read_only_tool_names()


def test_missing_action_is_a_clear_error(hub_session):
    payload = _call("hub", {})
    assert payload["ok"] is False
    assert payload["error"]["code"] == "missing_argument"


# ------------------------------------------------------------------ routing


def test_describe_routes_to_the_manager(hub_session):
    payload = _call("hub", {"action": "describe"})

    assert payload["ok"] is True
    assert payload["data"]["channels"] == [1, 2, 3, 4]
    assert payload["data"]["device"]["serial_no"] == "SUH-FAKE-0001"


def test_power_off_switches_the_configured_channel(hub_session):
    _set_profile({"channel": 2, "guard": "allow"})
    payload = _call("hub", {"action": "power", "state": "off"})

    assert payload["ok"] is True
    assert payload["data"]["channel"] == 2
    assert payload["data"]["channel_source"] == "profile"
    assert hub_session.hub.power[2] == 0


def test_explicit_channel_beats_the_profile(hub_session):
    _set_profile({"channel": 2, "guard": "allow"})
    payload = _call("hub", {"action": "power", "state": "off", "channel": 4})

    assert payload["data"]["channel"] == 4
    assert payload["data"]["channel_source"] == "argument"
    assert hub_session.hub.power == {1: 1, 2: 1, 3: 1, 4: 0}


def test_data_exclusive_leaves_one_probe_enumerated(hub_session):
    _set_profile({"channel": 3, "guard": "allow"})
    payload = _call("hub", {"action": "data", "state": "on", "exclusive": True})

    assert sorted(payload["data"]["data_off_channels"]) == [1, 2, 4]
    assert hub_session.hub.data == {1: 0, 2: 0, 3: 1, 4: 0}


def test_unmapped_session_refuses_rather_than_guessing(hub_session):
    payload = _call("hub", {"action": "power", "state": "off"})

    assert payload["ok"] is False
    assert payload["error"]["code"] == "hub_unavailable"
    assert "unmapped" in payload["error"]["message"]


def test_map_by_label_resolves_the_default_session(hub_session):
    _set_profile({"guard": "allow", "map": {1: {"label": "l151"}, 4: {"label": "default"}}})
    payload = _call("hub", {"action": "power", "state": "off"})

    assert payload["data"]["channel"] == 4
    assert payload["data"]["channel_source"] == "map_label"


# -------------------------------------------------------------------- guard


def test_confirm_mode_blocks_and_reports_hub_guard_blocked(hub_session):
    _set_profile({"channel": 1})  # default guard mode is "confirm"
    payload = _call("hub", {"action": "power", "state": "off"})

    assert payload["ok"] is False
    assert payload["error"]["code"] == "hub_guard_blocked"
    assert hub_session.hub.power[1] == 1


def test_confirm_true_lets_it_through(hub_session):
    _set_profile({"channel": 1})
    payload = _call("hub", {"action": "power", "state": "off", "confirm": True})

    assert payload["ok"] is True
    assert hub_session.hub.power[1] == 0


def test_a_live_gdb_server_on_the_channel_forces_confirmation(hub_session):
    _set_profile({"channel": 1, "guard": "allow"})
    hub_session.session.manager.alive = True

    payload = _call("hub", {"action": "power", "state": "off"})
    assert payload["ok"] is False
    assert payload["error"]["code"] == "hub_guard_blocked"
    assert "live GDB server" in payload["error"]["message"]
    assert hub_session.hub.power[1] == 1


def test_a_live_server_on_a_different_channel_does_not_block(hub_session):
    _set_profile({"channel": 1, "guard": "allow"})
    hub_session.session.manager.alive = True

    payload = _call("hub", {"action": "power", "state": "off", "channel": 3})
    assert payload["ok"] is True
    assert hub_session.hub.power[3] == 0


def test_a_dead_gdb_server_does_not_force_confirmation(hub_session):
    _set_profile({"channel": 1, "guard": "allow"})
    hub_session.session.manager.alive = False

    assert _call("hub", {"action": "power", "state": "off"})["ok"] is True


# ------------------------------------------------------------------- errors


def test_interlock_mode_is_reported_as_a_guard_block(hub_session):
    hub_session.hub.interlock = True
    _set_profile({"channel": 1, "guard": "allow"})

    payload = _call("hub", {"action": "power", "state": "off"})
    assert payload["error"]["code"] == "hub_guard_blocked"
    assert "interlock" in payload["error"]["message"]


def test_a_dropped_acknowledgement_is_reported_not_swallowed(hub_session):
    _set_profile({"channel": 1, "guard": "allow"})
    hub_session.hub.ack = False

    payload = _call("hub", {"action": "power", "state": "off"})
    assert payload["ok"] is False
    assert payload["error"]["code"] == "hub_unavailable"
    assert "did not acknowledge" in payload["error"]["message"]


@pytest.mark.parametrize("action", ["power", "data"])
def test_an_invalid_state_is_an_argument_error(hub_session, action):
    _set_profile({"channel": 1, "guard": "allow"})
    payload = _call("hub", {"action": action, "state": "toggle"})

    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_argument"


def test_a_nonexistent_channel_is_rejected(hub_session):
    _set_profile({"guard": "allow"})
    payload = _call("hub", {"action": "power", "state": "off", "channel": 9})

    assert payload["ok"] is False
    assert "does not exist" in payload["error"]["message"]
