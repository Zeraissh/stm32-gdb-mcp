"""Both write tools consult the guard, and an argument cannot smuggle a write past call_read.

Two defects found by auditing what call_read forwards without an approval prompt:

1. typed_memory(action=write) never consulted MemoryWriteGuard -- it was a two-line
   passthrough -- so it reached the IWDG key register that write_memory refuses, and
   left no audit entry.
2. read_cycle_counter / sample_pc write DEMCR.TRCENA (and halt a running core to do
   it) when enable is set, while sitting in the no-prompt read-only surface.
"""
import asyncio
import json

import pytest
from conftest import FakeGdbClient, FakeProfile

from mcp_server.server import handle_call_tool
from mcp_server.tool_surface import read_only_dispatch_target

IWDG_KEY = "0x40003000"   # in DEFAULT_PROTECTED_REGIONS


def _payload(result):
    return json.loads(result.content[0].text)


@pytest.fixture
def session(monkeypatch):
    import mcp_server.server as server_module
    from mcp_server.memory_guard import MemoryWriteGuard

    client = FakeGdbClient()
    guard = MemoryWriteGuard()
    monkeypatch.setattr(server_module, "gdb_client", client)
    monkeypatch.setattr(server_module, "debug_profile", FakeProfile())
    monkeypatch.setattr(server_module, "memory_guard", guard, raising=False)
    return client, guard


# --- 1. the guard applies to BOTH write tools ---------------------------------

def test_typed_memory_write_is_blocked_in_a_protected_region(session):
    payload = _payload(asyncio.run(handle_call_tool(
        "typed_memory", {"action": "write", "address": IWDG_KEY,
                         "value": "0x5555", "width_bits": 32})))

    assert payload["ok"] is False
    assert payload["error"]["code"] == "memory_write_blocked"


def test_write_memory_and_typed_memory_agree_about_the_same_address(session):
    # The defect was not "typed writes are unguarded" in the abstract -- it was that
    # the SAME address got two different answers depending on which tool you used.
    typed = _payload(asyncio.run(handle_call_tool(
        "typed_memory", {"action": "write", "address": IWDG_KEY,
                         "value": "0x5555", "width_bits": 32})))
    plain = _payload(asyncio.run(handle_call_tool(
        "write_memory", {"address": IWDG_KEY, "value": "0x5555"})))

    assert typed["ok"] == plain["ok"] is False
    assert typed["error"]["code"] == plain["error"]["code"]


def test_a_cast_expression_cannot_dodge_the_guard(session):
    # A literal-only check would be a bypass rather than a guard: the address is an
    # expression, so (char *)<protected> would sail past int(address, 0).
    client, _ = session
    client.expressions[f"(unsigned long)((char *){IWDG_KEY})"] = str(int(IWDG_KEY, 16))

    payload = _payload(asyncio.run(handle_call_tool(
        "typed_memory", {"action": "write", "address": f"(char *){IWDG_KEY}",
                         "value": "0x5555", "width_bits": 32})))

    assert payload["ok"] is False
    assert payload["error"]["code"] == "memory_write_blocked"


def test_an_unresolvable_address_is_refused_rather_than_written(session):
    # Fail closed: writing to an address the policy was never asked about is the
    # thing this whole change exists to stop.
    payload = _payload(asyncio.run(handle_call_tool(
        "typed_memory", {"action": "write", "address": "no_such_symbol",
                         "value": "1", "width_bits": 32})))

    assert payload["ok"] is False
    assert payload["error"]["code"] == "address_unresolved"


def test_a_permitted_typed_write_still_goes_through_and_is_audited(session):
    client, guard = session

    payload = _payload(asyncio.run(handle_call_tool(
        "typed_memory", {"action": "write", "address": "0x20000000",
                         "value": "0x1", "width_bits": 32})))

    assert payload["ok"] is True
    assert payload["data"]["resolved_address"] == "0x20000000"
    assert any(entry.get("tool") == "write_typed_memory" for entry in guard.get_audit_log())


# --- 2. an argument cannot smuggle a write past call_read ---------------------

@pytest.mark.parametrize("tool,args", [
    ("read_cycle_counter", {"enable": True}),
    ("sample_pc", {}),                       # enable defaults to true
    ("sample_pc", {"enable": True}),
    ("read_registers", {"what": "cycle", "enable": True}),
    # No discriminator at all must not ride the all-actions-read shortcut past the
    # per-argument guard on read_cycle_counter.
    ("read_registers", {"enable": True}),
])
def test_enabling_dwt_is_not_read_only(tool, args):
    # enable writes DEMCR.TRCENA, and GDB cannot write it while the core runs -- so
    # it halts a running target. That is not something to do without a prompt.
    assert read_only_dispatch_target(tool, args) is None


@pytest.mark.parametrize("tool,args", [
    ("read_cycle_counter", {}),
    ("read_cycle_counter", {"enable": False}),
    ("sample_pc", {"enable": False}),
    ("read_registers", {"what": "cycle"}),
    ("read_registers", {"what": "core"}),
])
def test_reading_without_enabling_stays_prompt_free(tool, args):
    # The point is to gate the argument, not to remove useful read-only calls: a
    # profiling run against an already-enabled DWT touches nothing.
    assert read_only_dispatch_target(tool, args) is not None


def test_every_arg_guarded_tool_is_actually_in_the_read_only_surface():
    # A guard on a tool that was never reachable would be dead code pretending to
    # be a control. Asserting the table is non-empty keeps emptying it from passing.
    from mcp_server.tool_surface import READ_ONLY_ARG_GUARDS, read_only_tool_names

    assert READ_ONLY_ARG_GUARDS
    assert set(READ_ONLY_ARG_GUARDS) <= read_only_tool_names()
