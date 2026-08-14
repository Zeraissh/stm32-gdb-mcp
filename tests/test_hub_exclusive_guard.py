"""Exclusive isolation goes through the same guard, audit and lock as any switch.

Three defects, all from the neighbour loop talking to the backend directly instead
of routing through _switch:

1. No guard on the neighbours, so an isolation could cut the data line under
   ANOTHER session's live GDB server with no confirm and no block.
2. No audit entry for them, so describe()'s recent_actions could not even tell you
   what had been disconnected -- or what to put back.
3. A neighbour that failed to switch was skipped silently and the call still
   reported success with a shorter list. More than one probe stays enumerated, and
   start_debug_session's auto-select then picks whichever.

Plus: in dry_run the whole exclusive leg was skipped while still returning ok, so a
caller that checked only ok=true went on to start a session on an un-isolated rack.
"""
import asyncio
import json

from conftest import FakeHub

from mcp_server.hub import HubManager


def _manager(hub=None, guard_mode="confirm"):
    hub = hub or FakeHub()
    mgr = HubManager(backend_factory=lambda exclude_ports=None: hub)
    mgr._idle_release_sec = 0
    mgr.configure({"port": "COM7", "guard": guard_mode})
    return mgr, hub


def test_every_disconnected_neighbour_lands_in_the_audit_trail():
    # Without this, describe()'s recent_actions shows only the target channel and
    # nothing records which boards were taken off the bus.
    mgr, hub = _manager()

    mgr.data(1, "on", confirm=True, exclusive=True)

    audited = [(e["action"], e["channel"]) for e in mgr.guard.get_audit_log()]
    for channel in (2, 3, 4):
        assert ("data_off", channel) in audited, f"CH{channel} was cut without a record"


def test_a_neighbour_that_cannot_be_switched_makes_the_call_fail_not_succeed():
    class StubbornHub(FakeHub):
        def set_channel_usb2_dataline(self, *channels, state):
            if channels and channels[0] == 3 and state == 0:
                return False          # the hub refuses this one
            return super().set_channel_usb2_dataline(*channels, state=state)

    mgr, _ = _manager(StubbornHub())

    result = mgr.data(1, "on", confirm=True, exclusive=True)

    assert result["exclusive"] is False
    assert [f["channel"] for f in result["exclusive_incomplete"]] == [3]
    assert 3 not in result["data_off_channels"]


def test_the_tool_reports_an_incomplete_isolation_as_an_error(monkeypatch):
    # The caller's next move is start_debug_session, which is only safe with exactly
    # one probe enumerated. ok=true here is how you end up on the wrong board.
    import mcp_server.server as server_module
    from mcp_server.server import handle_call_tool

    class StubbornHub(FakeHub):
        def set_channel_usb2_dataline(self, *channels, state):
            if channels and channels[0] == 3 and state == 0:
                return False
            return super().set_channel_usb2_dataline(*channels, state=state)

    mgr, _ = _manager(StubbornHub())
    monkeypatch.setattr(server_module, "hub_manager", mgr)

    payload = json.loads(asyncio.run(handle_call_tool(
        "hub", {"action": "data", "state": "on", "channel": 1,
                "exclusive": True, "confirm": True})).content[0].text)

    assert payload["ok"] is False
    assert payload["error"]["code"] == "hub_isolation_incomplete"
    assert "[3]" in payload["error"]["message"]


def test_dry_run_says_nothing_was_isolated_instead_of_looking_like_success():
    mgr, hub = _manager(guard_mode="dry_run")
    before = dict(hub.data)

    result = mgr.data(1, "on", confirm=True, exclusive=True)

    assert result["exclusive"] is False
    assert "exclusive_skipped" in result
    assert hub.data == before, "dry_run must not switch anything"


def test_a_neighbour_switch_is_refused_when_it_would_cut_a_live_session():
    # The guard's live-session rule now covers the neighbours too. Previously they
    # bypassed it entirely, so an isolation could drop the USB link under another
    # session's running OpenOCD without asking.
    mgr, hub = _manager(guard_mode="allow")

    # allow mode still forces confirmation when a GDB server is live on the channel.
    decision = mgr.guard.evaluate("data_off", 3, confirm=False, live_session="other")

    assert decision["action"] == "blocked", \
        "a live session must block a data cut even in allow mode"


def test_isolation_still_works_in_the_ordinary_case():
    # The fix must not make the documented rack workflow harder.
    mgr, hub = _manager()

    result = mgr.data(1, "on", confirm=True, exclusive=True)

    assert result["exclusive"] is True
    assert result["data_off_channels"] == [2, 3, 4]
    assert "exclusive_incomplete" not in result
    assert hub.data[1] == 1 and all(hub.data[c] == 0 for c in (2, 3, 4))
