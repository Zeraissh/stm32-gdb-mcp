import pytest

from mcp_server.netlist_parser import (
    detect_format,
    load_netlist_file,
    parse_kicad_netlist,
    parse_netlist,
)

KICAD_FIXTURE = """
(export (version "E")
  (components
    (comp (ref "U1") (value "STM32L431CBT6") (footprint "Package_QFP:LQFP-48"))
    (comp (ref "J1") (value "USB_C_Receptacle") (footprint "Connector:USB_C"))
    (comp (ref "Y1") (value "8MHz") (footprint "Crystal")))
  (nets
    (net (code "1") (name "/USART1_TX")
      (node (ref "U1") (pin "42") (pinfunction "PA9"))
      (node (ref "J1") (pin "3")))
    (net (code "2") (name "/I2C1_SCL")
      (node (ref "U1") (pin "45") (pinfunction "PB6")))
    (net (code "3") (name "+3V3")
      (node (ref "U1") (pin "1"))
      (node (ref "J1") (pin "1")))
    (net (code "4") (name "GND")
      (node (ref "U1") (pin "8"))
      (node (ref "J1") (pin "5")))))
"""


def test_detect_format():
    assert detect_format(KICAD_FIXTURE) == "kicad"
    assert detect_format("Component, Pin, Net\nU1, 1, GND") == "unknown"


def test_parse_kicad_components_and_pinmap():
    components, nets = parse_kicad_netlist(KICAD_FIXTURE)

    by_ref = {c["ref"]: c for c in components}
    assert by_ref["U1"]["value"] == "STM32L431CBT6"
    assert by_ref["U1"]["footprint"] == "Package_QFP:LQFP-48"
    # component pin -> net back-reference is populated
    assert by_ref["U1"]["pins"]["42"] == "/USART1_TX"
    assert by_ref["U1"]["pins"]["8"] == "GND"

    net_names = {n["name"] for n in nets}
    assert net_names == {"/USART1_TX", "/I2C1_SCL", "+3V3", "GND"}


def test_parse_kicad_captures_port_pin_from_pinfunction():
    _, nets = parse_kicad_netlist(KICAD_FIXTURE)
    tx = next(n for n in nets if n["name"] == "/USART1_TX")
    u1_node = next(node for node in tx["nodes"] if node["ref"] == "U1")
    assert u1_node["pin"] == "42"
    assert u1_node["port_pin"] == "PA9"


def test_parse_netlist_builds_board_description():
    board = parse_netlist(KICAD_FIXTURE)

    assert board["format"] == "kicad"
    assert board["mcu"]["ref"] == "U1"
    assert board["mcu"]["line"] == "STM32L431"

    pin_by_net = {p["net"]: p for p in board["mcu"]["pins"]}
    assert pin_by_net["/USART1_TX"]["function"] == {"peripheral": "USART1", "signal": "TX"}
    assert pin_by_net["/USART1_TX"]["port_pin"] == "PA9"
    assert pin_by_net["/I2C1_SCL"]["function"] == {"peripheral": "I2C1", "signal": "SCL"}

    assert board["power_nets"] == {"power": ["+3V3"], "ground": ["GND"]}
    assert board["stats"]["component_count"] == 3
    assert board["stats"]["net_count"] == 4


def test_parse_netlist_rejects_unknown_format():
    with pytest.raises(ValueError, match="Unsupported or undetected"):
        parse_netlist("Component, Pin, Net\nU1, 1, GND")


def test_load_netlist_file(tmp_path):
    path = tmp_path / "board.net"
    path.write_text(KICAD_FIXTURE, encoding="utf-8")

    board = load_netlist_file(str(path))

    assert board["source"] == str(path)
    assert board["mcu"]["part_normalized"] == "STM32L431CBT6"
