"""Regression tests for issue #21: ok:true despite raw GDB errors.

The agent-facing contract is that ok:true means the operation actually
happened. GDB reports many hard failures outside the ``^error`` result record,
so the verdict has to come from the raw records, not from "the command returned".
"""

import pytest

from mcp_server.mi_guard import GdbCommandError, ensure_ok, find_mi_error, has_terminal_result

DONE = {"type": "result", "message": "done", "payload": None}


def test_mi_error_result_record_is_reported():
    records = [{"type": "result", "message": "error", "payload": {"msg": "No symbol table is loaded"}}]

    assert find_mi_error(records) == "No symbol table is loaded"


def test_flash_errors_hiding_in_stream_text_are_reported():
    # The real shape from issue #21: the MI command "completes" while the actual
    # failure is only visible in the log/console stream.
    records = [
        {"type": "log", "payload": "Error erasing flash with vFlashErase packet\n"},
        DONE,
    ]

    assert "Error erasing flash" in find_mi_error(records)


def test_missing_file_in_stream_text_is_reported():
    records = [{"type": "console", "payload": "fw.axf: No such file or directory."}, DONE]

    assert "No such file or directory" in find_mi_error(records)


def test_clean_records_produce_no_error():
    assert find_mi_error([{"type": "console", "payload": "Loading section .text\n"}, DONE]) is None
    assert find_mi_error([]) is None


def test_ensure_ok_raises_with_the_operation_name():
    records = [{"type": "log", "payload": "Error erasing flash\n"}, DONE]

    with pytest.raises(GdbCommandError, match=r"flash download\(fw\.elf\) failed"):
        ensure_ok(records, "flash download(fw.elf)")


def test_ensure_ok_requires_terminal_completion_for_transfers():
    # A download that returned before GDB reported completion must not read as
    # success — issue #21 saw flash_firmware return before the download finished.
    with pytest.raises(GdbCommandError, match="did not report completion"):
        ensure_ok([{"type": "console", "payload": "Loading section .text\n"}], "flash", require_result=True)

    assert ensure_ok([DONE], "flash", require_result=True) == [DONE]


def test_has_terminal_result_only_counts_result_records():
    assert has_terminal_result([DONE]) is True
    assert has_terminal_result([{"type": "console", "payload": "..."}]) is False
