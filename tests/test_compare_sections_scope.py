"""compare-sections must ask the right question: read-only sections only.

A writable section (.data / RW_IRAM1) holds the ELF's INITIAL values. The moment
firmware runs it writes to its own variables, so comparing that section against
the file mismatches on every target that has executed. Comparing it anyway made
load_symbols declare perfectly good symbols meaningless and advise re-flashing --
a destructive remedy for a non-problem, and the mirror image of the false-success
failures this server exists to avoid.

Measured on hardware (STM32L151, firmware byte-identical to the loaded ELF):
    compare-sections       ER_IROM1 matched / RW_IRAM1 MIS-MATCHED!
    compare-sections -r    ER_IROM1 matched            (clean)
"""

import asyncio
import json

import pytest

from mcp_server.gdb_client import GdbClientManager, GdbCommandError
from mcp_server.server import handle_call_tool

# The exact console text the real hardware produced.
RO_MATCHED = {"type": "console",
              "payload": "Section ER_IROM1, range 0x8000000 -- 0x8008b18: matched.\n"}
RW_MISMATCHED = {"type": "console",
                 "payload": "Section RW_IRAM1, range 0x20000000 -- 0x200004a4: MIS-MATCHED!\n"}
RO_MISMATCHED = {"type": "console",
                 "payload": "Section ER_IROM1, range 0x8000000 -- 0x8008b18: MIS-MATCHED!\n"}
DONE = {"type": "result", "message": "done", "payload": None}


class SectionGdb:
    """Answers compare-sections and compare-sections -r differently, as GDB does."""

    def __init__(self, all_records, ro_records, ro_error=None):
        self.commands = []
        self._all = all_records
        self._ro = ro_records
        self._ro_error = ro_error

    def write(self, command, timeout_sec=1.0, raise_error_on_timeout=True):
        self.commands.append(command)
        if command.startswith("compare-sections -r"):
            if self._ro_error:
                raise RuntimeError(self._ro_error)
            return list(self._ro)
        if command.startswith("compare-sections"):
            return list(self._all)
        return [DONE]

    def get_gdb_response(self, timeout_sec=0.1, raise_error_on_timeout=False):
        return []


def _client(**kwargs):
    client = GdbClientManager()
    client.gdb = SectionGdb(
        all_records=kwargs.pop("all_records", [RO_MATCHED, RW_MISMATCHED, DONE]),
        ro_records=kwargs.pop("ro_records", [RO_MATCHED, DONE]),
        **kwargs,
    )
    return client


# ------------------------------------------------------------ the core fix


def test_a_mismatched_ram_section_alone_is_not_a_mismatch():
    # THE regression: code matches, RAM differs because the firmware ran.
    report = _client().compare_sections_report()

    assert report["checked"] is True
    assert report["mismatched"] == []
    assert report["scope"] == "read_only"


def test_read_only_is_the_default_and_uses_the_r_flag():
    client = _client()
    client.compare_sections_report()

    assert client.gdb.commands == ["compare-sections -r"]


def test_a_genuinely_mismatched_code_section_is_still_caught():
    report = _client(ro_records=[RO_MISMATCHED, DONE]).compare_sections_report()

    assert report["checked"] is True
    assert len(report["mismatched"]) == 1
    assert "ER_IROM1" in report["mismatched"][0]


def test_the_full_comparison_is_still_available_on_request():
    client = _client()
    report = client.compare_sections_report(read_only=False)

    assert client.gdb.commands == ["compare-sections"]
    assert report["scope"] == "all"
    assert len(report["mismatched"]) == 1
    assert "RW_IRAM1" in report["mismatched"][0]


def test_an_old_gdb_without_r_falls_back_rather_than_losing_the_check():
    client = _client(ro_error="Undefined compare-sections command: -r")
    report = client.compare_sections_report()

    assert report["checked"] is True
    assert report["scope"] == "all"
    assert "degraded_from_read_only" in report
    assert client.gdb.commands == ["compare-sections -r", "compare-sections"]


def test_a_link_failure_is_still_reported_not_swallowed():
    class Dead:
        def write(self, command, timeout_sec=1.0, raise_error_on_timeout=True):
            raise RuntimeError("target is not connected")

        def get_gdb_response(self, timeout_sec=0.1, raise_error_on_timeout=False):
            return []

    client = GdbClientManager()
    client.gdb = Dead()
    report = client.compare_sections_report()

    assert report["checked"] is False
    assert "not connected" in report["reason"]


# ------------------------------------------------------------- verify_flash


def test_verify_flash_passes_when_only_ram_differs():
    # Before the fix this raised on any board that had run its firmware.
    client = _client()
    client.verify_flash("fw.axf")

    assert "compare-sections -r" in client.gdb.commands


def test_verify_flash_still_fails_on_a_real_flash_mismatch():
    client = _client(ro_records=[RO_MISMATCHED, DONE])
    with pytest.raises(GdbCommandError, match="does not match the ELF"):
        client.verify_flash("fw.axf")


def test_verify_flash_can_opt_into_writable_sections():
    client = _client()
    with pytest.raises(GdbCommandError) as excinfo:
        client.verify_flash("fw.axf", include_writable=True)

    assert "writable sections were included" in str(excinfo.value)
    assert "compare-sections" in client.gdb.commands


# -------------------------------------------------------------- tool layer


def _payload(result):
    return json.loads(result[0].text)


class _Client:
    def __init__(self, mismatched, scope="read_only", checked=True):
        self._report = {"checked": checked, "mismatched": mismatched,
                        "reason": None if checked else "no target", "records": [],
                        "scope": scope}
        self.read_only_arg = None

    def load_symbols(self, path):
        return [{"message": "loaded"}]

    def compare_sections_report(self, read_only=True):
        self.read_only_arg = read_only
        return dict(self._report)


def test_load_symbols_asks_only_about_read_only_sections(monkeypatch, default_session):
    import mcp_server.server as server_module

    client = _Client(mismatched=[])
    monkeypatch.setattr(server_module, "gdb_client", client)

    payload = _payload(asyncio.run(handle_call_tool("load_symbols", {"elf_path": "fw.axf"})))

    assert client.read_only_arg is True
    assert payload["data"]["symbols_match"] is True
    assert payload["data"]["compared_sections"] == "read_only"
    assert "WARNING" not in payload["data"]["message"]


def test_load_symbols_still_warns_when_code_really_differs(monkeypatch, default_session):
    import mcp_server.server as server_module

    monkeypatch.setattr(server_module, "gdb_client",
                        _Client(mismatched=["Section ER_IROM1 ...: MIS-MATCHED!"]))

    payload = _payload(asyncio.run(handle_call_tool("load_symbols", {"elf_path": "fw.axf"})))

    assert payload["data"]["symbols_match"] is False
    assert "read-only section(s) mismatched" in payload["data"]["message"]
    assert "flash_firmware" in payload["suggested_next_actions"]
