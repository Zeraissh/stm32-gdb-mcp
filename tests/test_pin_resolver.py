"""pin_resolver: layered physical-first pin resolution."""

from mcp_server.board_validation import PinCapabilityDB
from mcp_server.pin_resolver import resolve_board

DB = PinCapabilityDB({
    "_meta": {"source": "t", "generated_by": "t", "db_version": None,
              "ref_names": ["STM32L151CCUx"], "warnings": []},
    "STM32L151": {
        "PA2": [{"peripheral": "ADC", "signal": "IN2"},
                {"peripheral": "USART2", "signal": "TX", "af": 4}],
        "PA13": [{"peripheral": "SYS", "signal": "JTMS-SWDIO"}],
        "PA14": [{"peripheral": "SYS", "signal": "JTCK-SWCLK"}],
        "PA9": [{"peripheral": "USART1", "signal": "TX", "af": 7},
                {"peripheral": "TIM1", "signal": "CH2", "af": 1}],
        "_positions": {"STM32L151CCUx": {
            "7": "NRST", "12": "PA2", "30": "PA9", "34": "PA13", "37": "PA14"}},
    },
})


def _board(pins):
    return {
        "source": "test", "format": "altium",
        "mcu": {"ref": "U6", "part": "STM32L151CCU6", "part_normalized": "STM32L151CCU6",
                "family": "STM32L1", "line": "STM32L151", "pins": pins},
        "components": [], "nets": [], "power_nets": {"power": [], "ground": ["GND"]},
        "warnings": [], "stats": {},
    }


def test_db_hint_resolves_u2tx_via_physical_candidate():
    board = _board([{"package_pin": "12", "port_pin": None, "net": "U2TX", "function": None}])
    out = resolve_board(board, DB)

    pin = out["mcu"]["pins"][0]
    assert pin["port_pin"] == "PA2"
    assert pin["port_pin_source"] == "package_position"
    assert pin["function"] == {"peripheral": "USART2", "signal": "TX"}
    assert pin["function_source"] == "db_hint"
    assert board["mcu"]["pins"][0]["port_pin"] is None  # input untouched


def test_swd_pins_resolve_via_db_hint():
    board = _board([
        {"package_pin": "34", "port_pin": None, "net": "SW_DIO", "function": None},
        {"package_pin": "37", "port_pin": None, "net": "SW_CLK", "function": None},
    ])
    out = resolve_board(board, DB)
    dio, clk = out["mcu"]["pins"]

    assert dio["port_pin"] == "PA13"
    assert dio["function"] == {"peripheral": "SWD", "signal": "SWDIO"}
    assert clk["function"] == {"peripheral": "SWD", "signal": "SWCLK"}
    assert clk["function_source"] == "db_hint"


def test_fixed_function_reset_pin():
    board = _board([{"package_pin": "7", "port_pin": None, "net": "NRST", "function": None}])
    pin = resolve_board(board, DB)["mcu"]["pins"][0]

    assert pin["port_pin"] == "NRST"
    assert pin["function"] == {"peripheral": "SYS", "signal": "NRST"}
    assert pin["function_source"] == "package_position"


def test_name_inference_corroborated_by_db():
    board = _board([{"package_pin": "30", "port_pin": None, "net": "USART1_TX",
                     "function": {"peripheral": "USART1", "signal": "TX"}}])
    pin = resolve_board(board, DB)["mcu"]["pins"][0]

    assert pin["port_pin"] == "PA9"
    assert pin["function_source"] == "name+db"


def test_ambiguous_hint_leaves_function_none():
    # PA1 candidates share the hint token TX, so the hint cannot arbitrate.
    db = PinCapabilityDB({
        "STM32L0": {
            "PA1": [{"peripheral": "USART2", "signal": "TX"},
                    {"peripheral": "TIM2", "signal": "TX"}],
            "_positions": {"STM32L071CBTx": {"5": "PA1"}},
        },
    })
    board = _board([{"package_pin": "5", "port_pin": None, "net": "LINK_TX", "function": None}])
    board["mcu"].update(part="STM32L071CBT6", part_normalized="STM32L071CBT6",
                        family="STM32L0", line="STM32L0")
    pin = resolve_board(board, db)["mcu"]["pins"][0]

    assert pin["port_pin"] == "PA1"      # physical layer still resolves
    assert pin["function"] is None       # ambiguous hint: honest None


def test_no_position_map_keeps_everything_as_is():
    db = PinCapabilityDB({"STM32F4": {"PA1": []}})
    board = _board([{"package_pin": "12", "port_pin": None, "net": "U2TX", "function": None}])
    pin = resolve_board(board, db)["mcu"]["pins"][0]

    assert pin["port_pin"] is None
    assert pin["function"] is None


def test_netlist_port_pin_disagreement_is_a_note_not_an_override():
    board = _board([{"package_pin": "12", "port_pin": "PB7", "net": "U2TX", "function": None}])
    out = resolve_board(board, DB)
    pin = out["mcu"]["pins"][0]

    assert pin["port_pin"] == "PB7"  # netlist value kept
    assert any("PB7" in n and "PA2" in n for n in out["resolution_notes"])


def test_resolve_board_is_idempotent():
    board = _board([{"package_pin": "12", "port_pin": None, "net": "U2TX", "function": None}])
    once = resolve_board(board, DB)
    twice = resolve_board(once, DB)

    assert twice["mcu"]["pins"][0] == once["mcu"]["pins"][0]
