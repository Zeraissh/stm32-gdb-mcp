"""Three defects the read-only audit raised and one test-quality gap it found.

Each was invisible to a fully green suite, which is why they are here:

1. call_read / call crashed with an unhandled AttributeError on a non-object `args`.
2. DebugProfileStore aliased the caller's dicts in BOTH directions, so hub.map --
   which decides the channel power gets cut on -- was editable from outside.
3. reset_target's cold path blamed the map whenever the hub binding was missing,
   including when the map was fine and the hub was simply unreachable.
4. Two of hub._unmapped's branches had no test at all.
"""
import asyncio
import json

import pytest

from mcp_server.debug_profile import DebugProfileStore
from mcp_server.hub import HubManager, HubUnavailableError
from mcp_server.server import handle_call_tool


def _payload(result):
    return json.loads(result.content[0].text)


# --- 1. a non-object `args` must not escape as a crash ------------------------

@pytest.mark.parametrize("dispatcher", ["call", "call_read"])
@pytest.mark.parametrize("bad", ["oops", 42, ["address", "0x0"], True])
def test_a_non_object_args_returns_an_envelope_not_an_exception(dispatcher, bad):
    # It used to raise AttributeError out of handle_call_tool -- arguments.get()
    # on a str -- so the client saw a protocol-level failure where every other bad
    # input gets a structured error.
    payload = _payload(asyncio.run(handle_call_tool(
        dispatcher, {"tool": "mcp_info", "args": bad})))

    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_arguments"
    assert type(bad).__name__ in payload["error"]["message"]


@pytest.mark.parametrize("dispatcher", ["call", "call_read"])
def test_an_omitted_or_null_args_still_works(dispatcher):
    # The guard must reject non-objects without breaking the ordinary no-args call.
    for args in ({}, None):
        arguments = {"tool": "mcp_info"}
        if args is not None:
            arguments["args"] = args
        assert _payload(asyncio.run(handle_call_tool(dispatcher, arguments)))["ok"] is True


# --- 2. the store must not share dicts with its callers ----------------------

def test_editing_a_returned_profile_does_not_edit_the_stored_one():
    store = DebugProfileStore()
    store.update({"hub": {"map": {"3": {"key": "instance_id:USB-3"}}}})

    returned = store.get()
    returned["hub"]["map"]["3"]["key"] = "TAMPERED"
    returned["hub"]["map"]["9"] = {"label": "injected"}

    assert store.get()["hub"]["map"] == {"3": {"key": "instance_id:USB-3"}}


def test_editing_the_dict_you_passed_in_does_not_edit_the_stored_one():
    store = DebugProfileStore()
    caller_owned = {"map": {"3": {"key": "orig"}}}

    store.update({"hub": caller_owned})
    caller_owned["map"]["3"]["key"] = "TAMPERED-LATER"
    caller_owned["map"]["4"] = {"label": "injected"}

    assert store.get()["hub"]["map"] == {"3": {"key": "orig"}}


def test_lists_are_copied_too():
    store = DebugProfileStore()
    args = ["-f", "interface/stlink.cfg"]

    store.update({"server_args": args})
    args.append("-c")
    args[0] = "TAMPERED"

    assert store.get()["server_args"] == ["-f", "interface/stlink.cfg"]


# --- 3. the cold-reset failure must name the real cause ----------------------

def test_cold_reset_reports_why_the_binding_failed_not_a_guess(hub_session, monkeypatch):
    # A pinned channel with an unreachable hub is NOT "no channel resolved from the
    # map": sending the user to re-run discovery there is the wrong direction.
    # Patch the name execution_tools bound at import, not the one in _helpers:
    # `from ._helpers import resolve_hub_binding` copies the reference, so patching
    # the source module would leave the caller pointing at the original.
    import mcp_server.tools.execution_tools as execution_tools

    hub_session.session.profile.update({"hub": {"channel": 3}})
    monkeypatch.setattr(execution_tools, "resolve_hub_binding",
                        lambda ctx: (None, "hub port busy: COM7 is already in use"))

    payload = _payload(asyncio.run(handle_call_tool(
        "reset_target", {"halt": True, "strategy": "cold"})))

    assert payload["error"]["code"] == "cold_reset_unavailable"
    assert "COM7 is already in use" in payload["error"]["message"]
    assert "hub(action=discover" not in str(payload["suggested_next_actions"])


# --- 4. the two untested _unmapped branches ----------------------------------

def _manager(spec):
    manager = HubManager()
    manager.configure(spec)
    return manager


def test_a_serial_only_map_with_no_probe_serial_says_to_start_the_session():
    # The map can only select on serial, and nothing has one yet -- so the answer is
    # "start the session", not "fix the map".
    spec = {"map": {1: {"serial": "AAA"}}}

    with pytest.raises(HubUnavailableError) as excinfo:
        _manager(spec).channel_for(profile={"hub": spec}, session_id="TC")

    assert "no" in str(excinfo.value) and "probe serial" in str(excinfo.value)
    assert "start_debug_session" in excinfo.value.next_actions


def test_a_map_with_neither_label_nor_serial_names_the_serial_less_probe_case():
    # What hub(action=discover) writes for a serial-less clone ST-Link: a USB port
    # key, which identifies a channel but cannot select one. The docs say so; this
    # is the error saying so too.
    spec = {"map": {3: {"key": "instance_id:USB-3"}, 4: {"key": "instance_id:USB-4"}}}

    with pytest.raises(HubUnavailableError) as excinfo:
        _manager(spec).channel_for(profile={"hub": spec}, session_id="TC")

    message = str(excinfo.value)
    assert "[3, 4]" in message
    assert "identifies a channel but cannot select one" in message
