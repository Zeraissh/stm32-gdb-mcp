"""The hub control port is handed back after an idle period.

The vendor library holds a CROSS-PROCESS lock on the hub's USB-CDC port, and the
connection was lazy and then cached forever -- so one MCP session that merely ran
hub(action=describe) locked every other session out of the bench until its process
exited, with no way to hand it back. Reproduced live: a second process got
"PortBusyError: Port COM7 is already in use by another process".
"""
import threading
import time

import pytest
from conftest import FakeHub

from mcp_server.hub import HubManager


def _manager(hub, idle_sec=0.2):
    manager = HubManager(backend_factory=lambda exclude_ports=None: hub)
    manager._idle_release_sec = idle_sec
    manager.configure({"port": "COM7"})
    return manager


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_the_port_is_released_after_the_idle_window(fake_hub):
    manager = _manager(fake_hub)
    manager.describe()
    assert manager._hub is not None

    assert _wait_until(lambda: manager._hub is None), "idle release never fired"
    # The port is genuinely handed back, not just forgotten by the manager --
    # another process can only take it once the vendor handle is closed.
    assert fake_hub.connected is False
    assert "disconnect" in fake_hub.calls


def test_the_next_call_reconnects_transparently(fake_hub):
    manager = _manager(fake_hub)
    manager.describe()
    assert _wait_until(lambda: manager._hub is None)

    info = manager.describe()   # must just work, because connecting was always lazy

    assert info["available"] is True
    assert manager._hub is not None


def test_activity_pushes_the_deadline_out_and_release_still_happens_after(fake_hub):
    # Two halves, and the SECOND is the one with teeth. Asserting only "a busy
    # manager keeps its link" passes even if the deadline is never re-armed --
    # the first timer fires, sees recent activity, returns, and then nothing ever
    # schedules another, so the port is held forever. That is the bug this guards.
    manager = _manager(fake_hub, idle_sec=0.25)
    for _ in range(5):
        manager.describe()
        time.sleep(0.08)      # total 0.4s, but never 0.25s idle

    assert manager._hub is not None, "a busy manager must not drop its link"

    assert _wait_until(lambda: manager._hub is None, timeout=3.0), \
        "the deadline must be re-armed by activity, or the port is never handed back"


def test_it_never_releases_while_it_owes_a_channel_its_power_back(fake_hub):
    # The safety rule. close() only restores power WHILE CONNECTED, so dropping the
    # link here would leave a board dark with nothing able to put it back.
    manager = _manager(fake_hub)
    manager.power(1, "off", confirm=True)
    assert manager._we_turned_off == {1}

    time.sleep(0.6)   # well past the idle window

    assert manager._hub is not None, "must keep the handle that can restore power"


def test_it_releases_again_once_the_power_is_restored(fake_hub):
    manager = _manager(fake_hub)
    manager.power(1, "off", confirm=True)
    time.sleep(0.6)
    assert manager._hub is not None

    manager.power(1, "on", confirm=True)

    assert _wait_until(lambda: manager._hub is None), "release must resume once nothing is owed"


def test_an_idle_release_does_not_touch_power(fake_hub):
    # An idle disconnect is not a shutdown. Re-powering here would silently undo a
    # deliberate hub(action=power, state="off") just because the agent paused.
    manager = _manager(fake_hub)
    manager.describe()
    before = dict(fake_hub.power)

    assert _wait_until(lambda: manager._hub is None)

    assert fake_hub.power == before


def test_disconnecting_cancels_the_timer_rather_than_leaking_a_thread(fake_hub):
    manager = _manager(fake_hub, idle_sec=30.0)
    manager.describe()
    assert manager._idle_timer is not None
    before = threading.active_count()

    manager.close(restore_power=False)

    assert manager._idle_timer is None
    assert threading.active_count() <= before


def test_idle_release_can_be_disabled(fake_hub):
    manager = _manager(fake_hub, idle_sec=0)
    manager.describe()

    time.sleep(0.5)

    assert manager._hub is not None
    assert manager._idle_timer is None


@pytest.fixture
def fake_hub():
    return FakeHub()


def test_a_timer_that_fires_early_re_arms_instead_of_giving_up(fake_hub):
    # threading.Timer waits on an Event whose clock need not agree with
    # time.monotonic(), so it can fire a hair early. Returning without re-arming
    # then leaves NOTHING scheduled and holds the port forever -- the exact failure
    # this feature exists to prevent. Windows CI caught it; local timing did not.
    #
    # Driven directly rather than by sleeping, so it pins the logic instead of
    # racing the clock.
    manager = _manager(fake_hub, idle_sec=10.0)
    manager.describe()
    first = manager._idle_timer
    assert first is not None

    manager._release_if_idle()          # as if the timer fired 10 s early

    assert manager._hub is not None, "must not release before the window elapses"
    assert manager._idle_timer is not None, "must still have a timer scheduled"
    assert manager._idle_timer is not first, "must have re-armed, not kept the fired one"

    # And it still releases once the window really has passed.
    manager._touched_at -= 20.0
    manager._release_if_idle()
    assert manager._hub is None
