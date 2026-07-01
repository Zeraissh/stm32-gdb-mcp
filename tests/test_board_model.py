from mcp_server.board_model import (
    build_board_description,
    classify_power_net,
    infer_pin_function,
    is_mcu_value,
    normalize_mcu_part,
)


def test_infer_pin_function_covers_common_buses():
    assert infer_pin_function("/USART1_TX") == {"peripheral": "USART1", "signal": "TX"}
    assert infer_pin_function("UART4_RX") == {"peripheral": "UART4", "signal": "RX"}
    assert infer_pin_function("I2C1_SCL") == {"peripheral": "I2C1", "signal": "SCL"}
    assert infer_pin_function("SPI2_MOSI") == {"peripheral": "SPI2", "signal": "MOSI"}
    assert infer_pin_function("TIM3_CH1") == {"peripheral": "TIM3", "signal": "CH1"}
    assert infer_pin_function("TIM1_CH1N") == {"peripheral": "TIM1", "signal": "CH1N"}
    assert infer_pin_function("ADC1_IN5") == {"peripheral": "ADC1", "signal": "IN5"}


def test_infer_pin_function_system_pins_and_hierarchy_and_prefix():
    assert infer_pin_function("SWDIO") == {"peripheral": "SWD", "signal": "SWDIO"}
    assert infer_pin_function("NRST") == {"peripheral": "SYS", "signal": "NRST"}
    assert infer_pin_function("BOOT0") == {"peripheral": "SYS", "signal": "BOOT0"}
    # hierarchical prefix and a leading component prefix both resolve
    assert infer_pin_function("/power/OSC_IN") == {"peripheral": "RCC", "signal": "OSC_IN"}
    assert infer_pin_function("MCU_I2C1_SDA") == {"peripheral": "I2C1", "signal": "SDA"}


def test_infer_pin_function_returns_none_for_unknown():
    assert infer_pin_function("GND") is None
    assert infer_pin_function("+3V3") is None
    assert infer_pin_function("NET0042") is None
    assert infer_pin_function("") is None
    assert infer_pin_function(None) is None


def test_normalize_mcu_part_extracts_family_and_line():
    assert normalize_mcu_part("STM32L431CBT6") == {
        "part": "STM32L431CBT6",
        "part_normalized": "STM32L431CBT6",
        "family": "STM32L4",
        "line": "STM32L431",
    }
    assert normalize_mcu_part("STM32F407VGT6")["family"] == "STM32F4"
    assert normalize_mcu_part("STM32F407VGT6")["line"] == "STM32F407"
    assert normalize_mcu_part("STM32H743ZIT6")["line"] == "STM32H743"
    assert normalize_mcu_part("LM358") is None
    assert normalize_mcu_part(None) is None


def test_is_mcu_value():
    assert is_mcu_value("STM32L431CBT6") is True
    assert is_mcu_value("10k") is False
    assert is_mcu_value(None) is False


def test_classify_power_net():
    assert classify_power_net("GND") == "ground"
    assert classify_power_net("VSSA") == "ground"
    assert classify_power_net("+3V3") == "power"
    assert classify_power_net("VDD") == "power"
    assert classify_power_net("VDDA") == "power"
    assert classify_power_net("3.3V") == "power"
    assert classify_power_net("/USART1_TX") is None


def test_build_board_description_identifies_mcu_and_infers_pins():
    components = [
        {"ref": "U1", "value": "STM32L431CBT6", "footprint": "LQFP-48", "pins": {}},
        {"ref": "J1", "value": "USB_C", "footprint": "Conn", "pins": {}},
    ]
    nets = [
        {"name": "/USART1_TX", "nodes": [{"ref": "U1", "pin": "42", "port_pin": "PA9"}, {"ref": "J1", "pin": "3"}]},
        {"name": "/I2C1_SCL", "nodes": [{"ref": "U1", "pin": "45", "port_pin": "PB6"}]},
        {"name": "+3V3", "nodes": [{"ref": "U1", "pin": "1"}]},
        {"name": "GND", "nodes": [{"ref": "U1", "pin": "8"}]},
    ]

    board = build_board_description(components, nets, source="board.net", fmt="kicad")

    assert board["source"] == "board.net"
    assert board["format"] == "kicad"
    assert board["mcu"]["ref"] == "U1"
    assert board["mcu"]["family"] == "STM32L4"
    assert board["mcu"]["line"] == "STM32L431"

    pin_by_net = {p["net"]: p for p in board["mcu"]["pins"]}
    assert pin_by_net["/USART1_TX"]["port_pin"] == "PA9"
    assert pin_by_net["/USART1_TX"]["function"] == {"peripheral": "USART1", "signal": "TX"}
    assert pin_by_net["/I2C1_SCL"]["function"] == {"peripheral": "I2C1", "signal": "SCL"}

    assert board["power_nets"] == {"power": ["+3V3"], "ground": ["GND"]}
    assert board["stats"] == {"component_count": 2, "net_count": 4, "mcu_pin_count": 4}
    assert board["warnings"] == []


def test_build_board_description_warns_when_no_mcu():
    components = [{"ref": "R1", "value": "10k", "footprint": "0402", "pins": {}}]
    nets = [{"name": "NET1", "nodes": [{"ref": "R1", "pin": "1"}]}]

    board = build_board_description(components, nets)

    assert board["mcu"] is None
    assert board["stats"]["mcu_pin_count"] == 0
    assert any("No STM32 MCU" in w for w in board["warnings"])


def test_build_board_description_warns_on_multiple_mcu_candidates():
    components = [
        {"ref": "U1", "value": "STM32L431CBT6", "footprint": "", "pins": {}},
        {"ref": "U2", "value": "STM32F407VGT6", "footprint": "", "pins": {}},
    ]
    board = build_board_description(components, [])

    assert board["mcu"]["ref"] == "U1"
    assert any("Multiple MCU candidates" in w for w in board["warnings"])
