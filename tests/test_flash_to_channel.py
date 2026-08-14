"""flash_and_run can bring up its own session, pinned to one probe.

"Flash the new firmware on CH4" should be ONE call. It was two -- start a session,
then flash -- and on a multi-probe bench it was four, because the session could not
say which probe it wanted so the bench had to be isolated first and put back after.

With the probe pinned by USB position nothing is disconnected, so the composite is
just the two steps joined, with no bench state to unwind.
"""
import asyncio
import json

import pytest
from conftest import FakeGdbClient, FakeGdbManager, FakeProfile

import mcp_server.server as server_module
from mcp_server.server import handle_call_tool

PROFILE = {
    "mcu": "STM32L151", "probe": "stlink", "server_type": "openocd",
    "hub": {"map": {"4": {"usb_location": "1-1.4"}}},
}


def _payload(result):
    return json.loads(result.content[0].text)


@pytest.fixture(autouse=True)
def _isolate_last_session(monkeypatch):
    # start_debug_session records into the module-global _last_session, which is
    # what recover_session replays. These tests really do start sessions, so
    # without this they leak "a session happened" into every later test in the run
    # -- two unrelated hub tests began failing in the full suite while passing
    # alone. The repo's existing convention is to patch it per test.
    monkeypatch.setattr(server_module, "_last_session",
                        {"server_type": None, "server_args": []})


@pytest.fixture
def bench(monkeypatch):
    manager = FakeGdbManager(alive=False)      # nothing running yet
    monkeypatch.setattr(server_module, "gdb_manager", manager)
    monkeypatch.setattr(server_module, "gdb_client", FakeGdbClient())
    monkeypatch.setattr(server_module, "debug_profile", FakeProfile(dict(PROFILE)))
    return manager


def test_it_starts_a_session_pinned_to_the_channel_then_flashes(bench):
    payload = _payload(asyncio.run(handle_call_tool(
        "flash_and_run", {"file_path": "fw.elf", "hub_channel": 4})))

    assert payload["ok"] is True
    assert payload["data"]["session"]["started"] is True
    assert payload["data"]["session"]["pinned_to"] == "hub channel 4"
    started_args = " ".join(str(a) for a in bench.started[-1])
    assert "adapter usb location 1-1.4" in started_args, \
        "the session must be pinned, not left to auto-select"


def test_an_explicit_location_works_without_a_hub_map(bench, monkeypatch):
    # Assigning server_module.debug_profile directly, as an earlier version did,
    # leaks the fake into every later test in the run -- two unrelated hub tests
    # started failing in the full suite while passing alone. monkeypatch undoes it.
    monkeypatch.setattr(server_module, "debug_profile", FakeProfile(
        {"mcu": "STM32L151", "probe": "stlink", "server_type": "openocd"}))

    payload = _payload(asyncio.run(handle_call_tool(
        "flash_and_run", {"file_path": "fw.elf", "usb_location": "1-1.2"})))

    assert payload["ok"] is True
    assert "adapter usb location 1-1.2" in " ".join(str(a) for a in bench.started[-1])


def test_a_live_session_is_used_rather_than_a_second_one_started(monkeypatch):
    # Flashing through the session the caller already has is the safe reading of
    # "flash this": starting a second one behind their back could attach elsewhere.
    manager = FakeGdbManager(alive=True)
    monkeypatch.setattr(server_module, "gdb_manager", manager)
    monkeypatch.setattr(server_module, "gdb_client", FakeGdbClient())
    monkeypatch.setattr(server_module, "debug_profile", FakeProfile(dict(PROFILE)))

    payload = _payload(asyncio.run(handle_call_tool(
        "flash_and_run", {"file_path": "fw.elf", "hub_channel": 4})))

    assert payload["ok"] is True
    assert "session" not in payload["data"], "no session should have been started"
    assert manager.started == [], "the live session must be reused"


def test_plain_flash_and_run_is_unchanged(monkeypatch):
    # No hub_channel, no usb_location: exactly the old behaviour, no session logic.
    manager = FakeGdbManager(alive=True)
    monkeypatch.setattr(server_module, "gdb_manager", manager)
    monkeypatch.setattr(server_module, "gdb_client", FakeGdbClient())
    monkeypatch.setattr(server_module, "debug_profile", FakeProfile(dict(PROFILE)))

    payload = _payload(asyncio.run(handle_call_tool(
        "flash_and_run", {"file_path": "fw.elf"})))

    assert payload["ok"] is True
    assert manager.started == []


def test_a_failed_start_is_reported_instead_of_flashing_blind(bench, monkeypatch):
    # Flashing after a failed start would attach to whatever OpenOCD picked, which
    # on a multi-probe bench is how the wrong board gets written.
    monkeypatch.setattr(server_module, "debug_profile", FakeProfile({"hub": {"map": {}}}))

    payload = _payload(asyncio.run(handle_call_tool(
        "flash_and_run", {"file_path": "fw.elf", "hub_channel": 4})))

    assert payload["ok"] is False
    assert "could not start a session" in payload["error"]["message"]
