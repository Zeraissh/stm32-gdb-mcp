"""sample_pc must be usable by default, and must never sell zeros as a hot spot.

Both defects measured on hardware (STM32L151, firmware running normally in
delay_ms), sample_pc(count=48):

    core state | enable         | result
    -----------+----------------+---------------------------------------------
    RUNNING    | true (DEFAULT) | FAIL "Cannot execute this command while the
               |                |  target is running" (the DEMCR write)
    RUNNING    | false          | ok, 48/48, hotspots=[{'0x00000000', 100.0%}]

The second is the dangerous one. Those zeros were counted as program counters.
On this rig the cause turned out to be broader than "trace unit off": EVERY
memory read returns 0 while the core runs, DEMCR included, though it reads
0x01000000 the instant the core halts.
"""

import asyncio
import json

import pytest

from mcp_server import dwt
from mcp_server.gdb_client import GdbClientManager
from mcp_server.server import handle_call_tool


def _payload(result):
    return json.loads(result[0].text)


# --------------------------------------------------------- sample decoding


def test_zero_reads_are_not_program_counters():
    # THE regression: 48 reads of 0x00000000 reported as a 100% hot spot at 0.
    profile = dwt.build_pc_profile([0x00000000] * 48, lambda pc: "")

    assert profile["sampled"] == 0
    assert profile["zero_samples"] == 48
    assert profile["hotspots"] == []
    assert profile["hot_addresses"] == []


def test_unsampleable_and_zero_reads_are_counted_separately():
    # "core is asleep" and "trace unit is off" need different fixes, so they must
    # not be reported as the same number.
    profile = dwt.build_pc_profile(
        [dwt.PCSR_UNSAMPLEABLE] * 3 + [0x00000000] * 5 + [0x08004F76] * 2, lambda pc: "delay_ms")

    assert profile["unsampleable"] == 3
    assert profile["zero_samples"] == 5
    assert profile["sampled"] == 2
    assert profile["hotspots"][0]["function"] == "delay_ms"


def test_a_single_repeated_address_is_still_a_valid_hot_spot():
    # Deliberately NOT rejected: one address repeated is the signature of a tight
    # loop or a `b .` park, which is exactly what this profiler is for. Rejecting
    # all-identical sets would break the headline use case.
    profile = dwt.build_pc_profile([0x08004F76] * 40, lambda pc: "HardFault_Handler")

    assert profile["sampled"] == 40
    assert profile["hotspots"] == [
        {"function": "HardFault_Handler", "samples": 40, "percent": 100.0}]


def test_real_samples_survive_alongside_invalid_ones():
    profile = dwt.build_pc_profile(
        [0x08004F76, 0x00000000, 0x08004F88, dwt.PCSR_UNSAMPLEABLE], lambda pc: "delay_ms")

    assert profile["sampled"] == 2
    assert profile["hotspots"][0]["samples"] == 2


# ------------------------------------------------------- enable ordering


class _Client(GdbClientManager):
    """Real profile_pc/enable_pc_sampling over a scripted target."""

    def __init__(self, running, trcena, write_fails_while_running=True):
        super().__init__()
        self.events = []
        self._running_state = running
        self._trcena = trcena
        self._write_fails_while_running = write_fails_while_running

    def is_running(self):
        return self._running_state

    def halt_execution(self):
        self.events.append("halt")
        self._running_state = False
        return []

    def continue_execution(self):
        self.events.append("continue")
        self._running_state = True
        return []

    def read_word(self, address):
        addr = int(address, 0) if isinstance(address, str) else address
        if addr == dwt.DEMCR:
            self.events.append("read_demcr")
            return dwt.DEMCR_TRCENA if self._trcena else 0
        # DWT_PCSR
        return 0x08004F76 if self._trcena else 0x00000000

    def write_typed_memory(self, address, value, width_bits=32):
        if self._running_state and self._write_fails_while_running:
            raise RuntimeError("Cannot execute this command while the target is running.")
        self.events.append(f"write:{address}")
        self._trcena = True
        return []

    def symbolize_pc(self, pc):
        return "delay_ms"


def test_the_default_path_works_on_a_running_core():
    # The documented happy path -- resume, then sample_pc -- used to always fail.
    client = _Client(running=True, trcena=False)

    profile = client.profile_pc(count=8)

    assert profile["sampled"] == 8
    assert profile["hotspots"][0]["function"] == "delay_ms"
    assert client.events[:1] == ["read_demcr"]
    assert "halt" in client.events and "continue" in client.events


def test_the_core_is_left_running_afterwards():
    client = _Client(running=True, trcena=False)
    client.profile_pc(count=4)

    assert client.is_running() is True
    assert client.events.index("halt") < client.events.index("continue")


def test_an_already_enabled_target_is_never_halted():
    # The profiler advertises itself as non-intrusive; a second call must cost
    # nothing but reads.
    client = _Client(running=True, trcena=True)

    profile = client.profile_pc(count=4)

    assert "halt" not in client.events
    assert not any(e.startswith("write:") for e in client.events)
    assert profile["trace"] == {"enabled": True, "wrote": False, "halted_for_enable": False}
    assert profile["sampled"] == 4


def test_a_halted_core_needs_no_halting_to_enable():
    client = _Client(running=False, trcena=False)
    client.profile_pc(count=4)

    assert "halt" not in client.events
    assert "continue" not in client.events
    assert any(e.startswith("write:") for e in client.events)


def test_enable_false_still_skips_the_trace_setup():
    client = _Client(running=True, trcena=False)
    profile = client.profile_pc(count=6, enable=False)

    assert client.events == []
    assert profile["trace"]["enabled"] is None
    # ...and the zeros it then reads are counted as zero_samples, not a hot spot.
    assert profile["sampled"] == 0
    assert profile["zero_samples"] == 6
    assert profile["hotspots"] == []


# ------------------------------------------------------------- tool layer


class _ToolClient:
    def __init__(self, profile):
        self._profile = profile

    def profile_pc(self, count=128, enable=True):
        return dict(self._profile)


def _sample(monkeypatch, profile):
    import mcp_server.server as server_module

    monkeypatch.setattr(server_module, "gdb_client", _ToolClient(profile))
    return _payload(asyncio.run(handle_call_tool("sample_pc", {"count": 48})))


def test_the_tool_says_nothing_was_profiled_when_trace_was_off(monkeypatch, default_session):
    payload = _sample(monkeypatch, dwt.build_pc_profile([0] * 48, lambda pc: ""))

    message = payload["data"]["message"]
    assert "trace unit was not enabled" in message
    assert "not a program counter" in message
    assert payload["data"]["hotspots"] == []


def test_zeros_despite_an_enabled_trace_unit_blame_the_link_not_the_trace_unit(
        monkeypatch, default_session):
    # The case the hardware actually produced: TRCENA verified set (DEMCR reads
    # 0x01000000 when halted) yet every sample reads 0, because this link cannot
    # serve memory reads while the core runs. Telling the user to "enable the
    # trace unit" there would send them in circles.
    profile = dwt.build_pc_profile([0] * 48, lambda pc: "")
    profile["trace"] = {"enabled": True, "wrote": True, "halted_for_enable": True}

    payload = _sample(monkeypatch, profile)

    message = payload["data"]["message"]
    assert "even though the trace unit IS enabled" in message
    assert "cannot serve memory reads while the core runs" in message
    assert "read_call_stack" in payload["suggested_next_actions"]
    assert payload["data"]["hotspots"] == []


def test_a_halted_core_still_gets_its_own_message(monkeypatch, default_session):
    payload = _sample(monkeypatch,
                      dwt.build_pc_profile([dwt.PCSR_UNSAMPLEABLE] * 48, lambda pc: ""))

    assert "core is halted, in WFI/sleep" in payload["data"]["message"]
    assert "continue_execution" in payload["suggested_next_actions"]


def test_a_real_profile_still_reports_its_hot_spot(monkeypatch, default_session):
    payload = _sample(monkeypatch,
                      dwt.build_pc_profile([0x08004F76] * 48, lambda pc: "delay_ms"))

    assert "hottest: delay_ms (100.0%)" in payload["data"]["message"]


@pytest.mark.parametrize("bad", [0x00000000, 0xFFFFFFFF])
def test_neither_impossible_value_ever_becomes_a_hotspot(bad):
    assert dwt.build_pc_profile([bad] * 10, lambda pc: "")["hotspots"] == []
