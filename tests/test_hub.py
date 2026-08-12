"""HubManager: connection, channel resolution, guard policy, and restore-on-close."""

import pytest

from mcp_server.hub import (
    HubGuardError,
    HubManager,
    HubPowerGuard,
    HubUnavailableError,
    _normalize_map,
)
from tests.conftest import FakeHub


def _manager(hub, spec=None):
    manager = HubManager(backend_factory=lambda **_kwargs: hub)
    if spec is not None:
        manager.configure(spec)
    return manager


# --------------------------------------------------------------- connection


def test_connect_is_lazy_and_cached(fake_hub):
    manager = _manager(fake_hub)
    assert fake_hub.calls == []  # configure/construct must not touch hardware

    manager.describe()
    first = fake_hub.calls.count("get_device_info")
    manager.describe()
    assert fake_hub.calls.count("get_device_info") == first == 1


def test_describe_reports_ports_channels_and_measurements(fake_hub):
    fake_hub.power[2] = 0
    fake_hub.data[3] = 0
    result = _manager(fake_hub).describe()

    assert result["available"] is True
    assert result["channels"] == [1, 2, 3, 4]
    assert result["adc"] is True
    ports = {port["channel"]: port for port in result["ports"]}
    assert ports[1]["power"] == "on"
    assert ports[2]["power"] == "off"
    assert ports[3]["data"] == "off"
    assert ports[1]["voltage_mv"] == 5012
    assert ports[1]["current_ma"] == 43


def test_describe_without_adc_reports_adc_false(fake_hub):
    fake_hub.adc = False
    result = _manager(fake_hub).describe()

    assert result["adc"] is False
    assert "voltage_mv" not in result["ports"][0]


def test_missing_device_raises_hub_unavailable():
    manager = HubManager(backend_factory=lambda **_kwargs: None)
    with pytest.raises(HubUnavailableError, match="no SmartUSBHub found"):
        manager.describe()


def test_dropped_link_reconnects_on_next_call(fake_hub):
    manager = _manager(fake_hub)
    manager.describe()

    fake_hub.disconnect_callback()  # the vendor's unexpected-disconnect hook
    fake_hub.connected = True
    manager.describe()

    assert fake_hub.calls.count("get_device_info") == 2


def test_configure_with_new_port_drops_the_old_connection(fake_hub):
    manager = _manager(fake_hub, {"port": "COM7"})
    manager.describe()

    manager.configure({"port": "COM9"})
    assert "disconnect" in fake_hub.calls


def test_is_available_never_raises():
    manager = HubManager(backend_factory=lambda **_kwargs: None)
    assert manager.is_available() is False


# ------------------------------------------------------------ channel_for


def test_channel_for_prefers_explicit_argument(fake_hub):
    manager = _manager(fake_hub, {"channel": 2})
    assert manager.channel_for(explicit=3) == (3, "argument")


def test_channel_for_falls_back_to_profile(fake_hub):
    manager = _manager(fake_hub, {"channel": 2})
    assert manager.channel_for(profile={"hub": {"channel": 2}}) == (2, "profile")


def test_channel_for_matches_map_by_probe_serial(fake_hub):
    spec = {"map": {1: {"serial": "AAA", "label": "l151"}, 3: {"serial": "BBB", "label": "l431"}}}
    manager = _manager(fake_hub, spec)
    assert manager.channel_for(probe_serial="BBB") == (3, "map_serial")


def test_channel_for_matches_map_by_session_label(fake_hub):
    spec = {"map": {1: {"label": "l151"}, 4: {"label": "u535"}}}
    manager = _manager(fake_hub, spec)
    assert manager.channel_for(session_id="u535") == (4, "map_label")


def test_channel_for_refuses_when_unmapped(fake_hub):
    manager = _manager(fake_hub, {})
    with pytest.raises(HubUnavailableError, match="hub channel unmapped"):
        manager.channel_for(session_id="whatever")


def test_channel_for_rejects_a_channel_the_hub_does_not_have(fake_hub):
    manager = _manager(fake_hub, {})
    manager.describe()  # learn the real channel list first
    with pytest.raises(HubUnavailableError, match="does not exist"):
        manager.channel_for(explicit=9)


def test_channel_for_rejects_non_integer_channels(fake_hub):
    manager = _manager(fake_hub, {})
    with pytest.raises(HubUnavailableError, match="must be an integer"):
        manager.channel_for(explicit="2")


def test_normalize_map_accepts_string_keys_from_yaml():
    assert _normalize_map({"2": {"label": "l431"}}) == {2: {"label": "l431"}}
    assert _normalize_map({3: "u535"}) == {3: {"label": "u535"}}
    assert _normalize_map("nonsense") == {}


# -------------------------------------------------------------------- guard


def test_confirm_mode_blocks_without_confirmation(fake_hub):
    manager = _manager(fake_hub, {"channel": 1})
    with pytest.raises(HubGuardError, match="confirm=true"):
        manager.power(1, "off")
    assert fake_hub.power[1] == 1  # nothing was switched


def test_confirm_mode_applies_with_confirmation(fake_hub):
    manager = _manager(fake_hub, {"channel": 1})
    result = manager.power(1, "off", confirm=True)

    assert result == {"channel": 1, "action": "power_off", "applied": True}
    assert fake_hub.power[1] == 0


def test_allow_mode_needs_no_confirmation(fake_hub):
    manager = _manager(fake_hub, {"channel": 1, "guard": "allow"})
    manager.power(1, "off")
    assert fake_hub.power[1] == 0


def test_dry_run_mode_simulates_without_switching(fake_hub):
    manager = _manager(fake_hub, {"channel": 1, "guard": "dry_run"})
    result = manager.power(1, "off", confirm=True)

    assert result["simulated"] is True
    assert result["applied"] is None
    assert fake_hub.power[1] == 1


def test_live_session_forces_confirmation_even_in_allow_mode(fake_hub):
    manager = _manager(fake_hub, {"channel": 1, "guard": "allow"})
    with pytest.raises(HubGuardError, match="live GDB server"):
        manager.power(1, "off", live_session="default")
    assert fake_hub.power[1] == 1


def test_live_session_can_still_be_overridden_with_confirm(fake_hub):
    manager = _manager(fake_hub, {"channel": 1, "guard": "allow"})
    manager.power(1, "off", confirm=True, live_session="default")
    assert fake_hub.power[1] == 0


def test_guard_rejects_an_unknown_mode():
    with pytest.raises(ValueError, match="mode must be one of"):
        HubPowerGuard().set_policy("yolo")


def test_audit_log_records_blocked_and_applied_actions(fake_hub):
    manager = _manager(fake_hub, {"channel": 1})
    with pytest.raises(HubGuardError):
        manager.power(1, "off")
    manager.power(1, "off", confirm=True)

    log = manager.guard.get_audit_log()
    assert [entry["decision"] for entry in log] == ["blocked", "apply"]
    assert log[-1]["acknowledged"] is True


def test_describe_surfaces_recent_actions(fake_hub):
    manager = _manager(fake_hub, {"channel": 1, "guard": "allow"})
    manager.power(1, "off")
    assert manager.describe()["recent_actions"][-1]["action"] == "power_off"


# ---------------------------------------------------------------- interlock


def test_interlock_mode_refuses_every_power_write():
    hub = FakeHub(interlock=True)
    manager = _manager(hub, {"channel": 1, "guard": "allow"})
    with pytest.raises(HubGuardError, match="interlock mode"):
        manager.power(1, "off")
    assert hub.power[1] == 1


def test_interlock_mode_still_allows_data_line_control():
    hub = FakeHub(interlock=True)
    manager = _manager(hub, {"channel": 1, "guard": "allow"})
    manager.data(1, "off")
    assert hub.data[1] == 0


# --------------------------------------------------------------- data lines


def test_exclusive_data_leaves_only_the_named_channel_connected(fake_hub):
    manager = _manager(fake_hub, {"channel": 2, "guard": "allow"})
    result = manager.data(2, "on", exclusive=True)

    assert result["exclusive"] is True
    assert sorted(result["data_off_channels"]) == [1, 3, 4]
    assert fake_hub.data == {1: 0, 2: 1, 3: 0, 4: 0}


def test_exclusive_is_ignored_when_turning_a_channel_off(fake_hub):
    manager = _manager(fake_hub, {"channel": 2, "guard": "allow"})
    result = manager.data(2, "off", exclusive=True)

    assert "data_off_channels" not in result
    assert fake_hub.data[1] == 1


def test_unacknowledged_switch_raises_rather_than_reporting_success(fake_hub):
    manager = _manager(fake_hub, {"channel": 1, "guard": "allow"})
    fake_hub.ack = False
    with pytest.raises(HubUnavailableError, match="did not acknowledge"):
        manager.power(1, "off")


def test_invalid_state_is_rejected(fake_hub):
    manager = _manager(fake_hub, {"channel": 1, "guard": "allow"})
    with pytest.raises(ValueError, match="state must be"):
        manager.power(1, "toggle")


# -------------------------------------------------------------------- close


def test_close_repowers_channels_this_process_turned_off(fake_hub):
    manager = _manager(fake_hub, {"channel": 1, "guard": "allow"})
    manager.power(1, "off")
    manager.power(3, "off")

    assert manager.close()["restored_channels"] == [1, 3]
    assert fake_hub.power[1] == 1
    assert fake_hub.power[3] == 1


def test_close_does_not_repower_a_channel_the_caller_turned_back_on(fake_hub):
    manager = _manager(fake_hub, {"channel": 1, "guard": "allow"})
    manager.power(1, "off")
    manager.power(1, "on")

    assert manager.close()["restored_channels"] == []


def test_close_leaves_untouched_channels_alone(fake_hub):
    manager = _manager(fake_hub, {"guard": "allow"})
    manager.describe()

    assert manager.close()["restored_channels"] == []
    assert fake_hub.power == {1: 1, 2: 1, 3: 1, 4: 1}


def test_close_with_restore_power_false_leaves_ports_dark(fake_hub):
    manager = _manager(fake_hub, {"channel": 1, "guard": "allow"})
    manager.power(1, "off")

    assert manager.close(restore_power=False)["restored_channels"] == []
    assert fake_hub.power[1] == 0


def test_close_is_idempotent(fake_hub):
    manager = _manager(fake_hub, {"guard": "allow"})
    manager.describe()
    manager.close()
    assert manager.close()["restored_channels"] == []


# ------------------------------------------------------------- degradation


def test_a_failing_status_read_does_not_break_describe(fake_hub):
    fake_hub.fail_on["get_channel_power_status"] = RuntimeError("no ACK")
    ports = _manager(fake_hub).describe()["ports"]
    assert all(port["power"] is None for port in ports)


def test_channel_names_are_optional(fake_hub):
    fake_hub.fail_on["get_channel_name"] = RuntimeError("unsupported firmware")
    assert all("name" not in port for port in _manager(fake_hub).describe()["ports"])


def test_channel_list_falls_back_to_max_channels_from_device_info(fake_hub):
    fake_hub.fail_on["get_channels"] = RuntimeError("unknown product type")
    assert _manager(fake_hub).describe()["channels"] == [1, 2, 3, 4]


# FIX 4 (unmapped-channel messages)
def test_an_empty_session_profile_is_named_as_a_missing_per_session_load(fake_hub):
    # The live failure: the rack config was loaded into the DEFAULT session, and
    # session "TC" -- which never loaded it -- was told to pin a channel instead,
    # i.e. to bypass the very map it was missing.
    manager = _manager(fake_hub, {"map": {1: {"label": "default"}}})

    with pytest.raises(HubUnavailableError) as excinfo:
        manager.channel_for(profile={}, session_id="TC")

    message = str(excinfo.value)
    assert "hub channel unmapped" in message
    assert "EMPTY" in message
    assert 'debug_config(action=load, path=..., session="TC")' in message
    assert 'debug_profile(action=set, hub={"channel": N})' not in message
    assert excinfo.value.next_actions[0] == 'debug_config(action=load, path=..., session="TC")'


# FIX 4 (unmapped-channel messages)
def test_a_profile_without_a_hub_block_is_not_reported_as_a_broken_map(fake_hub):
    manager = _manager(fake_hub, {})

    with pytest.raises(HubUnavailableError, match='has no "hub" block'):
        manager.channel_for(profile={"mcu": "STM32L151"}, session_id="TC")


# FIX 4 (unmapped-channel messages)
def test_a_label_miss_names_the_labels_defined_and_the_session_tried(fake_hub):
    # Otherwise this is a guessing game: the rule is an exact match against the
    # session name, so the two things needed to see it are the labels and the name.
    spec = {"map": {1: {"label": "l151"}, 4: {"label": "u535"}}}
    manager = _manager(fake_hub, spec)

    with pytest.raises(HubUnavailableError) as excinfo:
        manager.channel_for(profile={"hub": spec}, session_id="TC")

    message = str(excinfo.value)
    assert "['l151', 'u535']" in message
    assert '"TC"' in message
    assert "EXACTLY" in message


# FIX 4 (unmapped-channel messages)
def test_a_serial_miss_names_the_mapped_serials_and_the_probe_serial(fake_hub):
    spec = {"map": {1: {"serial": "AAA"}, 3: {"serial": "BBB"}}}
    manager = _manager(fake_hub, spec)

    with pytest.raises(HubUnavailableError) as excinfo:
        manager.channel_for(profile={"hub": spec}, session_id="TC", probe_serial="CCC")

    message = str(excinfo.value)
    assert "['AAA', 'BBB']" in message
    assert '"CCC"' in message


# FIX 4 (unmapped-channel messages)
def test_a_hub_block_with_nothing_to_select_on_points_at_discovery(fake_hub):
    manager = _manager(fake_hub, {})

    with pytest.raises(HubUnavailableError) as excinfo:
        manager.channel_for(profile={"hub": {"guard": "allow"}}, session_id="TC")

    assert "hub(action=discover, apply=true)" in excinfo.value.next_actions



def test_a_label_miss_never_names_one_label_as_the_one_to_use(fake_hub):
    # It used to end with `session="{labels[0]}"` -- the alphabetically first label in
    # the whole rack. This code cannot see which channel the caller's board is on, so
    # naming one is a coin flip, and acting on it selects (and power-cycles) whichever
    # board that label belongs to. Same wrong-board hazard the rest of this work is about.
    spec = {"map": {1: {"label": "aaa"}, 2: {"label": "zzz"}}}
    manager = _manager(fake_hub, spec)

    with pytest.raises(HubUnavailableError) as excinfo:
        manager.channel_for(profile={"hub": spec}, session_id="TC")

    message = str(excinfo.value)
    assert 'session="aaa"' not in message
    assert 'session="zzz"' not in message
    # It must still name every candidate, so the human can pick the right one.
    assert "['aaa', 'zzz']" in message
    assert "power-cycle" in message
