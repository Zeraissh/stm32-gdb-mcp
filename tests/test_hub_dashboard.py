"""hub(action=dashboard): sampler cache, event timeline, HTTP surface, and tool wiring."""

import asyncio
import http.client
import json

import pytest

from mcp_server.hub_dashboard import HubDashboard, hub_dashboard
from mcp_server.server import handle_call_tool


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def dashboard():
    board = HubDashboard(clock=_Clock())
    yield board
    board.stop()


@pytest.fixture(autouse=True)
def _stop_global_dashboard():
    yield
    hub_dashboard.stop()


def _get(url_port, path, host="127.0.0.1"):
    conn = http.client.HTTPConnection("127.0.0.1", url_port, timeout=5)
    conn.request("GET", path, headers={"Host": f"{host}:{url_port}"})
    res = conn.getresponse()
    body = res.read()
    conn.close()
    return res.status, res.getheader("Content-Type"), body


# ------------------------------------------------------------------ sampler


def test_poll_records_history_per_channel(dashboard, hub_manager, fake_hub):
    dashboard._manager = hub_manager
    dashboard.poll_once()
    dashboard._clock.t += 1
    dashboard.poll_once()

    state = dashboard.state()
    assert state["connected"] is True
    assert state["hub"]["channels"] == [1, 2, 3, 4]
    assert len(state["history"]["1"]) == 2
    t, voltage, current, power, data = state["history"]["1"][-1]
    assert (t, power, data) == (1001.0, 1, 1)
    assert voltage == fake_hub.voltage_mv and current == fake_hub.current_ma


def test_power_transition_and_audit_entries_become_events(dashboard, hub_manager, fake_hub):
    dashboard._manager = hub_manager
    dashboard.poll_once()
    hub_manager.power(3, "off", confirm=True)
    dashboard.poll_once()

    events = dashboard.state()["events"]
    kinds = [(e["kind"], e["channel"]) for e in events]
    assert ("power", 3) in kinds
    assert ("audit", 3) in kinds
    # The history marks the off sample, which is what the page shades.
    assert dashboard.state()["history"]["3"][-1][3] == 0


def test_audit_entries_are_reported_once(dashboard, hub_manager):
    dashboard._manager = hub_manager
    hub_manager.power(2, "off", confirm=True)
    dashboard.poll_once()
    dashboard.poll_once()

    audits = [e for e in dashboard.state()["events"] if e["kind"] == "audit"]
    assert len(audits) == 1


def test_hub_failure_is_shown_not_raised_and_recovery_is_logged(dashboard, hub_manager):
    dashboard._manager = hub_manager
    dashboard.poll_once()

    class Broken:
        guard = hub_manager.guard

        def describe(self):
            raise RuntimeError("hub unavailable: link down")

    dashboard._manager = Broken()
    dashboard.poll_once()
    state = dashboard.state()
    assert state["connected"] is False
    assert "link down" in state["error"]
    # The last good frame stays available for the page to show, dimmed.
    assert state["hub"]["channels"] == [1, 2, 3, 4]

    dashboard._manager = hub_manager
    dashboard.poll_once()
    kinds = [e["kind"] for e in dashboard.state()["events"]]
    assert kinds[-2:] == ["error", "recovered"]
    assert dashboard.state()["error"] is None


# ------------------------------------------------------------------ http


def test_http_serves_page_and_state(dashboard, hub_manager):
    info = dashboard.start(hub_manager, port=0, interval_sec=0.2)
    assert info["running"] is True
    # Not "localhost": on Windows that costs a ~2 s IPv6 retry per request.
    assert info["url"].startswith("http://127.0.0.1:")

    status, ctype, body = _get(info["port"], "/")
    assert status == 200 and ctype.startswith("text/html")
    assert b"/api/state" in body

    status, ctype, body = _get(info["port"], "/api/state")
    assert status == 200 and ctype == "application/json"
    assert "history" in json.loads(body)

    assert _get(info["port"], "/nope")[0] == 404


def test_http_rejects_foreign_host_header(dashboard, hub_manager):
    info = dashboard.start(hub_manager, port=0, interval_sec=0.2)
    assert _get(info["port"], "/api/state", host="evil.example")[0] == 403


def test_only_loopback_binds(dashboard, hub_manager):
    with pytest.raises(ValueError):
        dashboard.start(hub_manager, host="0.0.0.0", port=0)


def test_restart_on_same_port_keeps_server(dashboard, hub_manager):
    first = dashboard.start(hub_manager, port=0, interval_sec=0.2)
    again = dashboard.start(hub_manager, port=first["port"], interval_sec=0.5)
    assert again["port"] == first["port"]
    assert again["interval_sec"] == 0.5

    assert dashboard.stop() == {"running": False, "stopped": True}
    assert dashboard.stop() == {"running": False, "stopped": False}


# ------------------------------------------------------------------ tool


def _call(arguments):
    return json.loads(asyncio.run(handle_call_tool("hub", arguments))[0].text)


def test_tool_starts_and_stops_the_dashboard(hub_session):
    asyncio.run(handle_call_tool("debug_profile", {"action": "set", "hub": {"channel": 2}}))
    payload = _call({"action": "dashboard", "port": 0, "interval_sec": 0.2})

    assert payload["ok"] is True
    port = payload["data"]["port"]
    assert payload["data"]["url"] == f"http://127.0.0.1:{port}/"

    hub_dashboard.poll_once()
    rows = hub_dashboard.state()["sessions"]
    default = next(row for row in rows if row["session"] == "default")
    assert default["channel"] == 2
    assert default["channel_source"] == "profile"

    stopped = _call({"action": "dashboard", "state": "off"})
    assert stopped["ok"] is True
    assert stopped["data"]["stopped"] is True
    assert hub_dashboard.running is False


def test_unmapped_session_is_listed_without_a_channel(hub_session):
    payload = _call({"action": "dashboard", "port": 0})
    assert payload["ok"] is True

    hub_dashboard.poll_once()
    default = next(row for row in hub_dashboard.state()["sessions"] if row["session"] == "default")
    assert default["channel"] is None
    assert "unmapped" in default["channel_error"]


def test_tool_rejects_a_bad_state(hub_session):
    payload = _call({"action": "dashboard", "state": "maybe"})
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_argument"
