"""The escalation ladder: rung order, the escalation gate, and no-hub equivalence.

Every test here runs on an injected clock -- nothing sleeps.
"""

import pytest

from mcp_server.hub_recovery import (
    HUB_RECOVERABLE_CODES,
    HubRecoveryError,
    escalate,
)


class FakeClock:
    """Injectable sleep/monotonic pair; records what was waited for."""

    def __init__(self):
        self.now = 0.0
        self.slept = []

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds

    def monotonic(self):
        self.now += 0.001
        return self.now


class RecordingHub:
    """Records ladder actions; power_cycle reports a collapsed rail by default."""

    def __init__(self, off_voltage_mv=12):
        self.actions = []
        self.off_voltage_mv = off_voltage_mv
        self.fail_on = {}

    def data(self, channel, state, confirm=False):
        self.actions.append(("data", channel, state))
        if "data" in self.fail_on:
            raise self.fail_on["data"]
        return {"channel": channel, "applied": True}

    def power_cycle(self, channel, off_ms=400, settle_ms=1500, confirm=False,
                    sleep=None, monotonic=None):
        self.actions.append(("power_cycle", channel, off_ms))
        if "power_cycle" in self.fail_on:
            raise self.fail_on["power_cycle"]
        return {
            "channel": channel,
            "off_ms": off_ms,
            "measured_off_voltage_mv": self.off_voltage_mv,
            "browned_out": self.off_voltage_mv < 500,
        }


def _failing(times, message="open failed", then=3333):
    """A start thunk that raises `times` times, then returns `then`."""
    state = {"n": 0}

    def start():
        state["n"] += 1
        if state["n"] <= times:
            raise RuntimeError(message)
        return then

    start.calls = state
    return start


# ------------------------------------------------------ no hub == old behaviour


def test_without_a_hub_a_first_try_success_reports_only_the_soft_rung():
    clock = FakeClock()
    result, steps = escalate(lambda: 3333, sleep=clock.sleep, monotonic=clock.monotonic)

    assert result == 3333
    assert [s["rung"] for s in steps] == ["soft"]
    assert steps[0]["ok"] is True
    assert clock.slept == []


def test_without_a_hub_the_soft_rung_retries_exactly_three_times():
    clock = FakeClock()
    start = _failing(2)

    result, steps = escalate(start, sleep=clock.sleep, monotonic=clock.monotonic)

    assert result == 3333
    assert start.calls["n"] == 3  # same attempts=3 as the pre-hub retry_call
    assert clock.slept == [0.8, 1.6]  # same backoff_base=0.8 doubling
    assert [s["rung"] for s in steps] == ["soft"]


def test_without_a_hub_exhaustion_raises_with_the_original_cause():
    clock = FakeClock()
    with pytest.raises(HubRecoveryError) as excinfo:
        escalate(_failing(99), sleep=clock.sleep, monotonic=clock.monotonic)

    assert isinstance(excinfo.value.cause, RuntimeError)
    assert [s["rung"] for s in excinfo.value.steps] == ["soft"]


def test_a_hub_without_a_channel_does_not_escalate():
    clock = FakeClock()
    hub = RecordingHub()
    with pytest.raises(HubRecoveryError):
        escalate(_failing(99), hub, None, sleep=clock.sleep, monotonic=clock.monotonic)

    assert hub.actions == []


# ----------------------------------------------------------------- rung order


def test_data_toggle_is_tried_before_cutting_power():
    clock = FakeClock()
    hub = RecordingHub()
    # 3 soft attempts fail, then the data toggle's first retry succeeds.
    result, steps = escalate(_failing(3), hub, 2, sleep=clock.sleep, monotonic=clock.monotonic)

    assert result == 3333
    assert hub.actions == [("data", 2, "off"), ("data", 2, "on")]
    assert [s["rung"] for s in steps] == ["soft", "data_toggle"]
    assert steps[-1]["ok"] is True


def test_power_cycle_follows_a_failed_data_toggle():
    clock = FakeClock()
    hub = RecordingHub()
    # 3 soft + 2 data_toggle attempts fail; the power_cycle rung then succeeds.
    result, steps = escalate(_failing(5), hub, 1, sleep=clock.sleep, monotonic=clock.monotonic)

    assert result == 3333
    assert [s["rung"] for s in steps] == ["soft", "data_toggle", "power_cycle"]
    assert ("power_cycle", 1, 400) in hub.actions
    assert steps[-1]["measured_off_voltage_mv"] == 12
    assert steps[-1]["browned_out"] is True


def test_the_long_rung_is_only_reached_with_deep():
    clock = FakeClock()
    hub = RecordingHub()
    with pytest.raises(HubRecoveryError):
        escalate(_failing(99), hub, 1, sleep=clock.sleep, monotonic=clock.monotonic)
    assert not any(action[0] == "power_cycle" and action[2] == 2000 for action in hub.actions)

    hub2 = RecordingHub()
    with pytest.raises(HubRecoveryError) as excinfo:
        escalate(_failing(99), hub2, 1, deep=True, sleep=clock.sleep, monotonic=clock.monotonic)
    assert ("power_cycle", 1, 2000) in hub2.actions
    assert [s["rung"] for s in excinfo.value.steps] == [
        "soft", "data_toggle", "power_cycle", "power_cycle_long"]


def test_a_rung_that_cannot_run_falls_through_to_the_next():
    clock = FakeClock()
    hub = RecordingHub()
    hub.fail_on["data"] = RuntimeError("hub did not acknowledge")

    result, steps = escalate(_failing(3), hub, 1, sleep=clock.sleep, monotonic=clock.monotonic)

    assert result == 3333
    assert steps[1]["rung"] == "data_toggle" and steps[1]["ok"] is False
    assert steps[2]["rung"] == "power_cycle" and steps[2]["ok"] is True


def test_a_non_collapsing_rail_is_reported_rather_than_hidden():
    clock = FakeClock()
    hub = RecordingHub(off_voltage_mv=3300)  # a second supply holds the board up
    _result, steps = escalate(_failing(5), hub, 1, sleep=clock.sleep, monotonic=clock.monotonic)

    cycle = [s for s in steps if s["rung"] == "power_cycle"][0]
    assert cycle["measured_off_voltage_mv"] == 3300
    assert cycle["browned_out"] is False


# ------------------------------------------------------------ escalation gate


GATED_MESSAGES = {
    "probe_busy": "open failed",
    "probe_unavailable": "libusb no device found",
    "target_unresponsive": "timed out waiting for target halt",
    "connection_lost": "Remote communication error",
    "target_unreachable": "Error: init mode failed",
}


def test_the_gate_set_and_the_sample_messages_stay_in_sync():
    # Guards against a taxonomy edit silently making a case below untested.
    from mcp_server.error_taxonomy import classify_error

    assert set(GATED_MESSAGES) == set(HUB_RECOVERABLE_CODES)
    for code, message in GATED_MESSAGES.items():
        assert classify_error(message)["code"] == code


@pytest.mark.parametrize("code", sorted(HUB_RECOVERABLE_CODES))
def test_every_gated_code_reaches_the_hub(code):
    # The assertion is that escalation HAPPENED, not that it succeeded: half these
    # codes are classified non-retryable, so retry_call gives each rung a single
    # attempt and the ladder can legitimately run out.
    clock = FakeClock()
    hub = RecordingHub()
    try:
        escalate(_failing(99, GATED_MESSAGES[code]), hub, 1,
                 sleep=clock.sleep, monotonic=clock.monotonic)
    except HubRecoveryError:
        pass

    assert hub.actions, f"{code} should have escalated to the hub"
    assert hub.actions[0][0] == "data", f"{code} should try the data toggle first"


def test_probe_locked_never_escalates():
    # The lock file is held by a LIVE pid; cutting power cannot delete it, so the
    # next rung would fail identically after browning out a board for nothing.
    clock = FakeClock()
    hub = RecordingHub()
    with pytest.raises(HubRecoveryError) as excinfo:
        escalate(_failing(99, "probe 'stlink' held by PID 4242"), hub, 1,
                 sleep=clock.sleep, monotonic=clock.monotonic)

    assert hub.actions == []
    assert excinfo.value.steps[-1]["rung"] == "escalation_skipped"
    assert "not cured by re-enumerating" in excinfo.value.steps[-1]["reason"]


def test_probe_locked_is_absent_from_the_gate_set():
    assert "probe_locked" not in HUB_RECOVERABLE_CODES


def test_a_non_probe_failure_does_not_touch_the_hub():
    clock = FakeClock()
    hub = RecordingHub()
    with pytest.raises(HubRecoveryError):
        escalate(_failing(99, "No symbol table is loaded"), hub, 1,
                 sleep=clock.sleep, monotonic=clock.monotonic)

    assert hub.actions == []
