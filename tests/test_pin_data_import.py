"""pin_data_import: parse ST open_pin_data / CubeMX db XML into capability DB JSON."""

import json

import pytest

from mcp_server.board_validation import load_capability_db
from mcp_server.pin_data_import import (
    build_db,
    normalize_port_pin,
    parse_gpio_modes_xml,
    parse_mcu_xml,
    split_signal,
)

MCU_XML_L4 = """<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<Mcu ClockTree="STM32L4" DBVersion="V3.0" Family="STM32L4" HasPowerPad="false" Line="STM32L431" Package="LQFP48" RefName="STM32L431C(B-C)Tx" xmlns="http://dummy.com">
    <IP InstanceName="USART1" Name="USART" Version="sci2_v2_1_Cube"/>
    <IP ConfigFile="GPIO-STM32L4xx" InstanceName="GPIO" Name="GPIO" Version="STM32L43x_gpio_v1_0"/>
    <Pin Name="VBAT" Position="1" Type="Power"/>
    <Pin Name="PA0-WKUP1" Position="6" Type="I/O">
        <Signal Name="ADC1_IN5"/>
        <Signal Name="USART2_CTS"/>
        <Signal IOModes="Input,Output,Analog,EVENTOUT,EXTI" Name="GPIO"/>
    </Pin>
    <Pin Name="PA9" Position="21" Type="I/O">
        <Signal Name="I2C1_SCL"/>
        <Signal Name="USART1_TX"/>
        <Signal IOModes="Input,Output,Analog,EVENTOUT,EXTI" Name="GPIO"/>
    </Pin>
    <Pin Name="NRST" Position="4" Type="Reset"/>
</Mcu>
"""


def test_normalize_port_pin_strips_suffix():
    assert normalize_port_pin("PA0-WKUP1") == "PA0"
    assert normalize_port_pin("PC14-OSC32_IN") == "PC14"
    assert normalize_port_pin("PA9") == "PA9"


def test_split_signal_first_underscore():
    assert split_signal("ADC1_IN5") == ("ADC1", "IN5")
    assert split_signal("RCC_OSC32_IN") == ("RCC", "OSC32_IN")
    assert split_signal("GPIO") is None
    assert split_signal("EVENTOUT") is None
    assert split_signal("VBAT") is None


def test_parse_mcu_xml_header_and_pins():
    parsed = parse_mcu_xml(MCU_XML_L4)

    assert parsed["ref_name"] == "STM32L431C(B-C)Tx"
    assert parsed["family"] == "STM32L4"
    assert parsed["line"] == "STM32L431"
    assert parsed["package"] == "LQFP48"
    assert parsed["db_version"] == "V3.0"
    assert parsed["gpio_version"] == "STM32L43x_gpio_v1_0"
    # Non-I/O pins excluded.
    assert set(parsed["pins"]) == {"PA0", "PA9"}
    assert parsed["pins"]["PA9"] == [
        {"peripheral": "I2C1", "signal": "SCL"},
        {"peripheral": "USART1", "signal": "TX"},
    ]
    # GPIO signal skipped, suffix normalized, order preserved.
    assert parsed["pins"]["PA0"] == [
        {"peripheral": "ADC1", "signal": "IN5"},
        {"peripheral": "USART2", "signal": "CTS"},
    ]


def test_parse_mcu_xml_rejects_non_mcu_root():
    with pytest.raises(ValueError, match="Mcu"):
        parse_mcu_xml('<Foo xmlns="http://dummy.com"/>')


GPIO_MODES_L4 = """<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<IP DBVersion="V3.0" Name="GPIO" Version="STM32L43x_gpio_v1_0" xmlns="http://dummy.com">
    <GPIO_Pin PortName="PA" Name="PA9">
        <SpecificParameter Name="GPIO_Pin">
            <PossibleValue>GPIO_PIN_9</PossibleValue>
        </SpecificParameter>
        <PinSignal Name="I2C1_SCL">
            <SpecificParameter Name="GPIO_AF">
                <PossibleValue>GPIO_AF4_I2C1</PossibleValue>
            </SpecificParameter>
        </PinSignal>
        <PinSignal Name="USART1_TX">
            <SpecificParameter Name="GPIO_AF">
                <PossibleValue>GPIO_AF7_USART1</PossibleValue>
            </SpecificParameter>
        </PinSignal>
    </GPIO_Pin>
    <GPIO_Pin PortName="PA" Name="PA0-WKUP1">
        <PinSignal Name="ADC1_IN5"/>
    </GPIO_Pin>
</IP>
"""

GPIO_MODES_F1 = """<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<IP DBVersion="V3.0" Name="GPIO" Version="STM32F103x8_gpio_v1_0" xmlns="http://dummy.com">
    <GPIO_Pin PortName="PA" Name="PA0-WKUP">
        <PinSignal Name="TIM2_CH1">
            <RemapBlock Name="TIM2_REMAP0" DefaultRemap="true" />
            <RemapBlock Name="TIM2_REMAP2">
               <SpecificParameter Name="GPIO_AF">
                   <PossibleValue>__HAL_AFIO_REMAP_TIM2_PARTIAL_2</PossibleValue>
               </SpecificParameter>
            </RemapBlock>
        </PinSignal>
        <PinSignal Name="USART2_CTS">
            <RemapBlock Name="USART2_REMAP0" DefaultRemap="true" />
        </PinSignal>
    </GPIO_Pin>
</IP>
"""


def test_parse_gpio_modes_extracts_numeric_af():
    table = parse_gpio_modes_xml(GPIO_MODES_L4)

    assert table[("PA9", "I2C1_SCL")] == 4
    assert table[("PA9", "USART1_TX")] == 7
    # PinSignal without GPIO_AF parameter contributes nothing.
    assert ("PA0", "ADC1_IN5") not in table


def test_parse_gpio_modes_afio_remap_yields_no_numeric_af():
    # F1 uses the AFIO remap model; __HAL_AFIO_REMAP_* values are not numeric AFs.
    table = parse_gpio_modes_xml(GPIO_MODES_F1)

    assert table == {}


MCU_XML_L4_TWIN = MCU_XML_L4.replace(
    'RefName="STM32L431C(B-C)Tx"', 'RefName="STM32L431CCUx"'
).replace(
    '<Pin Name="PA9" Position="21" Type="I/O">',
    '<Pin Name="PA10" Position="22" Type="I/O">',
).replace('Name="I2C1_SCL"', 'Name="I2C1_SDA"').replace('Name="USART1_TX"', 'Name="USART1_RX"')


def test_build_db_keys_by_line_and_attaches_af():
    mcu = parse_mcu_xml(MCU_XML_L4)
    modes = parse_gpio_modes_xml(GPIO_MODES_L4)

    db = build_db([mcu], {"STM32L43x_gpio_v1_0": modes}, source="/data/open_pin_data")

    assert {frozenset(e.items()) for e in db["STM32L431"]["PA9"]} == {
        frozenset({"peripheral": "I2C1", "signal": "SCL", "af": 4}.items()),
        frozenset({"peripheral": "USART1", "signal": "TX", "af": 7}.items()),
    }
    # ADC1_IN5 has no GPIO_AF parameter -> entry without "af".
    assert db["STM32L431"]["PA0"][0] == {"peripheral": "ADC1", "signal": "IN5"}
    assert db["_meta"]["source"] == "/data/open_pin_data"
    assert db["_meta"]["db_version"] == "V3.0"
    assert db["_meta"]["ref_names"] == ["STM32L431C(B-C)Tx"]
    assert db["_meta"]["warnings"] == []


def test_build_db_unions_ref_names_sharing_a_line():
    first = parse_mcu_xml(MCU_XML_L4)
    twin = parse_mcu_xml(MCU_XML_L4_TWIN)

    db = build_db([first, twin], {}, source="/data")

    assert sorted(db["_meta"]["ref_names"]) == ["STM32L431C(B-C)Tx", "STM32L431CCUx"]
    assert "PA9" in db["STM32L431"] and "PA10" in db["STM32L431"]


def test_build_db_missing_modes_file_warns_and_omits_af():
    mcu = parse_mcu_xml(MCU_XML_L4)

    db = build_db([mcu], {}, source="/data")

    assert db["_meta"]["warnings"] == [
        "no GPIO modes file for STM32L43x_gpio_v1_0 (STM32L431C(B-C)Tx)"
    ]
    assert all("af" not in e for pin in db["STM32L431"].values() for e in pin)


def test_build_db_requires_line_or_family():
    mcu = parse_mcu_xml(MCU_XML_L4)
    mcu["line"] = None
    mcu["family"] = None

    with pytest.raises(ValueError, match="line"):
        build_db([mcu], {}, source="/data")


def test_generated_db_round_trips_through_capability_db(tmp_path):
    mcu = parse_mcu_xml(MCU_XML_L4)
    modes = parse_gpio_modes_xml(GPIO_MODES_L4)
    db = build_db([mcu], {"STM32L43x_gpio_v1_0": modes}, source="/data")
    path = tmp_path / "caps.json"
    path.write_text(json.dumps(db, indent=2, sort_keys=True), encoding="utf-8")

    caps = load_capability_db(str(path))

    assert caps.supports("STM32L431", "STM32L4", "PA9", "USART1", "TX") is True
    assert caps.supports("STM32L431", "STM32L4", "PA9", "SPI1", "MOSI") is False
    # Unknown pin degrades to None, never a false conflict.
    assert caps.supports("STM32L431", "STM32L4", "PB7", "USART1", "RX") is None

    af_map = caps.af_map()
    assert af_map["STM32L431"]["PA9"]["USART1_TX"] == 7
    assert af_map["STM32L431"]["PA9"]["I2C1_SCL"] == 4
    # Entries without af stay out of the projection.
    assert "ADC1_IN5" not in af_map["STM32L431"].get("PA0", {})
    # _meta must not leak into either consumer.
    assert caps.supports("_meta", None, "PA9", "USART1", "TX") is None
    assert "_meta" not in af_map
