"""Hub daemon, phase 3: per-channel write locks and rack-wide leases.

Timing assertions use generous margins: a power cycle here really sleeps (the
fake hub answers instantly, but power_cycle holds its off window with
time.sleep), so "concurrent" means two 400 ms cycles finishing well under the
800 ms they would take back to back.
"""

import asyncio
import json
import threading
import time

import pytest

import mcp_server.hub_daemon as daemon_module
import mcp_server.hub_remote as remote_module
from mcp_server.hub import HubBusyError, HubGuardError
from mcp_server.hub_daemon import HubDaemon, rpc_call
from mcp_server.hub_locks import ALL, ChannelLocks
from mcp_server.hub_remote import RemoteHubManager

TOKEN = "test-token"


@pytest.fixture
def daemon(hub_manager):
    owner = HubDaemon(manager=hub_manager, token=TOKEN, idle_exit_sec=0)
    owner.start(port=0, interval_sec=5)
    yield owner
    owner.stop()


@pytest.fixture(autouse=True)
def _no_heartbeats(monkeypatch):
    monkeypatch.setattr(remote_module, "HEARTBEAT_SEC", 3600)


def _server(daemon, name, guard=None):
    client = RemoteHubManager(connector=lambda spawn=True: (daemon.info["url"], TOKEN), client_name=name)
    client.configure({"guard": guard} if guard else {})
    return client


def _in_thread(fn):
    """Run ``fn`` on a thread; returns a callable that joins and yields (result, error)."""
    box = {}

    def run():
        try:
            box["result"] = fn()
        except Exception as exc:  # noqa: BLE001 - surfaced to the test
            box["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()

    def join():
        thread.join(10)
        return box.get("result"), box.get("error")

    return join


def _wait_inflight(daemon, timeout=10.0):
    """Block until some write holds a channel -- i.e. the cycle under test is running.

    Replaces a fixed sleep, which on a slow runner could return before the
    cycle's request even reached the daemon.
    """
    deadline = time.monotonic() + timeout
    while not daemon.locks.holders() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert daemon.locks.holders(), "the background write never started"


# ------------------------------------------------------------------ ChannelLocks


def test_disjoint_channels_do_not_wait():
    locks = ChannelLocks()
    a = locks.acquire({1}, {"client": "a"}, timeout=0)
    b = locks.acquire({2}, {"client": "b"}, timeout=0)
    assert a is not None and b is not None


def test_overlap_and_all_conflict():
    locks = ChannelLocks()
    held = locks.acquire({1, 2}, {"client": "a"}, timeout=0)
    assert locks.acquire({2}, {"client": "b"}, timeout=0.05) is None
    assert locks.acquire(ALL, {"client": "c"}, timeout=0.05) is None
    locks.release(held)
    everything = locks.acquire(ALL, {"client": "c"}, timeout=0)
    assert locks.acquire({4}, {"client": "d"}, timeout=0.05) is None
    locks.release(everything)


def test_a_queued_rack_wide_request_is_not_starved():
    locks = ChannelLocks()
    held = locks.acquire({1}, {"client": "cycle"}, timeout=0)
    waiter = _in_thread(lambda: locks.acquire(ALL, {"client": "exclusive"}, timeout=5))
    deadline = time.monotonic() + 2
    while locks.waiting() == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    # Channel 2 is free right now, but the earlier ALL request wants it too.
    assert locks.acquire({2}, {"client": "late"}, timeout=0.1) is None
    locks.release(held)
    token, error = waiter()
    assert error is None and token is not None


def test_a_token_can_be_released_from_another_thread():
    locks = ChannelLocks()
    token = locks.acquire({3}, {"client": "lease"}, timeout=0)
    _in_thread(lambda: locks.release(token))()
    assert locks.acquire({3}, {"client": "next"}, timeout=0.5) is not None


def test_holders_describe_what_is_running():
    locks = ChannelLocks()
    locks.acquire({2}, {"client": "bench-B", "method": "power_cycle"}, timeout=0)
    locks.acquire(ALL, {"client": "x"}, timeout=0.01)  # times out; not a holder
    rows = locks.holders({2, 3})
    assert [(r["client"], r["method"], r["channels"]) for r in rows] == [("bench-B", "power_cycle", [2])]
    assert locks.holders({4}) == []


# ------------------------------------------------------------------ concurrency


def test_cycles_on_different_channels_overlap(daemon):
    a, b = _server(daemon, "A"), _server(daemon, "B")
    started = time.monotonic()
    # 1 s windows: back to back is >= 2.0 s, overlapping ~1.0 s, so 1.6 s separates
    # the two with room for a slow CI runner's per-request overhead.
    first = _in_thread(lambda: a.power_cycle(1, off_ms=1000, settle_ms=0, confirm=True))
    second = _in_thread(lambda: b.power_cycle(2, off_ms=1000, settle_ms=0, confirm=True))
    (r1, e1), (r2, e2) = first(), second()
    elapsed = time.monotonic() - started

    assert e1 is None and e2 is None
    assert r1["action"] == r2["action"] == "power_cycle"
    assert elapsed < 1.6, f"cycles on channels 1 and 2 ran back to back ({elapsed:.2f}s)"
    assert "queued_ms" not in r1 and "queued_ms" not in r2


def test_writes_on_the_same_channel_still_queue(daemon):
    a, b = _server(daemon, "A"), _server(daemon, "B", guard="allow")
    cycle = _in_thread(lambda: a.power_cycle(3, off_ms=400, settle_ms=0, confirm=True))
    _wait_inflight(daemon)
    result = b.power(3, "off")
    cycle()
    assert result["applied"] is True
    assert result["queued_ms"] >= 200


def test_guard_mode_and_attribution_stay_per_call_under_concurrency(daemon, fake_hub, hub_manager):
    ci, human = _server(daemon, "ci", guard="allow"), _server(daemon, "human")
    cycle = _in_thread(lambda: ci.power_cycle(1, off_ms=400, settle_ms=0))  # allowed: ci's own mode
    _wait_inflight(daemon)
    # While ci's call is running with ITS mode, the human's cut must still be
    # judged in confirm mode -- the old swap-the-shared-guard approach leaked here.
    with pytest.raises(HubGuardError, match="confirm"):
        human.power(2, "off")
    result, error = cycle()

    assert error is None and result["action"] == "power_cycle"
    assert fake_hub.power[2] == 1
    assert hub_manager.guard.mode == "confirm"
    by_channel = {(e["channel"], e["decision"]): e.get("client") for e in hub_manager.guard.audit_log}
    assert by_channel[(1, "apply")] == "ci"
    assert by_channel[(2, "blocked")] == "human"


def test_exclusive_waits_for_an_in_flight_cycle(daemon, fake_hub):
    a, b = _server(daemon, "A"), _server(daemon, "B")
    cycle = _in_thread(lambda: a.power_cycle(2, off_ms=400, settle_ms=0, confirm=True))
    _wait_inflight(daemon)
    result = b.data(1, "on", confirm=True, exclusive=True)
    cycle()
    assert result["queued_ms"] >= 200
    assert result["data_off_channels"] == [2, 3, 4]
    assert fake_hub.power[2] == 1  # the cycle completed before isolation touched channel 2


# ------------------------------------------------------------------ leases


def test_a_lease_holds_the_rack_for_its_holder_only(daemon, fake_hub):
    a, b = _server(daemon, "A"), _server(daemon, "B")
    with a.hold("discover"):
        # A's own writes inside the block go straight through...
        assert "queued_ms" not in a.data(2, "off", confirm=True)
        # ...everyone else's queue and are refused after their wait.
        with pytest.raises(HubBusyError, match=r"A's discover on every channel"):
            b._write("power", {"channel": 3, "state": "off", "confirm": True, "max_wait_sec": 0.2})
        a.data(2, "on", confirm=True)
    assert fake_hub.power[3] == 1
    assert b.power(3, "off", confirm=True)["applied"] is True  # free again
    assert daemon._leases == {}


def test_a_lease_only_covers_the_thread_that_took_it(daemon):
    a = _server(daemon, "A")
    with a.hold("discover"):
        _result, error = _in_thread(lambda: a._write(
            "power", {"channel": 1, "state": "off", "confirm": True, "max_wait_sec": 0.2}))()
    assert isinstance(error, HubBusyError)


def test_someone_elses_lease_id_is_worthless(daemon):
    a, b = _server(daemon, "A"), _server(daemon, "B")
    with a.hold("discover") as lease:
        with pytest.raises(HubBusyError):
            b._write("power", {"channel": 1, "state": "off", "confirm": True,
                               "max_wait_sec": 0.2, "lease": lease["lease"]})


def test_a_lease_expires_and_a_dead_holders_lease_is_dropped(hub_manager, monkeypatch):
    clock = [1000.0]
    owner = HubDaemon(manager=hub_manager, token=TOKEN, idle_exit_sec=0, clock=lambda: clock[0])
    owner.start(port=0, interval_sec=5)
    try:
        lease = rpc_call(owner.info["url"], TOKEN, "lease_acquire", {"reason": "discover", "ttl_sec": 30},
                         client={"id": "hung", "pid": None, "name": "hung"})
        clock[0] += 31
        owner.maintain()
        assert lease["lease"] not in owner._leases
        assert owner.locks.holders() == []

        dead_pid = 4_000_001
        rpc_call(owner.info["url"], TOKEN, "lease_acquire", {"reason": "discover"},
                 client={"id": "ghost", "pid": dead_pid, "name": "ghost"})
        monkeypatch.setattr(daemon_module, "_pid_alive", lambda pid: pid != dead_pid)
        owner.maintain()
        assert owner._leases == {} and owner.locks.holders() == []
    finally:
        owner.stop()


def test_restore_that_cannot_get_its_channel_keeps_the_debt(daemon, fake_hub):
    a = _server(daemon, "A")
    a.power(1, "off", confirm=True)
    token = daemon.locks.acquire({1}, {"client": "B", "method": "power_cycle"}, timeout=0)
    try:
        result = daemon._release(a.client_id, restore_power=True, restore_data=True,
                                 reason="client closed", wait=0.1)
    finally:
        daemon.locks.release(token)
    assert "deferred" in result
    assert fake_hub.power[1] == 0
    assert daemon._owed[a.client_id]["power"] == {1}
    assert daemon._release(a.client_id, True, True, "retry")["restored_channels"] == [1]


# ------------------------------------------------------------------ discover


def test_discover_holds_the_rack_while_it_runs(daemon, fake_hub, default_session, monkeypatch):
    import mcp_server.server as server_module
    from mcp_server.server import handle_call_tool

    other = _server(daemon, "bench-B")
    seen = []

    def detect():
        # Another server tries to switch mid-discovery: it must be refused.
        try:
            other._write("power", {"channel": 4, "state": "off", "confirm": True, "max_wait_sec": 0.05})
            seen.append("ran")
        except HubBusyError as exc:
            seen.append(str(exc))
        return {"probes": []}

    monkeypatch.setattr(server_module, "hub_manager", _server(daemon, "this-server"))
    monkeypatch.setattr(server_module, "detect_probe", detect)
    default_session.manager.alive = False

    payload = json.loads(asyncio.run(handle_call_tool("hub", {"action": "discover", "settle_sec": 0}))[0].text)

    assert payload["ok"] is True, payload
    assert seen and all("discover on every channel" in entry for entry in seen)
    assert fake_hub.power[4] == 1
    assert daemon._leases == {} and daemon.locks.holders() == []
