"""Channel-to-probe discovery: the toggle/diff, its fallbacks, and its refusals."""

import asyncio
import json

from mcp_server.hub_topology import (
    discover_by_toggle,
    map_from_config,
    probe_key,
    probe_keys,
    usb_location_hint,
)
from mcp_server.server import handle_call_tool


def _payload(result):
    return json.loads(result[0].text)


def _call(name, arguments):
    return _payload(asyncio.run(handle_call_tool(name, arguments)))


STLINK_A = {"type": "stlink", "serial": "0671FF565251", "instance_id": "USB\\VID_0483&PID_374B\\A"}
STLINK_B = {"type": "stlink", "serial": "066BFF383733", "instance_id": "USB\\VID_0483&PID_374B\\B"}
NOSERIAL_A = {"type": "cmsis-dap", "instance_id": "USB\\VID_0D28&PID_0204\\6&1A2B"}
NOSERIAL_B = {"type": "cmsis-dap", "location": "1-4.2"}


# ------------------------------------------------------------------- keying


def test_serial_is_preferred_as_the_identity():
    assert probe_key(STLINK_A) == "serial:0671FF565251"


def test_instance_id_is_the_fallback_for_an_unserialised_probe():
    assert probe_key(NOSERIAL_A) == "instance_id:USB\\VID_0D28&PID_0204\\6&1A2B"


def test_location_is_the_last_resort():
    assert probe_key(NOSERIAL_B) == "location:1-4.2"


def test_a_probe_with_no_identifying_field_is_skipped():
    assert probe_key({"type": "stlink"}) is None
    assert probe_keys({"probes": [{"type": "stlink"}, STLINK_A]}) == {"serial:0671FF565251"}


def test_probe_keys_tolerates_a_missing_or_empty_detection():
    assert probe_keys({}) == set()
    assert probe_keys({"probes": None}) == set()


def test_usb_location_hint_is_advisory_only():
    assert usb_location_hint(NOSERIAL_B) == "1-4.2"
    assert usb_location_hint({}) is None


def test_map_from_config_normalizes_yaml_string_keys():
    assert map_from_config({"map": {"2": {"label": "l431"}}}) == {2: {"label": "l431"}}
    assert map_from_config(None) == {}


# ---------------------------------------------------------------- discovery


class _Hub:
    """Tracks which channels are data-connected and which probe sits on each."""

    def __init__(self, wiring):
        self.wiring = dict(wiring)          # {channel: probe dict or None}
        self.connected = dict.fromkeys(wiring, True)
        self.actions = []
        self.fail_on_channel = None

    def data(self, channel, state, confirm=False):
        self.actions.append((channel, state))
        if channel == self.fail_on_channel:
            raise RuntimeError("hub did not acknowledge")
        self.connected[channel] = state == "on"

    def detect(self):
        return {"probes": [probe for ch, probe in self.wiring.items()
                           if probe and self.connected[ch]]}


def _discover(hub, channels=None, settle=0.0):
    return discover_by_toggle(hub, hub.detect, channels or tuple(hub.wiring),
                              sleep=lambda _s: None, settle_sec=settle)


def test_each_channel_is_matched_to_the_probe_that_vanishes():
    hub = _Hub({1: STLINK_A, 2: None, 3: STLINK_B, 4: None})
    result = _discover(hub)

    assert result["map"][1]["serial"] == "0671FF565251"
    assert result["map"][3]["serial"] == "066BFF383733"
    assert 2 not in result["map"] and 4 not in result["map"]
    assert result["ambiguous"] == []
    assert result["unmapped_probes"] == []


def test_every_data_line_is_restored_afterwards():
    hub = _Hub({1: STLINK_A, 2: STLINK_B})
    _discover(hub)

    assert all(hub.connected.values())
    assert hub.actions == [(1, "off"), (1, "on"), (2, "off"), (2, "on")]


def test_an_unserialised_probe_maps_by_instance_id():
    hub = _Hub({1: NOSERIAL_A, 2: None})
    result = _discover(hub)

    assert result["map"][1]["key"] == "instance_id:USB\\VID_0D28&PID_0204\\6&1A2B"
    assert "serial" not in result["map"][1]
    assert result["map"][1]["probe"] == "cmsis-dap"


def test_the_probe_type_and_usb_hint_ride_along():
    hub = _Hub({1: STLINK_A})
    entry = _discover(hub)["map"][1]

    assert entry["probe"] == "stlink"
    assert entry["usb_hint"] == "USB\\VID_0483&PID_374B\\A"


def test_two_probes_vanishing_at_once_is_reported_not_guessed():
    class _Ganged(_Hub):
        def data(self, channel, state, confirm=False):
            # One physical switch feeding two probes: both disappear together.
            super().data(channel, state, confirm)
            if channel == 1:
                self.connected[2] = state == "on"

    hub = _Ganged({1: STLINK_A, 2: STLINK_B})
    result = _discover(hub)

    assert 1 not in result["map"]
    assert result["ambiguous"][0]["channel"] == 1
    assert "cannot attribute" in result["ambiguous"][0]["reason"]
    assert len(result["ambiguous"][0]["probes"]) == 2


def test_a_probe_on_no_hub_channel_is_reported_unmapped():
    hub = _Hub({1: STLINK_A})
    hub.detect_extra = STLINK_B
    original = hub.detect

    def detect():
        found = original()
        found["probes"] = list(found["probes"]) + [STLINK_B]  # plugged straight into the host
        return found

    result = discover_by_toggle(hub, detect, (1,), sleep=lambda _s: None)

    assert result["map"][1]["serial"] == "0671FF565251"
    assert result["unmapped_probes"] == ["serial:066BFF383733"]


def test_a_channel_that_cannot_be_toggled_is_reported_not_silently_skipped():
    hub = _Hub({1: STLINK_A, 2: STLINK_B})
    hub.fail_on_channel = 1
    result = _discover(hub)

    assert 1 not in result["map"]
    assert result["ambiguous"][0]["channel"] == 1
    assert "could not disconnect" in result["ambiguous"][0]["reason"]
    assert result["map"][2]["serial"] == "066BFF383733"  # the rest still works


def test_detect_call_count_is_reported_because_it_is_the_expensive_part():
    hub = _Hub({1: STLINK_A, 2: None, 3: None, 4: None})
    assert _discover(hub)["detect_calls"] == 5  # one baseline + one per channel


# -------------------------------------------------------------------- tool


def test_discover_refuses_while_a_gdb_server_is_alive(hub_session):
    hub_session.session.manager.alive = True

    payload = _call("hub", {"action": "discover"})
    assert payload["ok"] is False
    assert payload["error"]["code"] == "hub_busy_sessions"
    assert "set_channel_usb2_dataline" not in hub_session.hub.calls


def test_force_overrides_the_live_session_refusal(monkeypatch, hub_session):
    import mcp_server.server as server_module

    hub_session.session.manager.alive = True
    monkeypatch.setattr(server_module, "detect_probe", lambda **_kw: {"probes": []})
    monkeypatch.setattr("mcp_server.tools.hub_tools.time", _NoSleep)

    payload = _call("hub", {"action": "discover", "force": True, "settle_sec": 0})
    assert payload["ok"] is True


class _NoSleep:
    @staticmethod
    def sleep(_seconds):
        return None


def test_discover_applies_the_map_to_the_profile(monkeypatch, hub_session):
    import mcp_server.server as server_module

    hub = hub_session.hub
    monkeypatch.setattr("mcp_server.tools.hub_tools.time", _NoSleep)
    monkeypatch.setattr(server_module, "detect_probe",
                        lambda **_kw: {"probes": [STLINK_A] if hub.data[2] else []})

    payload = _call("hub", {"action": "discover", "apply": True, "settle_sec": 0})

    assert payload["data"]["applied"] is True
    # JSON has no integer keys: the wire form is stringified, and _normalize_map
    # accepts it back unchanged.
    assert payload["data"]["map"]["2"]["serial"] == "0671FF565251"

    profile = _call("debug_profile", {"action": "get"})
    assert profile["data"]["hub"]["map"]["2"]["serial"] == "0671FF565251"
    assert "note" not in payload["data"]  # every probe had a serial


def test_an_unserialised_probe_is_persisted_by_port_key_not_dropped(monkeypatch, hub_session):
    # Found on real hardware: two ST-Link/V2 clones both report VID:PID 0483:3748
    # with NO serial. Persisting only serial/label wrote {} and silently threw the
    # discovery result away.
    import mcp_server.server as server_module

    hub = hub_session.hub
    monkeypatch.setattr("mcp_server.tools.hub_tools.time", _NoSleep)
    monkeypatch.setattr(server_module, "detect_probe",
                        lambda **_kw: {"probes": [NOSERIAL_A] if hub.data[2] else []})

    payload = _call("hub", {"action": "discover", "apply": True, "settle_sec": 0})

    entry = _call("debug_profile", {"action": "get"})["data"]["hub"]["map"]["2"]
    assert entry != {}
    assert entry["key"] == "instance_id:USB\\VID_0D28&PID_0204\\6&1A2B"
    assert "serial" not in entry
    # And it says so, rather than leaving an entry that cannot select anything.
    assert "no serial number" in payload["data"]["note"]
    assert "label" in payload["data"]["note"]


def test_applying_a_map_never_drops_a_human_chosen_label(monkeypatch, hub_session):
    import mcp_server.server as server_module

    hub = hub_session.hub
    monkeypatch.setattr("mcp_server.tools.hub_tools.time", _NoSleep)
    monkeypatch.setattr(server_module, "detect_probe",
                        lambda **_kw: {"probes": [STLINK_A] if hub.data[2] else []})
    asyncio.run(handle_call_tool("debug_profile", {
        "action": "set", "hub": {"guard": "allow", "map": {"2": {"label": "l431"}}}}))

    _call("hub", {"action": "discover", "apply": True, "settle_sec": 0})

    entry = _call("debug_profile", {"action": "get"})["data"]["hub"]["map"]["2"]
    assert entry["label"] == "l431"
    assert entry["serial"] == "0671FF565251"


def test_an_applied_map_resolves_the_channel_on_the_next_call(monkeypatch, hub_session):
    import mcp_server.server as server_module

    hub = hub_session.hub
    monkeypatch.setattr("mcp_server.tools.hub_tools.time", _NoSleep)
    monkeypatch.setattr(server_module, "detect_probe",
                        lambda **_kw: {"probes": [STLINK_A] if hub.data[2] else []})
    _call("hub", {"action": "discover", "apply": True, "settle_sec": 0})

    # The discovered serial now selects the channel without anyone naming it.
    hub_session.session.session_serial = None
    asyncio.run(handle_call_tool("debug_profile", {"action": "set", "serial": "0671FF565251"}))
    payload = _call("hub", {"action": "measure"})

    assert list(payload["data"]["series"]) == ["2"]
    assert payload["data"]["channel_source"] == "map_serial"


def test_discovery_leaves_every_data_line_connected(monkeypatch, hub_session):
    import mcp_server.server as server_module

    monkeypatch.setattr("mcp_server.tools.hub_tools.time", _NoSleep)
    monkeypatch.setattr(server_module, "detect_probe", lambda **_kw: {"probes": []})

    _call("hub", {"action": "discover", "settle_sec": 0})
    assert hub_session.hub.data == {1: 1, 2: 1, 3: 1, 4: 1}


def test_discovery_needs_no_per_channel_confirmation(monkeypatch, hub_session):
    # The guard is left at its default "confirm": calling discover IS the request,
    # and it only ever restores what it disconnected.
    import mcp_server.server as server_module

    monkeypatch.setattr("mcp_server.tools.hub_tools.time", _NoSleep)
    monkeypatch.setattr(server_module, "detect_probe", lambda **_kw: {"probes": []})

    assert _call("hub", {"action": "discover", "settle_sec": 0})["ok"] is True


# FIX 3 (profile merge)
def test_labelling_a_channel_with_merge_keeps_the_discovered_identity(monkeypatch, hub_session):
    # The loss as observed: discovery writes serial/key per channel, and a later
    # debug_profile set carrying only a label replaced the whole hub block --
    # taking with it the identity that resolves the channel.
    import mcp_server.server as server_module

    hub = hub_session.hub
    monkeypatch.setattr("mcp_server.tools.hub_tools.time", _NoSleep)
    monkeypatch.setattr(server_module, "detect_probe",
                        lambda **_kw: {"probes": [STLINK_A] if hub.data[2] else []})
    _call("hub", {"action": "discover", "apply": True, "settle_sec": 0})

    _call("debug_profile", {"action": "set", "merge": True,
                            "hub": {"map": {"2": {"label": "TC"}}}})

    entry = _call("debug_profile", {"action": "get"})["data"]["hub"]["map"]["2"]
    assert entry["serial"] == "0671FF565251"
    assert entry["label"] == "TC"


# FIX 3 (profile merge)
def test_without_merge_a_nested_block_replaces_so_a_stale_channel_can_be_dropped(monkeypatch, hub_session):
    import mcp_server.server as server_module

    hub = hub_session.hub
    monkeypatch.setattr("mcp_server.tools.hub_tools.time", _NoSleep)
    monkeypatch.setattr(server_module, "detect_probe",
                        lambda **_kw: {"probes": [STLINK_A] if hub.data[2] else []})
    _call("hub", {"action": "discover", "apply": True, "settle_sec": 0})

    _call("debug_profile", {"action": "set", "hub": {"map": {"3": {"label": "TC"}}}})

    # Re-cabled rig: replacing is how channel 2's now-wrong entry goes away.
    channel_map = _call("debug_profile", {"action": "get"})["data"]["hub"]["map"]
    assert list(channel_map) == ["3"]

