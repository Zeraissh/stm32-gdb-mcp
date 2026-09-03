"""pin_data_import: parse ST open_pin_data / CubeMX db XML into capability DB JSON."""

import pytest

from mcp_server.pin_data_import import (
    normalize_port_pin,
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
