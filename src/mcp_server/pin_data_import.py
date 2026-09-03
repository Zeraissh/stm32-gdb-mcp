"""Import ST's official pin data (STM32_open_pin_data checkout or CubeMX ``db/``)
into the capability DB JSON consumed by ``board_validation.PinCapabilityDB``.

Pure module: stdlib only, no imports from the rest of the package, everything
plain dicts. Generation is an offline, git-reviewable step; the MCP server only
ever loads the produced JSON. Never fabricates an ``af`` -- entries without a
numeric alternate function are emitted without the key.

DB keys are concrete lines derived from each MCU's ``RefName`` (matching
``board_model.normalize_mcu_part``, e.g. ``STM32L431C(B-C)Tx`` ->
``STM32L431``), falling back to the XML ``Line`` attribute (which may carry
ST's lowercase-x wildcards, e.g. ``STM32L4x1``), then to the family.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

_AF_RE = re.compile(r"^GPIO_AF(\d+)_")
_SKIP_SIGNALS = frozenset({"GPIO", "EVENTOUT"})
# Same semantics as board_model._MCU_LINE_RE: concrete line from a part number.
_REF_LINE_RE = re.compile(r"STM32[A-Z][A-Z0-9]{3}", re.IGNORECASE)


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
    ref_name = root.get("RefName")
    key_line_match = _REF_LINE_RE.match(ref_name) if ref_name else None
    return {
        "ref_name": ref_name,
        "family": root.get("Family"),
        "line": root.get("Line"),
        "key_line": key_line_match.group(0).upper() if key_line_match else None,
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


GENERATED_BY = "stm32-gdb-mcp scripts/import_pin_data.py"


def build_db(mcus: list[dict], modes_by_version: dict[str, dict], source: str) -> dict:
    """Merge parsed MCUs into one capability DB keyed by concrete line, with provenance.

    The key is the concrete line derived from ``RefName`` (``key_line``,
    matching ``board_model.normalize_mcu_part``), falling back to the XML
    ``Line`` attribute, then the family. ``modes_by_version`` maps a GPIO IP
    version string (from each MCU's ``gpio_version``) to the table returned by
    :func:`parse_gpio_modes_xml`. A missing table degrades honestly: entries
    keep no ``af`` and a warning is recorded in ``_meta``. Duplicate
    (peripheral, signal) entries across RefNames of one line are merged; a
    later entry may contribute a missing ``af``.
    """
    db: dict = {
        "_meta": {
            "source": source,
            "generated_by": GENERATED_BY,
            "db_version": None,
            "ref_names": [],
            "warnings": [],
        }
    }
    for mcu in mcus:
        scope = mcu["key_line"] or mcu["line"] or mcu["family"]
        if not scope:
            raise ValueError(f"{mcu['ref_name']}: no key_line, line, or family to key the DB by")
        if db["_meta"]["db_version"] is None:
            db["_meta"]["db_version"] = mcu["db_version"]
        if mcu["ref_name"]:
            db["_meta"]["ref_names"].append(mcu["ref_name"])
        modes = modes_by_version.get(mcu["gpio_version"])
        if mcu["gpio_version"] and modes is None:
            db["_meta"]["warnings"].append(f"no GPIO modes file for {mcu['gpio_version']} ({mcu['ref_name']})")
        table = db.setdefault(scope, {})
        for port_pin, entries in mcu["pins"].items():
            bucket = table.setdefault(port_pin, [])
            by_pair = {(e["peripheral"], e["signal"]): e for e in bucket}
            for entry in entries:
                pair = (entry["peripheral"], entry["signal"])
                signal_name = f"{pair[0]}_{pair[1]}"
                af = modes.get((port_pin, signal_name)) if modes else None
                existing = by_pair.get(pair)
                if existing is not None:
                    if af is not None and "af" not in existing:
                        existing["af"] = af
                    continue
                merged = dict(entry)
                if af is not None:
                    merged["af"] = af
                bucket.append(merged)
                by_pair[pair] = merged
    db["_meta"]["ref_names"].sort()
    return db
