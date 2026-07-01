from mcp_server.board_validation import (
    PinCapabilityDB,
    validate_board,
)


def _fn(peripheral, signal):
    return {"peripheral": peripheral, "signal": signal}


def _pin(package_pin, port_pin, net, function=None):
    return {"package_pin": package_pin, "port_pin": port_pin, "net": net, "function": function}


def _board(pins, power=None, mcu=True):
    return {
        "source": "<test>",
        "format": "kicad",
        "mcu": (
            {
                "ref": "U1",
                "part": "STM32L431CBT6",
                "part_normalized": "STM32L431CBT6",
                "family": "STM32L4",
                "line": "STM32L431",
                "pins": pins,
            }
            if mcu
            else None
        ),
        "power_nets": power if power is not None else {"power": ["+3V3"], "ground": ["GND"]},
        "nets": [],
    }


def _clean_pins():
    return [
        _pin("42", "PA9", "/USART1_TX", _fn("USART1", "TX")),
        _pin("43", "PA10", "/USART1_RX", _fn("USART1", "RX")),
        _pin("46", "PA13", "/SWDIO", _fn("SWD", "SWDIO")),
        _pin("49", "PA14", "/SWCLK", _fn("SWD", "SWCLK")),
        _pin("7", "NRST", "/NRST", _fn("SYS", "NRST")),
        _pin("1", None, "+3V3", None),
        _pin("8", None, "GND", None),
    ]


def test_clean_board_has_no_errors_and_lists_unassigned_power_pins():
    report = validate_board(_board(_clean_pins()))

    assert report["ok"] is True
    assert report["conflicts"] == []
    assert report["warnings"] == []
    assert report["stats"]["unassigned_count"] == 2  # VDD + VSS power pins


def test_detects_pin_double_assignment():
    pins = _clean_pins() + [_pin("42", "PB6", "/I2C1_SCL", _fn("I2C1", "SCL"))]
    report = validate_board(_board(pins))

    kinds = {c["type"] for c in report["conflicts"]}
    assert "pin_double_assignment" in kinds
    assert report["ok"] is False
    conflict = next(c for c in report["conflicts"] if c["type"] == "pin_double_assignment")
    assert conflict["package_pin"] == "42"
    assert set(conflict["nets"]) == {"/USART1_TX", "/I2C1_SCL"}


def test_detects_duplicate_peripheral_signal():
    pins = _clean_pins() + [_pin("20", "PB6", "/MCU_USART1_TX", _fn("USART1", "TX"))]
    report = validate_board(_board(pins))

    conflict = next(c for c in report["conflicts"] if c["type"] == "duplicate_peripheral_signal")
    assert conflict["peripheral"] == "USART1"
    assert conflict["signal"] == "TX"
    assert set(conflict["package_pins"]) == {"42", "20"}
    assert report["ok"] is False


def test_detects_port_pin_double_assignment():
    pins = [
        _pin("42", "PA9", "/USART1_TX", _fn("USART1", "TX")),
        _pin("99", "PA9", "/TIM1_CH2", _fn("TIM1", "CH2")),
    ]
    report = validate_board(_board(pins))

    conflict = next(c for c in report["conflicts"] if c["type"] == "port_pin_double_assignment")
    assert conflict["port_pin"] == "PA9"
    assert report["ok"] is False


def test_warns_on_missing_critical_nets():
    pins = [_pin("42", "PA9", "/USART1_TX", _fn("USART1", "TX"))]
    report = validate_board(_board(pins, power={"power": [], "ground": []}))

    kinds = {w["type"] for w in report["warnings"]}
    assert kinds == {"no_power_net", "no_ground_net", "no_debug_pins", "no_reset_pin"}
    # Missing critical nets are warnings, not blocking errors.
    assert report["ok"] is True


def test_no_mcu_warns():
    report = validate_board(_board([], mcu=False))

    assert any(w["type"] == "no_mcu" for w in report["warnings"])


# --- Alternate-function legality (DB-backed) --------------------------------

_DB = PinCapabilityDB(
    {
        "STM32L431": {
            "PA9": [{"peripheral": "USART1", "signal": "TX"}, {"peripheral": "TIM1", "signal": "CH2"}],
            "PB6": [{"peripheral": "I2C1", "signal": "SCL"}, {"peripheral": "USART1", "signal": "TX"}],
        }
    }
)


def test_capability_db_supports_returns_true_false_none():
    assert _DB.supports("STM32L431", "STM32L4", "PA9", "USART1", "TX") is True
    assert _DB.supports("STM32L431", "STM32L4", "PA9", "SPI1", "MOSI") is False
    # Unknown pin and unknown line both degrade to None (never a false positive).
    assert _DB.supports("STM32L431", "STM32L4", "PZ9", "USART1", "TX") is None
    assert _DB.supports("STM32F407", "STM32F4", "PA9", "USART1", "TX") is None


def test_af_legality_flags_illegal_and_counts_unverified():
    pins = [
        _pin("42", "PA9", "/USART1_TX", _fn("USART1", "TX")),   # legal, known
        _pin("20", "PA9", "/SPI1_MOSI", _fn("SPI1", "MOSI")),   # illegal on PA9
        _pin("30", "PZ9", "/I2C1_SDA", _fn("I2C1", "SDA")),     # unknown pin -> unverified
    ]
    report = validate_board(_board(pins), capability_db=_DB)

    assert report["af_checked"] is True
    illegal = [c for c in report["conflicts"] if c["type"] == "illegal_af"]
    assert len(illegal) == 1
    assert illegal[0]["port_pin"] == "PA9"
    assert illegal[0]["peripheral"] == "SPI1"
    assert report["stats"]["unverified_af_pins"] == 1
    assert report["ok"] is False


def test_af_legality_skipped_without_db():
    report = validate_board(_board(_clean_pins()))

    assert report["af_checked"] is False
    assert report["stats"]["unverified_af_pins"] == 0
    assert not any(c["type"] == "illegal_af" for c in report["conflicts"])
