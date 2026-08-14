"""A data line this process switched off is put back, like power already was.

Power had _we_turned_off and close(restore_power=True); data lines had NOTHING.
hub(action=data, exclusive=true) -- the documented way to make
start_debug_session's single-probe auto-select work on a rack -- switches off every
OTHER channel, so the common case left several boards off the USB bus with nothing
remembering to reconnect them. It happened on this bench: CH3 and CH4 sat
disconnected long after the run that isolated CH1.

A dark board is at least visible. A board whose probe silently vanished from the
bus looks like a broken cable.
"""
import pytest
from conftest import FakeHub

from mcp_server.hub import HubManager


@pytest.fixture
def manager():
    hub = FakeHub()
    mgr = HubManager(backend_factory=lambda exclude_ports=None: hub)
    mgr._idle_release_sec = 0          # the idle timer is tested separately
    mgr.configure({"port": "COM7"})
    return mgr, hub


def test_shutdown_puts_back_a_data_line_this_process_switched_off(manager):
    mgr, hub = manager
    mgr.data(2, "off", confirm=True)
    assert hub.data[2] == 0

    result = mgr.close()

    assert result["reconnected_channels"] == [2]
    assert hub.data[2] == 1


def test_shutdown_puts_back_every_channel_an_exclusive_isolation_dropped(manager):
    # The case that actually bites: isolating one channel takes every other one off
    # the bus, and that is the documented multi-board workflow.
    mgr, hub = manager

    mgr.data(1, "on", confirm=True, exclusive=True)
    assert [ch for ch, state in hub.data.items() if state == 0] == [2, 3, 4]

    result = mgr.close()

    assert result["reconnected_channels"] == [2, 3, 4]
    assert all(state == 1 for state in hub.data.values())


def test_a_channel_switched_back_on_is_no_longer_owed(manager):
    mgr, hub = manager
    mgr.data(2, "off", confirm=True)
    mgr.data(2, "on", confirm=True)

    result = mgr.close()

    assert result["reconnected_channels"] == [], "nothing was still owed"


def test_it_does_not_touch_a_channel_someone_else_switched_off(manager):
    # Only what THIS process changed. Re-enabling a line the user turned off by
    # hand would be the mirror of the bug: silently undoing a deliberate choice.
    mgr, hub = manager
    # A real operation first, so the manager is CONNECTED -- close() skips its
    # restore entirely when it is not, which made an earlier version of this test
    # pass no matter what close() did.
    mgr.data(2, "off", confirm=True)
    hub.data[3] = 0                    # switched off behind the manager's back

    result = mgr.close()

    assert result["reconnected_channels"] == [2], "only the one we took down"
    assert hub.data[3] == 0, "a line we never touched must stay as the user left it"


def test_restore_data_can_be_declined(manager):
    mgr, hub = manager
    mgr.data(2, "off", confirm=True)

    mgr.close(restore_data=False)

    assert hub.data[2] == 0


def test_power_and_data_are_both_restored_together(manager):
    mgr, hub = manager
    mgr.power(1, "off", confirm=True)
    mgr.data(2, "off", confirm=True)

    result = mgr.close()

    assert result["restored_channels"] == [1]
    assert result["reconnected_channels"] == [2]
    assert hub.power[1] == 1 and hub.data[2] == 1


def test_the_idle_release_will_not_drop_the_link_while_a_data_line_is_owed():
    # Same rule as power: holding the handle is what makes the restore possible at
    # all, because close() only restores while connected.
    import time

    hub = FakeHub()
    mgr = HubManager(backend_factory=lambda exclude_ports=None: hub)
    mgr._idle_release_sec = 0.05
    mgr.configure({"port": "COM7"})

    mgr.data(2, "off", confirm=True)
    time.sleep(0.25)

    assert mgr._hub is not None, "must keep the handle that can reconnect the line"
