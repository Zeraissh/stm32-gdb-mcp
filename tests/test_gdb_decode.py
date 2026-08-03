from mcp_server.gdb_decode import (
    decode_backtrace,
    decode_breakpoints,
    decode_console_text,
    decode_evaluated_value,
    decode_memory_bytes,
    decode_registers,
    decode_symbol_resolution,
    decode_variables,
    implausible_register_set,
    registers_summary,
)


def _result(key, value):
    return [{"type": "result", "message": "done", "payload": {key: value}}]


def test_decode_registers_maps_names_to_values():
    names = _result("register-names", ["r0", "r1", "", "pc", "xpsr"])
    values = _result("register-values", [
        {"number": "0", "value": "0x10"},
        {"number": "1", "value": "0x200040b0"},
        {"number": "3", "value": "0x8000058"},
        {"number": "4", "value": "0x01000000"},
    ])

    regs = decode_registers(names, values)

    assert regs == {
        "r0": "0x10",
        "r1": "0x200040b0",
        "pc": "0x8000058",
        "xpsr": "0x01000000",
    }


def test_decode_registers_falls_back_for_unnamed_numbers():
    names = _result("register-names", ["r0", ""])
    values = _result("register-values", [{"number": "1", "value": "0x5"}])

    regs = decode_registers(names, values)

    assert regs == {"r1": "0x5"}


def test_registers_summary_highlights_pc_lr_sp():
    summary = registers_summary({"pc": "0x8000058", "lr": "0xfffffff9", "sp": "0x200040b0"})

    assert "pc=0x8000058" in summary
    assert "lr=0xfffffff9" in summary
    assert "sp=0x200040b0" in summary


def test_decode_backtrace_returns_clean_frames():
    records = _result("stack", [
        {"level": "0", "addr": "0x08000046", "func": "trigger_divzero", "file": "main.c", "line": "21"},
        {"level": "1", "addr": "0x0800006e", "func": "main", "file": "main.c", "line": "33"},
    ])

    frames = decode_backtrace(records)

    assert frames == [
        {"level": 0, "func": "trigger_divzero", "file": "main.c", "line": 21, "addr": "0x08000046"},
        {"level": 1, "func": "main", "file": "main.c", "line": 33, "addr": "0x0800006e"},
    ]


def test_decode_variables_returns_name_value_map():
    records = _result("variables", [
        {"name": "i", "value": "42"},
        {"name": "g_divisor", "value": "0"},
    ])

    assert decode_variables(records) == {"i": "42", "g_divisor": "0"}


def test_decode_breakpoints_extracts_hit_counts_and_conditions():
    records = [{"type": "result", "message": "done", "payload": {"BreakpointTable": {"body": [
        {"number": "1", "func": "flash_write_buggy", "file": "main.c", "line": "14",
         "addr": "0x08000040", "enabled": "y", "times": "0", "original-location": "flash_write_buggy"},
        {"number": "2", "func": "do_thing", "file": "main.c", "line": "30",
         "addr": "0x08000100", "enabled": "y", "times": "5", "cond": "state == 2"},
    ]}}}]

    bps = decode_breakpoints(records)

    assert bps[0]["number"] == "1"
    assert bps[0]["hit_count"] == 0          # never reached -> path not taken
    assert bps[0]["func"] == "flash_write_buggy"
    assert bps[1]["hit_count"] == 5
    assert bps[1]["condition"] == "state == 2"
    assert bps[1]["enabled"] is True


def test_decode_breakpoints_handles_nested_bkpt_and_empty():
    nested = [{"payload": {"BreakpointTable": {"body": [{"bkpt": {"number": "3", "times": "2"}}]}}}]
    assert decode_breakpoints(nested)[0]["hit_count"] == 2
    assert decode_breakpoints([{"type": "console", "payload": "noise"}]) == []


def test_decoders_tolerate_missing_payload():
    assert decode_backtrace([{"type": "console", "payload": "noise"}]) == []
    assert decode_variables([]) == {}


def test_decode_evaluated_value_prefers_structured_payload():
    records = [{"type": "result", "payload": {"value": "0x1234"}}]
    assert decode_evaluated_value(records) == "0x1234"


def test_decode_memory_bytes_reads_mi_memory_contents():
    records = [{"type": "result", "payload": {"memory": [{"contents": "00112233"}]}}]
    assert decode_memory_bytes(records) == "00112233"


def test_decoded_values_ignore_empty_strings_and_continue_searching():
    values = [
        {"type": "result", "payload": {"value": "  "}},
        {"type": "result", "payload": {"value": "42"}},
    ]
    memory = [
        {"type": "result", "payload": {"memory": [{"contents": "  "}]}},
        {"type": "result", "payload": {"memory": [{"contents": "DEADBEEF"}]}},
    ]

    assert decode_evaluated_value(values) == "42"
    assert decode_memory_bytes(memory) == "DEADBEEF"


# --- issue #37: a register set that cannot have come from a halted Cortex-M ---

def _console(*lines):
    return [{"type": "log", "payload": "echo"}] + [
        {"type": "console", "payload": line} for line in lines
    ]


def test_all_zero_register_set_is_rejected():
    registers = {name: "0x0" for name in ("r0", "sp", "lr", "pc", "xpsr", "msp", "psp")}

    reason = implausible_register_set(registers)

    assert reason is not None
    assert "xPSR" in reason and "Thumb" in reason


def test_xpsr_with_thumb_bit_clear_is_rejected_even_with_a_plausible_pc():
    reason = implausible_register_set({"pc": "0x08000456", "xpsr": "0x00000003"})

    assert reason is not None and "Thumb" in reason


def test_pc_zero_is_rejected_even_when_xpsr_looks_sane():
    reason = implausible_register_set({"pc": "0x0", "xpsr": "0x01000000"})

    assert reason is not None and "pc=0x0" in reason


def test_empty_register_map_is_rejected():
    assert implausible_register_set({}) is not None


def test_a_real_halted_register_set_passes():
    # xPSR 0x61000000: Thumb bit set plus condition flags, as read off a halted L431.
    assert implausible_register_set({
        "pc": "0x08000456", "lr": "0xfffffff9", "sp": "0x20004fd0", "xpsr": "0x61000000",
    }) is None


def test_registers_without_xpsr_or_pc_are_not_second_guessed():
    # A partial reply is not evidence of a bad read; only impossible values are.
    assert implausible_register_set({"r0": "0x1", "r1": "0x2"}) is None


def test_unparsable_register_values_do_not_raise():
    assert implausible_register_set({"pc": "<unavailable>", "xpsr": "???"}) is None


# --- issue #40: info line / info symbol must be parsed, not discarded ---

def test_decode_console_text_joins_console_records_and_skips_the_echo():
    text = decode_console_text(_console("first\n", "second\n"))

    assert text == "first\nsecond"


def test_resolution_parses_line_file_symbol_offset_and_section():
    records = _console(
        'Line 412 of "boot.c" starts at address 0x8000c74 <Boot_ValidateStaging+148> '
        "and ends at 0x8000c78 <Boot_WriteState>.\n",
        "Boot_ValidateStaging + 148 in section .text\n",
    )

    out = decode_symbol_resolution(records)

    assert out["resolved"] is True
    assert out["symbol"] == "Boot_ValidateStaging"
    assert out["offset"] == 148
    assert out["section"] == ".text"
    assert out["file"] == "boot.c"
    assert out["line"] == 412


def test_resolution_handles_a_symbol_with_no_offset():
    out = decode_symbol_resolution(_console("Boot_WriteState in section .text\n"))

    assert out["symbol"] == "Boot_WriteState"
    assert out["offset"] == 0
    assert out["section"] == ".text"


def test_resolution_handles_section_qualified_by_object_file():
    out = decode_symbol_resolution(
        _console("Boot_WriteState + 4 in section .text of /tmp/fw.axf\n"))

    assert out["symbol"] == "Boot_WriteState"
    assert out["section"] == ".text"


def test_resolution_keeps_the_symbol_when_there_is_no_line_information():
    # ARMCC -O2 builds hit this constantly: no line table, but the symbol is right there.
    out = decode_symbol_resolution(_console(
        "No line number information available for address 0x8000c74 <Boot_ValidateStaging+148>\n"))

    assert out["resolved"] is True
    assert out["symbol"] == "Boot_ValidateStaging"
    assert out["offset"] == 148
    assert out["file"] is None and out["line"] is None


def test_resolution_reports_an_unresolvable_address_as_unresolved():
    out = decode_symbol_resolution(_console(
        "No line number information available for address 0x20000000\n",
        "No symbol matches 0x20000000.\n",
    ))

    assert out["resolved"] is False
    assert out["symbol"] is None
    assert "No symbol matches" in out["text"]


def test_symbol_resolution_handles_a_symbol_name_containing_spaces():
    records = [{"type": "console", "payload": "Foo::bar(int, int) + 8 in section .text\n"}]

    out = decode_symbol_resolution(records)

    assert out["resolved"] is True
    assert out["symbol"] == "Foo::bar(int, int)"
    assert out["offset"] == 8


def test_thumb_bit_check_survives_a_gdb_that_names_the_register_xPSR():
    reason = implausible_register_set({"pc": "0x0", "sp": "0x0", "xPSR": "0x0"})

    assert reason is not None and "Thumb" in reason


def test_a_failed_unwind_reporting_pc_zero_for_an_outer_frame_is_not_called_implausible():
    # -data-list-register-values reads the SELECTED frame; a broken unwind at the
    # bottom of a stack legitimately answers pc=0 while sp is still real.
    assert implausible_register_set({"pc": "0x0", "sp": "0x20004fd0"}) is None


def test_an_all_zero_set_is_still_rejected_via_pc_and_sp():
    assert implausible_register_set({"pc": "0x0", "sp": "0x0"}) is not None
