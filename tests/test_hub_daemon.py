"""Shared hub daemon: one owner, many MCP servers.

Most tests run the daemon in-process against FakeHub and talk to it over real
loopback HTTP through ``RemoteHubManager``, so the wire format is exercised; one
test spawns the real daemon subprocess end to end.
"""

import asyncio
import json
import os
import subprocess
import sys
import time

import pytest

import mcp_server.hub_daemon as daemon_module
import mcp_server.hub_remote as remote_module
from mcp_server.hub import HubGuardError, HubManager, HubUnavailableError
from mcp_server.hub_daemon import PROTOCOL, HubDaemon, rpc_call
from mcp_server.hub_remote import HubManagerSelector, RemoteHubManager

TOKEN = "test-token"


@pytest.fixture
def daemon(hub_manager):
    owner = HubDaemon(manager=hub_manager, token=TOKEN, idle_exit_sec=0)
    owner.start(port=0, interval_sec=0.2)
    yield owner
    owner.stop()


def _remote(daemon, name="A", guard=None, **spec):
    client = RemoteHubManager(connector=lambda spawn=True: (daemon.info["url"], TOKEN), client_name=name)
    if guard is not None:
        spec["guard"] = guard
    client.configure(spec)
    return client


# ------------------------------------------------------------------ reads


def test_describe_goes_through_the_daemon(daemon, fake_hub):
    client = _remote(daemon, map={"2": {"label": "TC"}})
    result = client.describe()

    assert result["mode"] == "daemon"
    assert result["channels"] == [1, 2, 3, 4]
    assert result["device"]["serial_no"] == "SUH-FAKE-0001"
    assert result["daemon"]["pid"] == os.getpid()
    # Annotated with THIS client's map, exactly as the in-process describe was.
    assert [p.get("mapped") for p in result["ports"]] == [None, {"label": "TC"}, None, None]


def test_channel_resolution_stays_local_and_validates_against_the_daemon(daemon):
    client = _remote(daemon, channel=3)
    client.channels()
    assert client.channel_for(profile={"hub": {"channel": 3}}) == (3, "profile")
    with pytest.raises(HubUnavailableError, match="does not exist"):
        client.channel_for(explicit=9)


def test_measure_returns_the_series_shape(daemon, fake_hub):
    result = _remote(daemon).measure((1,), duration_sec=0)
    assert result["series"]["1"][0]["current_ma"] == fake_hub.current_ma


# ------------------------------------------------------------------ writes


def test_power_write_is_applied_and_attributed(daemon, fake_hub, hub_manager):
    client = _remote(daemon, name="bench-A")
    result = client.power(2, "off", confirm=True)

    assert result == {"channel": 2, "action": "power_off", "applied": True}
    assert fake_hub.power[2] == 0
    assert hub_manager.guard.audit_log[-1]["client"] == "bench-A"


def test_guard_mode_travels_with_each_client_and_never_leaks(daemon, fake_hub, hub_manager):
    ci = _remote(daemon, name="ci", guard="allow")
    careful = _remote(daemon, name="human")  # default confirm mode

    ci.power(1, "off")  # allowed without confirm in ITS mode
    assert fake_hub.power[1] == 0
    with pytest.raises(HubGuardError, match="confirm"):
        careful.power(2, "off")
    assert fake_hub.power[2] == 1
    # The daemon's own guard was only borrowed for the call.
    assert hub_manager.guard.mode == "confirm"


def test_live_session_interlock_still_applies(daemon):
    with pytest.raises(HubGuardError, match="live GDB server"):
        _remote(daemon, guard="allow").power(1, "off", live_session="TC")


def test_bad_arguments_come_back_as_value_errors(daemon):
    with pytest.raises(ValueError, match="state must be"):
        _remote(daemon).power(1, "sideways", confirm=True)


def test_exclusive_isolation_works_over_the_wire(daemon, fake_hub):
    result = _remote(daemon).data(1, "on", confirm=True, exclusive=True)
    assert result["exclusive"] is True
    assert fake_hub.data == {1: 1, 2: 0, 3: 0, 4: 0}


# ------------------------------------------------------------------ restore


def test_release_restores_only_what_that_client_switched_off(daemon, fake_hub):
    a, b = _remote(daemon, name="A"), _remote(daemon, name="B")
    a.power(1, "off", confirm=True)
    b.power(2, "off", confirm=True)

    released = a.close()

    assert released["restored_channels"] == [1]
    assert fake_hub.power[1] == 1
    assert fake_hub.power[2] == 0  # B still wants it off


def test_turning_a_channel_back_on_settles_the_debt(daemon, fake_hub):
    a, b = _remote(daemon, name="A"), _remote(daemon, name="B")
    a.power(3, "off", confirm=True)
    b.power(3, "on", confirm=True)
    b.power(3, "off", confirm=True)

    assert a.close()["restored_channels"] == []
    assert fake_hub.power[3] == 0


def test_exclusive_neighbours_are_reconnected_on_release(daemon, fake_hub):
    client = _remote(daemon)
    client.data(1, "on", confirm=True, exclusive=True)
    assert client.close()["reconnected_channels"] == [2, 3, 4]
    assert fake_hub.data == {1: 1, 2: 1, 3: 1, 4: 1}


def test_a_dead_client_is_released_by_maintenance(daemon, fake_hub, monkeypatch):
    dead_pid = 4_000_000
    rpc_call(daemon.info["url"], TOKEN, "power", {"channel": 4, "state": "off", "confirm": True},
             client={"id": "ghost", "pid": dead_pid, "name": "crashed"})
    assert fake_hub.power[4] == 0

    monkeypatch.setattr(daemon_module, "_pid_alive", lambda pid: pid != dead_pid)
    daemon.maintain()

    assert fake_hub.power[4] == 1
    assert "ghost" not in daemon._clients


def test_close_without_ever_connecting_does_not_spawn(daemon):
    calls = []
    client = RemoteHubManager(connector=lambda spawn=True: calls.append(spawn) or ("http://x", "t"))
    assert client.close() == {"restored_channels": [], "reconnected_channels": []}
    assert calls == []


# ------------------------------------------------------------------ idle exit


def test_idle_exit_waits_while_a_channel_is_owed(hub_manager):
    clock = [1000.0]
    owner = HubDaemon(manager=hub_manager, token=TOKEN, idle_exit_sec=60, clock=lambda: clock[0])
    owner.start(port=0, interval_sec=5)
    try:
        client = _remote(owner)
        client.power(1, "off", confirm=True)
        clock[0] += 120
        assert owner.maintain() is False  # never strand a board dark by exiting
        client.power(1, "on", confirm=True)
        clock[0] += 120
        assert owner.maintain() is True
    finally:
        owner.stop()


# ------------------------------------------------------------------ transport


def test_wrong_token_is_rejected(daemon):
    with pytest.raises(HubUnavailableError, match="token"):
        rpc_call(daemon.info["url"], "wrong", "ping")


def test_protocol_mismatch_names_the_fix(daemon):
    status, payload = daemon.handle_rpc(
        json.dumps({"method": "ping", "protocol": PROTOCOL + 1}).encode(),
        {"Authorization": f"Bearer {TOKEN}"})
    assert status == 409
    assert "hub_daemon stop" in payload["error"]["message"]


def test_unknown_method_is_refused(daemon):
    with pytest.raises(ValueError, match="unknown method"):
        rpc_call(daemon.info["url"], TOKEN, "format_disk")


def test_client_reconnects_to_a_restarted_daemon(hub_manager, fake_hub):
    first = HubDaemon(manager=hub_manager, token=TOKEN, idle_exit_sec=0)
    first.start(port=0, interval_sec=5)
    endpoint = {"url": first.info["url"]}
    client = RemoteHubManager(connector=lambda spawn=True: (endpoint["url"], TOKEN))
    client.configure({"map": {"1": {"label": "TC"}}})
    client.describe()

    first.stop()
    second = HubDaemon(manager=HubManager(backend_factory=lambda **_k: fake_hub), token=TOKEN, idle_exit_sec=0)
    second.start(port=0, interval_sec=5)
    endpoint["url"] = second.info["url"]
    try:
        assert client.describe()["channels"] == [1, 2, 3, 4]
        # The new daemon learned this client's spec again before the call.
        assert second._merged_spec()["map"] == {"1": {"label": "TC"}}
    finally:
        second.stop()


def test_dashboard_state_lists_clients_in_daemon_mode(daemon):
    _remote(daemon, name="bench-A").power(2, "off", confirm=True)
    state = daemon.dashboard.state()

    assert state["mode"] == "daemon"
    row = next(c for c in state["clients"] if c["name"] == "bench-A")
    assert row["owes_power"] == [2]


# ------------------------------------------------------------------ tool layer


def test_hub_tools_run_unchanged_against_the_daemon(daemon, fake_hub, default_session, monkeypatch):
    import mcp_server.server as server_module
    from mcp_server.server import handle_call_tool

    monkeypatch.setattr(server_module, "hub_manager", _remote(daemon))
    default_session.manager.alive = False
    asyncio.run(handle_call_tool("debug_profile", {"action": "set", "hub": {"channel": 2, "guard": "allow"}}))

    payload = json.loads(asyncio.run(handle_call_tool("hub", {"action": "power", "state": "off"}))[0].text)
    assert payload["ok"] is True and payload["data"]["channel_source"] == "profile"
    assert fake_hub.power[2] == 0

    shown = json.loads(asyncio.run(handle_call_tool("hub", {"action": "dashboard"}))[0].text)
    assert shown["ok"] is True
    assert shown["data"]["url"] == daemon.info["url"]
    assert shown["data"]["mode"] == "daemon"


# ------------------------------------------------------------------ selector


def test_selector_local_mode_uses_the_in_process_manager(monkeypatch):
    import mcp_server.hub as hub_module

    monkeypatch.setenv("STM32_GDB_MCP_HUB_MODE", "local")
    selector = HubManagerSelector()
    assert selector.resolve() is hub_module.hub_manager
    assert selector.selected_mode == "local"


def test_selector_auto_without_the_extra_stays_local(monkeypatch):
    monkeypatch.setenv("STM32_GDB_MCP_HUB_MODE", "auto")
    monkeypatch.setattr(remote_module.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(remote_module, "connect", lambda **_k: pytest.fail("must not spawn"))
    selector = HubManagerSelector()
    selector.resolve()
    assert selector.selected_mode == "local"
    assert "not installed" in selector.fallback_reason


def test_selector_auto_falls_back_when_the_daemon_will_not_start(monkeypatch):
    monkeypatch.setenv("STM32_GDB_MCP_HUB_MODE", "auto")
    monkeypatch.setattr(remote_module.importlib.util, "find_spec", lambda name: object())

    def refuse(**_kwargs):
        raise HubUnavailableError("did not come up")

    monkeypatch.setattr(remote_module, "connect", refuse)
    selector = HubManagerSelector()
    selector.resolve()
    assert selector.selected_mode == "local"
    assert "did not come up" in selector.fallback_reason


def test_selector_daemon_mode_and_close_before_use(monkeypatch, daemon):
    monkeypatch.setenv("STM32_GDB_MCP_HUB_MODE", "daemon")
    selector = HubManagerSelector()
    assert selector.close() == {"restored_channels": [], "reconnected_channels": []}
    assert selector.selected_mode is None  # closing never decided (or spawned) anything

    monkeypatch.setattr(remote_module, "connect", lambda spawn=True, **_k: (daemon.info["url"], TOKEN))
    assert selector.describe()["mode"] == "daemon"
    assert selector.selected_mode == "daemon"


# ------------------------------------------------------------------ end to end


def test_spawned_daemon_end_to_end(tmp_path, monkeypatch):
    """The real thing: spawn, share, report the hub error, stop, clean up."""
    monkeypatch.setenv("STM32_GDB_MCP_HUB_DAEMON_DIR", str(tmp_path))
    url, token = remote_module.connect(spawn=True, timeout=20)
    state = daemon_module.read_state()
    assert state and state["url"] == url
    if os.name == "posix":
        assert (os.stat(daemon_module.state_path()).st_mode & 0o077) == 0  # token file is private

    try:
        # A second connect reuses the same daemon instead of spawning another.
        assert remote_module.connect(spawn=True, timeout=5) == (url, token)
        client = RemoteHubManager(connector=lambda spawn=True: (url, token))
        if remote_module.importlib.util.find_spec("smartusbhub") is None:
            with pytest.raises(HubUnavailableError, match="smartusbhub is not installed"):
                client.describe()
        status = subprocess.run([sys.executable, "-m", "mcp_server.hub_daemon", "status"],
                                capture_output=True, text=True, timeout=20,
                                env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)})
        assert "hub daemon pid" in status.stdout
    finally:
        rpc_call(url, token, "shutdown", timeout=5)
        deadline = time.monotonic() + 15
        while daemon_module.read_state() is not None and time.monotonic() < deadline:
            time.sleep(0.1)
    assert daemon_module.read_state() is None
    assert not os.path.exists(os.path.join(str(tmp_path), "daemon.lock"))
