"""pin_data_import: parse ST open_pin_data / CubeMX db XML into capability DB JSON."""

import json
import subprocess
import sys
from pathlib import Path

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
<Mcu ClockTree="STM32L4" DBVersion="V3.0" Family="STM32L4" HasPowerPad="false" Line="STM32L4x1" Package="LQFP48" RefName="STM32L431C(B-C)Tx" xmlns="http://dummy.com">
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
    assert parsed["line"] == "STM32L4x1"
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


def test_parse_mcu_xml_malformed_raises_value_error():
    # Pins the CLI's `except ValueError` fatality contract: a raw
    # xml.etree.ParseError must never escape the parser.
    with pytest.raises(ValueError, match="malformed MCU XML"):
        parse_mcu_xml("<Mcu")


def test_parse_mcu_xml_derives_concrete_key_line_from_ref_name():
    parsed = parse_mcu_xml(MCU_XML_L4)

    # ST's Line attribute uses lowercase-x wildcards; the DB key must be the
    # concrete line from RefName, matching board_model.normalize_mcu_part.
    assert parsed["line"] == "STM32L4x1"
    assert parsed["key_line"] == "STM32L431"


def test_parse_mcu_xml_key_line_none_when_ref_name_unmatched():
    xml = MCU_XML_L4.replace('RefName="STM32L431C(B-C)Tx"', 'RefName="EVALBOARD-01"')

    parsed = parse_mcu_xml(xml)

    assert parsed["key_line"] is None
    assert parsed["line"] == "STM32L4x1"


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


def test_parse_gpio_modes_xml_malformed_raises_value_error():
    with pytest.raises(ValueError, match="malformed GPIO modes XML"):
        parse_gpio_modes_xml("<IP")


MCU_XML_L4_TWIN = (
    MCU_XML_L4.replace('RefName="STM32L431C(B-C)Tx"', 'RefName="STM32L431CCUx"')
    .replace(
        '<Pin Name="PA9" Position="21" Type="I/O">',
        '<Pin Name="PA10" Position="22" Type="I/O">',
    )
    .replace('Name="I2C1_SCL"', 'Name="I2C1_SDA"')
    .replace('Name="USART1_TX"', 'Name="USART1_RX"')
)


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

    assert db["_meta"]["warnings"] == ["no GPIO modes file for STM32L43x_gpio_v1_0 (STM32L431C(B-C)Tx)"]
    assert all("af" not in e for pin in db["STM32L431"].values() for e in pin)


def test_build_db_falls_back_to_line_when_ref_name_unmatched():
    xml = MCU_XML_L4.replace('RefName="STM32L431C(B-C)Tx"', 'RefName="EVALBOARD-01"')
    mcu = parse_mcu_xml(xml)

    db = build_db([mcu], {}, source="/data")

    assert "STM32L4x1" in db
    assert "STM32L431" not in db


def test_build_db_requires_line_or_family():
    mcu = parse_mcu_xml(MCU_XML_L4)
    mcu["key_line"] = None
    mcu["line"] = None
    mcu["family"] = None

    with pytest.raises(ValueError, match="line"):
        build_db([mcu], {}, source="/data")


# Same concrete line, different RefName, identical PA9 signals -- exercises the
# dedupe / af-backfill merge branch in build_db.
MCU_XML_L4_SAME_PINS = MCU_XML_L4.replace('RefName="STM32L431C(B-C)Tx"', 'RefName="STM32L431CBTx"')


def test_build_db_merges_duplicate_pair_across_ref_names():
    first = parse_mcu_xml(MCU_XML_L4)
    second = parse_mcu_xml(MCU_XML_L4_SAME_PINS)
    modes = parse_gpio_modes_xml(GPIO_MODES_L4)

    db = build_db([first, second], {"STM32L43x_gpio_v1_0": modes}, source="/data")

    # Exactly one entry for the duplicated (peripheral, signal) pair, with af.
    usart1_tx = [e for e in db["STM32L431"]["PA9"] if (e["peripheral"], e["signal"]) == ("USART1", "TX")]
    assert usart1_tx == [{"peripheral": "USART1", "signal": "TX", "af": 7}]
    assert db["_meta"]["ref_names"] == ["STM32L431C(B-C)Tx", "STM32L431CBTx"]


def test_build_db_backfills_af_from_later_ref_name():
    # First RefName's GPIO version has no modes file -> entry without af; the
    # second RefName's modes table backfills the same merged entry.
    first = parse_mcu_xml(MCU_XML_L4.replace('Version="STM32L43x_gpio_v1_0"', 'Version="STM32L43x_gpio_v9_9"'))
    second = parse_mcu_xml(MCU_XML_L4_SAME_PINS)
    modes = parse_gpio_modes_xml(GPIO_MODES_L4)

    db = build_db([first, second], {"STM32L43x_gpio_v1_0": modes}, source="/data")

    usart1_tx = [e for e in db["STM32L431"]["PA9"] if (e["peripheral"], e["signal"]) == ("USART1", "TX")]
    assert usart1_tx == [{"peripheral": "USART1", "signal": "TX", "af": 7}]
    assert db["_meta"]["warnings"] == ["no GPIO modes file for STM32L43x_gpio_v9_9 (STM32L431C(B-C)Tx)"]


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


REPO_ROOT = Path(__file__).resolve().parent.parent
CLI = REPO_ROOT / "scripts" / "import_pin_data.py"


def _make_source(root: Path) -> Path:
    mcu_dir = root / "mcu"
    (mcu_dir / "IP").mkdir(parents=True)
    (mcu_dir / "STM32L431C(B-C)Tx.xml").write_text(MCU_XML_L4, encoding="utf-8")
    (mcu_dir / "IP" / "GPIO-STM32L43x_gpio_v1_0_Modes.xml").write_text(GPIO_MODES_L4, encoding="utf-8")
    return root


def _run_cli(*args):
    return subprocess.run([sys.executable, str(CLI), *args], capture_output=True, text=True)


def test_cli_generates_loadable_db(tmp_path):
    source = _make_source(tmp_path / "src_root")
    out = tmp_path / "caps.json"

    result = _run_cli("--source", str(source), "--mcu", "stm32l431", "-o", str(out))

    assert result.returncode == 0, result.stderr
    db = json.loads(out.read_text(encoding="utf-8"))
    assert db["STM32L431"]["PA9"][0] == {"peripheral": "I2C1", "signal": "SCL", "af": 4}
    assert db["_meta"]["ref_names"] == ["STM32L431C(B-C)Tx"]


def test_cli_unknown_mcu_lists_close_matches(tmp_path):
    source = _make_source(tmp_path / "src_root")

    result = _run_cli("--source", str(source), "--mcu", "STM32L999", "-o", str(tmp_path / "x.json"))

    assert result.returncode == 1
    assert "STM32L431C(B-C)Tx" in result.stderr


def test_cli_missing_source_root_fails(tmp_path):
    result = _run_cli("--source", str(tmp_path / "nope"), "--all", "-o", str(tmp_path / "x.json"))

    assert result.returncode == 1
    assert "mcu" in result.stderr


# An unrelated MCU that a wildcard expansion must never select.
MCU_XML_F1 = (
    MCU_XML_L4.replace('RefName="STM32L431C(B-C)Tx"', 'RefName="STM32F103C8Tx"')
    .replace('Line="STM32L4x1"', 'Line="STM32F103"')
    .replace('Family="STM32L4"', 'Family="STM32F1"')
)

# Same wildcard stem shape with a different package suffix (Tx vs Ux).
MCU_XML_L4_UX = MCU_XML_L4.replace('RefName="STM32L431C(B-C)Tx"', 'RefName="STM32L431C(B-C)Ux"')


def test_cli_mcu_selector_matches_wildcard_stem(tmp_path):
    source = _make_source(tmp_path / "src_root")
    (source / "mcu" / "STM32F103C8Tx.xml").write_text(MCU_XML_F1, encoding="utf-8")
    out = tmp_path / "caps.json"

    # Documented invocation: STM32L431CC must expand-match STM32L431C(B-C)Tx.
    result = _run_cli("--source", str(source), "--mcu", "STM32L431CC", "-o", str(out))

    assert result.returncode == 0, result.stderr
    db = json.loads(out.read_text(encoding="utf-8"))
    assert db["_meta"]["ref_names"] == ["STM32L431C(B-C)Tx"]
    assert "STM32F103" not in db


def test_cli_mcu_selector_matches_raw_wildcard_stem(tmp_path):
    source = _make_source(tmp_path / "src_root")
    (source / "mcu" / "STM32L431C(B-C)Ux.xml").write_text(MCU_XML_L4_UX, encoding="utf-8")
    out = tmp_path / "caps.json"

    # Regression: the exact file stem must select that file even though the
    # stem also carries (B-C) wildcard expansions.
    result = _run_cli("--source", str(source), "--mcu", "STM32L431C(B-C)Tx", "-o", str(out))

    assert result.returncode == 0, result.stderr
    db = json.loads(out.read_text(encoding="utf-8"))
    assert db["_meta"]["ref_names"] == ["STM32L431C(B-C)Tx"]


def test_cli_mcu_selector_ambiguous_across_wildcard_files(tmp_path):
    source = _make_source(tmp_path / "src_root")
    (source / "mcu" / "STM32L431C(B-C)Ux.xml").write_text(MCU_XML_L4_UX, encoding="utf-8")

    result = _run_cli("--source", str(source), "--mcu", "STM32L431CC", "-o", str(tmp_path / "x.json"))

    assert result.returncode == 1
    assert "ambiguous" in result.stderr
    # Error message shows the real file stems, not expansions.
    assert "STM32L431C(B-C)Tx" in result.stderr
    assert "STM32L431C(B-C)Ux" in result.stderr


MCU_XML_POSITIONS = """\
<Mcu Family="STM32L1" Line="STM32L151/152" Package="UFQFPN48" RefName="STM32L151CCUx"
     DBVersion="V3.0" xmlns="http://dummy.com">
  <IP Name="GPIO" Version="STM32L152xC_gpio_v1_0"/>
  <Pin Name="NRST" Position="7" Type="Reset"/>
  <Pin Name="VSSA" Position="8" Type="Power"/>
  <Pin Name="PA2" Position="12" Type="I/O">
    <Signal Name="ADC_IN2"/>
    <Signal Name="USART2_TX"/>
  </Pin>
  <Pin Name="PC14-OSC32_IN" Position="3" Type="I/O">
    <Signal Name="RCC_OSC32_IN"/>
  </Pin>
  <Pin Name="BOOT0" Position="44" Type="Boot"/>
</Mcu>
"""


def test_parse_mcu_xml_extracts_positions_for_all_pin_types():
    mcu = parse_mcu_xml(MCU_XML_POSITIONS)

    assert mcu["positions"] == {
        "3": "PC14",   # I/O name normalized
        "7": "NRST",   # Reset pin kept
        "8": "VSSA",   # Power pin kept
        "12": "PA2",
        "44": "BOOT0",  # Boot pin kept
    }


def test_build_db_emits_positions_per_ref_name():
    mcu = parse_mcu_xml(MCU_XML_POSITIONS)
    db = build_db([mcu], {}, source="test")

    scope = db["STM32L151"]
    assert scope["_positions"] == {
        "STM32L151CCUx": {"3": "PC14", "7": "NRST", "8": "VSSA", "12": "PA2", "44": "BOOT0"}
    }
    # The pins table is untouched: only I/O pins with peripheral signals.
    assert {(e["peripheral"], e["signal"]) for e in scope["PA2"]} == {("ADC", "IN2"), ("USART2", "TX")}
    assert "NRST" not in scope  # non-I/O pins never enter the AF table
