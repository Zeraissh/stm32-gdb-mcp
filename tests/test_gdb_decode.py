from mcp_server.gdb_decode import (
    decode_backtrace,
    decode_registers,
    decode_variables,
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


def test_decoders_tolerate_missing_payload():
    assert decode_backtrace([{"type": "console", "payload": "noise"}]) == []
    assert decode_variables([]) == {}
