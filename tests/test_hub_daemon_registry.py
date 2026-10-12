"""Hub daemon, phase 2: session registry, cross-server interlock, bus generation,
bounded write queue.

Same harness as test_hub_daemon: an in-process daemon over FakeHub, reached
through real loopback HTTP by one RemoteHubManager per simulated MCP server.
"""

import asyncio
import json
import threading
import time
import types

import pytest

import mcp_server.hub_daemon as daemon_module
import mcp_server.hub_remote as remote_module
from mcp_server import openocd_config
from mcp_server.hub import HubBusyError, HubGuardError
from mcp_server.hub_daemon import HubDaemon
from mcp_server.hub_remote import RemoteHubManager

TOKEN = "test-token"


@pytest.fixture
def daemon(hub_manager):
    owner = HubDaemon(manager=hub_manager, token=TOKEN, idle_exit_sec=0)
    owner.start(port=0, interval_sec=5)
    yield owner
    owner.stop()


class _Profile:
    def __init__(self, data):
        self.data = data

    def get(self):
        return dict(self.data)


class _Server:
    def __init__(self, alive):
        self.alive = alive

    def is_alive(self):
        return self.alive


def _bundle(session, hub=None, alive=True, mcu="STM32L431"):
    profile = {"mcu": mcu}
    if hub is not None:
        profile["hub"] = hub
    return types.SimpleNamespace(id=session, debug_profile=_Profile(profile), gdb_manager=_Server(alive))


def _server(daemon, name, guard=None, bundles=None, **spec):
    """One simulated MCP server: a RemoteHubManager plus its session bundles."""
    client = RemoteHubManager(connector=lambda spawn=True: (daemon.info["url"], TOKEN), client_name=name)
    if guard is not None:
        spec["guard"] = guard
    client.configure(spec)
    if bundles is not None:
        client.set_session_provider(lambda: bundles)
    return client


@pytest.fixture(autouse=True)
def _stop_heartbeats(monkeypatch):
    # Heartbeats are exercised explicitly below; elsewhere keep them out of the way.
    monkeypatch.setattr(remote_module, "HEARTBEAT_SEC", 3600)


# ------------------------------------------------------------------ registry


def test_sessions_with_a_hub_block_are_registered(daemon):
    bench = _server(daemon, "bench-B", bundles=[_bundle("TC", hub={"channel": 2}),
                                                _bundle("scratch", hub=None)])
    bench.report_sessions()

    rows = daemon._session_rows()
    assert [(r["session"], r["channel"], r["server_alive"], r["client"]) for r in rows] == \
        [("TC", 2, True, "bench-B")]


def test_unchanged_sessions_are_not_re_pushed(daemon):
    bench = _server(daemon, "bench-B", bundles=[_bundle("TC", hub={"channel": 2})])
    bench.report_sessions()
    calls = daemon._clients[bench.client_id]["calls"]
    bench.report_sessions()
    assert daemon._clients[bench.client_id]["calls"] == calls


def test_heartbeat_keeps_registering_without_tool_calls(daemon, monkeypatch):
    monkeypatch.setattr(remote_module, "HEARTBEAT_SEC", 0.05)
    bundles = [_bundle("TC", hub={"channel": 2}, alive=False)]
    bench = _server(daemon, "bench-B", bundles=bundles)
    bench.report_sessions()
    try:
        # The GDB server comes up without any hub call announcing it.
        bundles[0].gdb_manager.alive = True
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not daemon._live_on(2):
            time.sleep(0.05)
        assert daemon._live_on(2) == "TC @ bench-B"
    finally:
        bench.close()


def test_release_and_dead_clients_drop_their_sessions(daemon, monkeypatch):
    gone = _server(daemon, "gone", bundles=[_bundle("TC", hub={"channel": 2})])
    gone.report_sessions()
    closing = _server(daemon, "closing", bundles=[_bundle("AGS", hub={"channel": 3})])
    closing.report_sessions()

    closing.close()
    assert daemon._live_on(3) is None

    monkeypatch.setattr(daemon_module, "_pid_alive", lambda pid: False)
    assert daemon._live_on(2) is None  # its GDB servers died with it
    daemon.maintain()
    assert gone.client_id not in daemon._sessions


def test_a_silent_server_still_counts_as_live_but_is_marked_stale(hub_manager):
    clock = [1000.0]
    owner = HubDaemon(manager=hub_manager, token=TOKEN, idle_exit_sec=0, clock=lambda: clock[0])
    owner.start(port=0, interval_sec=5)
    try:
        _server(owner, "hung", bundles=[_bundle("TC", hub={"channel": 2})]).report_sessions()
        clock[0] += 60
        row = owner._session_rows()[0]
        assert row["stale"] is True
        assert owner._live_on(2) == "TC @ hung"
    finally:
        owner.stop()


# ------------------------------------------------------------------ interlock


def test_a_cut_on_another_servers_live_channel_needs_confirm(daemon, fake_hub):
    _server(daemon, "bench-B", bundles=[_bundle("TC", hub={"channel": 2})]).report_sessions()
    ci = _server(daemon, "ci", guard="allow")

    with pytest.raises(HubGuardError, match=r"TC @ bench-B.*channel 2"):
        ci.power(2, "off")
    assert fake_hub.power[2] == 1
    with pytest.raises(HubGuardError, match="TC @ bench-B"):
        ci.power_cycle(2, off_ms=0, settle_ms=0)

    assert ci.power(2, "off", confirm=True)["applied"] is True


def test_turning_a_port_on_is_never_blocked_by_the_registry(daemon, fake_hub):
    fake_hub.power[2] = 0
    _server(daemon, "bench-B", bundles=[_bundle("TC", hub={"channel": 2})]).report_sessions()
    assert _server(daemon, "ci", guard="allow").power(2, "on")["applied"] is True


def test_exclusive_isolation_skips_a_neighbour_live_on_another_server(daemon, fake_hub):
    _server(daemon, "bench-B", bundles=[_bundle("TC", hub={"channel": 2})]).report_sessions()
    result = _server(daemon, "ci", guard="allow").data(1, "on", exclusive=True)

    assert result["exclusive"] is False
    assert [entry["channel"] for entry in result["exclusive_incomplete"]] == [2]
    assert fake_hub.data == {1: 1, 2: 1, 3: 0, 4: 0}


def test_idle_sessions_do_not_block(daemon, fake_hub):
    _server(daemon, "bench-B", bundles=[_bundle("TC", hub={"channel": 2}, alive=False)]).report_sessions()
    assert _server(daemon, "ci", guard="allow").power(2, "off")["applied"] is True


# ------------------------------------------------------------------ generation


def test_generation_moves_only_on_real_switches(daemon):
    client = _server(daemon, "A")
    start = daemon.generation
    with pytest.raises(HubGuardError):
        client.power(1, "off")  # blocked: confirm mode
    assert daemon.generation == start

    client.power(1, "off", confirm=True)
    assert client.bus_generation() == start + 1
    client.close()  # restores channel 1
    assert daemon.generation == start + 2


def test_bus_generation_never_spawns(daemon):
    calls = []
    client = RemoteHubManager(connector=lambda spawn=True: calls.append(spawn))
    assert client.bus_generation() is None
    assert calls == []


def test_probe_cache_is_dropped_when_another_server_switches(monkeypatch):
    class Runner:
        calls = 0

        def __call__(self, *args, **kwargs):
            Runner.calls += 1
            return types.SimpleNamespace(returncode=0, stdout="[]", stderr="")

    generation = [5]
    monkeypatch.setattr(openocd_config.subprocess, "run", Runner())
    monkeypatch.setattr(openocd_config, "_cache_generation", lambda: generation[0])
    openocd_config.invalidate_probe_cache()
    try:
        openocd_config.detect_probe(platform_name="Windows")
        openocd_config.detect_probe(platform_name="Windows")
        assert Runner.calls == 1  # same generation: cached

        generation[0] = 6  # some other server switched a port
        openocd_config.detect_probe(platform_name="Windows")
        assert Runner.calls == 2
    finally:
        openocd_config.invalidate_probe_cache()


# ------------------------------------------------------------------ queue


def test_a_write_that_waits_too_long_is_refused_and_names_the_holder(daemon, fake_hub):
    daemon._busy = {"client": "bench-B", "method": "power_cycle", "channel": 3,
                    "since": time.time(), "_t0": time.monotonic()}
    held = threading.Event()
    release = threading.Event()

    def hold():
        with daemon._write_lock:
            held.set()
            release.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    held.wait(2)
    try:
        client = _server(daemon, "ci", guard="allow")
        with pytest.raises(HubBusyError, match=r"bench-B's power_cycle on channel 3.*NOT executed"):
            client._write("power", {"channel": 1, "state": "off", "max_wait_sec": 0.2})
        assert fake_hub.power[1] == 1
    finally:
        release.set()
        holder.join()


def test_a_write_that_queued_reports_how_long(daemon):
    release = threading.Event()
    held = threading.Event()

    def hold():
        with daemon._write_lock:
            held.set()
            release.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    held.wait(2)
    threading.Timer(0.3, release.set).start()
    result = _server(daemon, "ci", guard="allow").power(1, "off")
    holder.join()
    assert result["applied"] is True
    assert result["queued_ms"] >= 200


def test_the_page_shows_the_write_in_progress(daemon):
    daemon._busy = {"client": "bench-B", "method": "power_cycle", "channel": 3,
                    "since": 1.0, "_t0": time.monotonic()}
    try:
        busy = daemon.dashboard.state()["busy"]
    finally:
        daemon._busy = None
    assert busy == {"client": "bench-B", "method": "power_cycle", "channel": 3, "since": 1.0}


# ------------------------------------------------------------------ server wiring


def test_tool_calls_register_this_servers_sessions(daemon, default_session, monkeypatch):
    import mcp_server.server as server_module
    from mcp_server.server import handle_call_tool

    client = _server(daemon, "this-server")
    client.set_session_provider(server_module._hub_session_bundles)
    monkeypatch.setattr(server_module, "hub_manager", client)
    default_session.manager.alive = True  # its GDB server is running

    asyncio.run(handle_call_tool("debug_profile", {"action": "set", "hub": {"channel": 4}}))

    # Registered by the post-dispatch hook alone -- no hub tool was called.
    assert daemon._live_on(4) == "default @ this-server"


def test_discover_refuses_while_any_server_is_live(daemon, default_session, monkeypatch):
    import mcp_server.server as server_module
    from mcp_server.server import handle_call_tool

    _server(daemon, "bench-B", bundles=[_bundle("TC", hub={"channel": 2})]).report_sessions()
    monkeypatch.setattr(server_module, "hub_manager", _server(daemon, "this-server"))
    default_session.manager.alive = False

    payload = json.loads(asyncio.run(handle_call_tool("hub", {"action": "discover"}))[0].text)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "hub_busy_sessions"
    assert "TC @ bench-B" in payload["error"]["message"]


def test_dashboard_lists_sessions_from_every_server(daemon):
    _server(daemon, "bench-A", bundles=[_bundle("AGS", hub={"channel": 1})]).report_sessions()
    _server(daemon, "bench-B", bundles=[_bundle("TC", hub={"channel": 2})]).report_sessions()
    daemon.dashboard.poll_once()

    rows = daemon.dashboard.state()["sessions"]
    assert sorted((r["session"], r["client"]) for r in rows) == [("AGS", "bench-A"), ("TC", "bench-B")]
