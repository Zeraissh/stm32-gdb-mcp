"""Import ST's official pin data (STM32_open_pin_data checkout or CubeMX ``db/``)
into the capability DB JSON consumed by ``board_validation.PinCapabilityDB``.

Pure module: stdlib only, no imports from the rest of the package, everything
plain dicts. Generation is an offline, git-reviewable step; the MCP server only
ever loads the produced JSON. Never fabricates an ``af`` -- entries without a
numeric alternate function are emitted without the key.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

_AF_RE = re.compile(r"^GPIO_AF(\d+)_")
_SKIP_SIGNALS = frozenset({"GPIO", "EVENTOUT"})


def _ns(root: ET.Element) -> str:
    """Namespace prefix of the document, e.g. ``{http://dummy.com}`` ('' when absent)."""
    tag = root.tag
    return tag[: tag.index("}") + 1] if tag.startswith("{") else ""


def normalize_port_pin(pin_name: str) -> str:
    """``PA0-WKUP1`` -> ``PA0``; ``PC14-OSC32_IN`` -> ``PC14``."""
    return pin_name.split("-", 1)[0]


def split_signal(signal_name: str) -> tuple[str, str] | None:
    """Split ``PERIPHERAL_SIGNAL`` at the first underscore; None when not a peripheral signal."""
    if signal_name in _SKIP_SIGNALS or "_" not in signal_name:
        return None
    peripheral, signal = signal_name.split("_", 1)
    return (peripheral, signal)


def parse_mcu_xml(text: str) -> dict:
    """Parse one ``mcu/<RefName>.xml`` into header facts + per-port-pin signal pairs."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise ValueError(f"malformed MCU XML: {e}") from e
    if not root.tag.endswith("Mcu"):
        raise ValueError(f"expected Mcu root element, got {root.tag!r}")
    ns = _ns(root)
    gpio_version = None
    for ip in root.findall(f"{ns}IP"):
        if ip.get("Name") == "GPIO":
            gpio_version = ip.get("Version")
    pins: dict[str, list[dict]] = {}
    for pin in root.findall(f"{ns}Pin"):
        if pin.get("Type") != "I/O":
            continue
        port_pin = normalize_port_pin(pin.get("Name", ""))
        if not port_pin:
            continue
        entries: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for sig in pin.findall(f"{ns}Signal"):
            pair = split_signal(sig.get("Name", ""))
            if pair is None or pair in seen:
                continue
            seen.add(pair)
            entries.append({"peripheral": pair[0], "signal": pair[1]})
        if entries:
            pins[port_pin] = entries
    return {
        "ref_name": root.get("RefName"),
        "family": root.get("Family"),
        "line": root.get("Line"),
        "package": root.get("Package"),
        "db_version": root.get("DBVersion"),
        "gpio_version": gpio_version,
        "pins": pins,
    }


def parse_gpio_modes_xml(text: str) -> dict[tuple[str, str], int]:
    """Parse ``mcu/IP/GPIO-<version>_Modes.xml`` into ``{(port_pin, PERIPHERAL_SIGNAL): af}``.

    Only ``GPIO_AF<n>_*`` values count; AFIO remap values (F1 family) and signals
    without a ``GPIO_AF`` parameter contribute nothing -- an absent entry means
    "no numeric AF known", never a guess.
    """
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise ValueError(f"malformed GPIO modes XML: {e}") from e
    ns = _ns(root)
    table: dict[tuple[str, str], int] = {}
    for gpio_pin in root.iter(f"{ns}GPIO_Pin"):
        port_pin = normalize_port_pin(gpio_pin.get("Name", ""))
        if not port_pin:
            continue
        for pin_signal in gpio_pin.findall(f"{ns}PinSignal"):
            signal_name = pin_signal.get("Name", "")
            for param in pin_signal.iter(f"{ns}SpecificParameter"):
                if param.get("Name") != "GPIO_AF":
                    continue
                for value in param.findall(f"{ns}PossibleValue"):
                    match = _AF_RE.match((value.text or "").strip())
                    if match:
                        table[(port_pin, signal_name)] = int(match.group(1))
    return table
