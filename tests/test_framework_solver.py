from mcp_server.framework_solver import (
    build_framework_plan,
    classify_peripheral,
    gpio_role,
    merge_af_maps,
    parse_port_pin,
    summarize_framework,
)


def _fn(peripheral, signal):
    return {"peripheral": peripheral, "signal": signal}


def _pin(package_pin, port_pin, net, function=None):
    return {"package_pin": package_pin, "port_pin": port_pin, "net": net, "function": function}


def _board(pins, line="STM32L431", family="STM32L4"):
    return {
        "source": "<test>",
        "format": "kicad",
        "mcu": {
            "ref": "U1", "part": "STM32L431CBT6", "part_normalized": "STM32L431CBT6",
            "family": family, "line": line, "pins": pins,
        },
        "power_nets": {"power": ["+3V3"], "ground": ["GND"]},
        "nets": [],
    }


def _mixed_pins():
    return [
        _pin("42", "PA9", "/USART1_TX", _fn("USART1", "TX")),
        _pin("43", "PA10", "/USART1_RX", _fn("USART1", "RX")),
        _pin("22", "PB6", "/I2C1_SCL", _fn("I2C1", "SCL")),
        _pin("23", "PB7", "/I2C1_SDA", _fn("I2C1", "SDA")),
        _pin("15", "PA0", "/ADC1_IN5", _fn("ADC1", "IN5")),
        _pin("46", "PA13", "/SWDIO", _fn("SWD", "SWDIO")),
        _pin("49", "PA14", "/SWCLK", _fn("SWD", "SWCLK")),
        _pin("7", "NRST", "/NRST", _fn("SYS", "NRST")),
        _pin("1", None, "+3V3", None),
    ]


# --- unit helpers -----------------------------------------------------------


def test_classify_peripheral_covers_common_families():
    assert classify_peripheral("USART1") == "uart"
    assert classify_peripheral("LPUART1") == "uart"
    assert classify_peripheral("UART4") == "uart"
    assert classify_peripheral("I2C2") == "i2c"
    assert classify_peripheral("SPI1") == "spi"
    assert classify_peripheral("TIM3") == "timer"
    assert classify_peripheral("ADC1") == "adc"
    assert classify_peripheral("FDCAN1") == "can"
    assert classify_peripheral("SWD") == "debug"
    assert classify_peripheral("SYS") == "system"
    assert classify_peripheral("WHATSIT9") == "other"


def test_parse_port_pin():
    assert parse_port_pin("PA9") == {"port": "A", "pin": 9}
    assert parse_port_pin("PB15") == {"port": "B", "pin": 15}
    assert parse_port_pin("NRST") is None
    assert parse_port_pin("PA16") is None  # out of range
    assert parse_port_pin(None) is None


def test_gpio_role_by_kind():
    assert gpio_role("i2c", "SCL") == "af_od"
    assert gpio_role("adc", "IN5") == "analog"
    assert gpio_role("uart", "TX") == "af_pp"
    assert gpio_role("debug", "SWDIO") == "skip"
    assert gpio_role("other", "FOO") == "unknown"


# --- plan assembly ----------------------------------------------------------


def test_plan_enables_clocks_for_used_ports_and_peripherals():
    plan = build_framework_plan(_board(_mixed_pins()))
    macros = {c["hal_macro"] for c in plan["clocks"]}

    # GPIO port clocks (PA + PB used) and peripheral clocks, no debug/reset clocks.
    assert "__HAL_RCC_GPIOA_CLK_ENABLE" in macros
    assert "__HAL_RCC_GPIOB_CLK_ENABLE" in macros
    assert "__HAL_RCC_USART1_CLK_ENABLE" in macros
    assert "__HAL_RCC_I2C1_CLK_ENABLE" in macros
    assert "__HAL_RCC_ADC1_CLK_ENABLE" in macros
    assert not any("SWD" in m or "SYS" in m for m in macros)


def test_plan_gpio_roles_and_modes():
    plan = build_framework_plan(_board(_mixed_pins()))
    by_pin = {g["port_pin"]: g for g in plan["gpio"]}

    assert by_pin["PA9"]["hal_mode"] == "GPIO_MODE_AF_PP"      # USART TX
    assert by_pin["PB6"]["hal_mode"] == "GPIO_MODE_AF_OD"      # I2C SCL open-drain
    assert by_pin["PA0"]["hal_mode"] == "GPIO_MODE_ANALOG"     # ADC input
    assert by_pin["PA0"]["speed"] is None                      # analog has no speed
    # Debug/reset pins are never emitted as GPIO config.
    assert "PA13" not in by_pin and "NRST" not in by_pin


def test_gpio_sorted_by_port_then_pin():
    plan = build_framework_plan(_board(_mixed_pins()))
    order = [(g["port"], g["pin"]) for g in plan["gpio"]]
    assert order == sorted(order)


def test_af_unknown_is_surfaced_not_invented():
    plan = build_framework_plan(_board(_mixed_pins()))
    tx = next(g for g in plan["gpio"] if g["port_pin"] == "PA9")

    assert tx["af"] is None
    assert tx["hal_alternate"] is None
    assert any(u["type"] == "af_unknown" and u["port_pin"] == "PA9" for u in plan["unresolved"])


def test_af_map_resolves_alternate_function():
    af_map = {"STM32L431": {"PA9": {"USART1_TX": 7}, "PA10": {"USART1_RX": 7}}}
    plan = build_framework_plan(_board(_mixed_pins()), af_map=af_map)
    tx = next(g for g in plan["gpio"] if g["port_pin"] == "PA9")

    assert tx["af"] == 7
    assert tx["hal_alternate"] == "GPIO_AF7_USART1"
    assert not any(u["type"] == "af_unknown" and u["port_pin"] == "PA9" for u in plan["unresolved"])


def test_analog_pin_needs_no_af():
    plan = build_framework_plan(_board(_mixed_pins()))
    assert not any(u["type"] == "af_unknown" and u["port_pin"] == "PA0" for u in plan["unresolved"])


def test_design_config_maps_to_hal_init_fields():
    design = {"USART1": {"baud": 115200, "word_length": "UART_WORDLENGTH_8B"}}
    plan = build_framework_plan(_board(_mixed_pins()), design=design)
    usart = next(b for b in plan["peripherals"] if b["name"] == "USART1")

    assert usart["handle"] == "huart1"
    assert usart["hal_type"] == "UART_HandleTypeDef"
    assert usart["init_fn"] == "MX_USART1_UART_Init"
    assert usart["has_config"] is True
    fields = {f["field"]: f for f in usart["config_fields"]}
    assert fields["BaudRate"]["rendered"] == "115200"
    assert fields["BaudRate"]["mapped"] is True
    assert fields["WordLength"]["value"] == "UART_WORDLENGTH_8B"


def test_peripheral_without_config_is_unresolved_but_present():
    plan = build_framework_plan(_board(_mixed_pins()))
    names = [b["name"] for b in plan["peripherals"]]

    assert names == ["ADC1", "I2C1", "USART1"]  # sorted, debug/sys excluded
    assert any(u["type"] == "no_config" and u["peripheral"] == "SPI1" for u in plan["unresolved"]) is False
    assert any(u["type"] == "no_config" and u["peripheral"] == "USART1" for u in plan["unresolved"])


def test_init_order_is_clocks_then_gpio_then_peripherals():
    plan = build_framework_plan(_board(_mixed_pins()))
    assert plan["init_order"][:2] == ["SystemClock_Config", "MX_GPIO_Init"]
    assert "MX_USART1_UART_Init" in plan["init_order"]


def test_unknown_handle_index_falls_back_to_name():
    pins = [_pin("30", "PC0", "/OTG_FS_DP", _fn("USB", "DP"))]
    plan = build_framework_plan(_board(pins))
    usb = next(b for b in plan["peripherals"] if b["name"] == "USB")
    assert usb["handle"] == "hpcdusb"  # prefix + fallback name (no trailing index)


def test_port_pin_unknown_is_surfaced():
    pins = [_pin("42", "AF9", "/USART1_TX", _fn("USART1", "TX"))]  # AF9 is not a P<port><n>
    plan = build_framework_plan(_board(pins))
    assert any(u["type"] == "port_pin_unknown" for u in plan["unresolved"])
    assert plan["gpio"] == []


def test_no_mcu_returns_empty_plan_with_warning():
    plan = build_framework_plan({"mcu": None})
    assert plan["peripherals"] == []
    assert plan["gpio"] == []
    assert any("No MCU" in w for w in plan["warnings"])


def test_summarize_framework_is_compact():
    plan = build_framework_plan(_board(_mixed_pins()))
    summary = summarize_framework(plan)

    assert summary["mcu"]["line"] == "STM32L431"
    assert "__HAL_RCC_USART1_CLK_ENABLE" in summary["clocks"]
    assert {p["name"] for p in summary["peripherals"]} == {"ADC1", "I2C1", "USART1"}
    assert summary["stats"]["gpio_count"] == len(plan["gpio"])


# --- af_map merging (DB-derived + explicit override) ------------------------


def test_merge_af_maps_override_wins_and_base_preserved():
    base = {"STM32L431": {"PA9": {"USART1_TX": 7}, "PB6": {"I2C1_SCL": 4}}}
    override = {"STM32L431": {"PA9": {"USART1_TX": 99, "TIM1_CH2": 1}}}

    merged = merge_af_maps(base, override)

    assert merged == {
        "STM32L431": {
            "PA9": {"USART1_TX": 99, "TIM1_CH2": 1},
            "PB6": {"I2C1_SCL": 4},
        }
    }
    # Inputs are never mutated.
    assert base["STM32L431"]["PA9"] == {"USART1_TX": 7}


def test_merge_af_maps_handles_none_inputs():
    assert merge_af_maps(None, None) == {}
    assert merge_af_maps(None, {"F4": {"PA0": {"X_Y": 1}}}) == {"F4": {"PA0": {"X_Y": 1}}}
    assert merge_af_maps({"F4": {"PA0": {"X_Y": 1}}}, None) == {"F4": {"PA0": {"X_Y": 1}}}
