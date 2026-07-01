"""Normalized board model and pin-function inference.

This module turns the raw ``components`` / ``nets`` produced by a netlist parser
into a **BoardDescription** — a machine-readable BSP model that downstream
pipeline stages (clock-tree solver, pin-mux validator, init codegen, acceptance
synthesis) consume. Everything is plain dicts so it serializes directly through
the ``content_success`` JSON envelope.

BoardDescription schema::

    {
      "source": "<path|memory>",
      "format": "kicad",
      "mcu": {"ref", "part", "part_normalized", "family", "line",
              "pins": [{"package_pin", "port_pin", "net", "function"}]},
      "components": [{"ref", "value", "footprint", "pins": {pin: net}}],
      "nets": [{"name", "nodes": [{"ref", "pin", "port_pin"?}]}],
      "power_nets": {"power": [...], "ground": [...]},
      "warnings": [...],
      "stats": {"component_count", "net_count", "mcu_pin_count"},
    }
"""

import re

# --- MCU part-number normalization -----------------------------------------

_MCU_PART_RE = re.compile(r"(STM32[A-Z]\d[A-Z0-9]*)", re.IGNORECASE)
_MCU_LINE_RE = re.compile(r"(STM32[A-Z][A-Z0-9]{3})", re.IGNORECASE)
_MCU_FAMILY_RE = re.compile(r"STM32([A-Z][A-Z0-9])", re.IGNORECASE)


def normalize_mcu_part(value: str | None) -> dict | None:
    """Parse a component value like ``STM32L431CBT6`` into a part descriptor.

    Returns ``None`` when the value is not a recognizable STM32 part number.
    """
    if not value:
        return None
    text = value.strip()
    part_match = _MCU_PART_RE.search(text)
    if not part_match:
        return None
    part_normalized = part_match.group(1).upper()
    line_match = _MCU_LINE_RE.match(part_normalized)
    family_match = _MCU_FAMILY_RE.match(part_normalized)
    return {
        "part": text,
        "part_normalized": part_normalized,
        "family": ("STM32" + family_match.group(1).upper()) if family_match else None,
        "line": line_match.group(1).upper() if line_match else None,
    }


def is_mcu_value(value: str | None) -> bool:
    """Return True when a component value looks like a supported MCU (STM32 today)."""
    return normalize_mcu_part(value) is not None


# --- Pin-function inference from net names ----------------------------------

# Single-token system pins: net label -> (peripheral, signal).
SYSTEM_PIN_FUNCTIONS = {
    "SWDIO": ("SWD", "SWDIO"),
    "SWCLK": ("SWD", "SWCLK"),
    "SWO": ("SWD", "SWO"),
    "JTMS": ("JTAG", "JTMS"),
    "JTCK": ("JTAG", "JTCK"),
    "JTDI": ("JTAG", "JTDI"),
    "JTDO": ("JTAG", "JTDO"),
    "NJTRST": ("JTAG", "NJTRST"),
    "NRST": ("SYS", "NRST"),
    "RESET": ("SYS", "NRST"),
    "BOOT0": ("SYS", "BOOT0"),
    "BOOT1": ("SYS", "BOOT1"),
    "OSC_IN": ("RCC", "OSC_IN"),
    "OSC_OUT": ("RCC", "OSC_OUT"),
    "OSC32_IN": ("RCC", "OSC32_IN"),
    "OSC32_OUT": ("RCC", "OSC32_OUT"),
    "HSE_IN": ("RCC", "OSC_IN"),
    "HSE_OUT": ("RCC", "OSC_OUT"),
    "MCO": ("RCC", "MCO"),
}

# Peripheral bus patterns: each captures ``peripheral`` and ``signal`` groups.
_PIN_FUNCTION_REGEXES = [
    re.compile(r"^(?P<peripheral>US?ART\d+)_?(?P<signal>TX|RX|CTS|RTS|CK|DE)$"),
    re.compile(r"^(?P<peripheral>LPUART\d+)_?(?P<signal>TX|RX|CTS|RTS)$"),
    re.compile(r"^(?P<peripheral>I2C\d+)_?(?P<signal>SCL|SDA|SMBA)$"),
    re.compile(r"^(?P<peripheral>SPI\d+)_?(?P<signal>SCK|SCLK|MISO|MOSI|NSS)$"),
    re.compile(r"^(?P<peripheral>FDCAN\d+|CAN\d*)_?(?P<signal>TX|RX)$"),
    re.compile(r"^(?P<peripheral>USB|USB_OTG_FS|USB_OTG_HS|OTG_FS|OTG_HS)_?(?P<signal>DP|DM|ID|VBUS|SOF)$"),
    re.compile(r"^(?P<peripheral>TIM\d+)_?(?P<signal>CH\d+N?|ETR|BKIN\d?)$"),
    re.compile(r"^(?P<peripheral>ADC\d*)_?(?P<signal>IN\d+)$"),
    re.compile(r"^(?P<peripheral>DAC\d*)_?(?P<signal>OUT\d)$"),
    re.compile(r"^(?P<peripheral>SDMMC\d+|SDIO)_?(?P<signal>CK|CMD|D\d)$"),
    re.compile(r"^(?P<peripheral>QUADSPI|QSPI|OCTOSPI\d*|OSPI\d*)_?(?P<signal>CLK|NCS|IO\d)$"),
]


def _net_basename(net_name: str) -> str:
    """Strip a KiCad hierarchical prefix (``/sheet/NAME``) down to the label."""
    return net_name.strip().lstrip("~").split("/")[-1].strip()


def _match_function(label: str) -> dict | None:
    if label in SYSTEM_PIN_FUNCTIONS:
        peripheral, signal = SYSTEM_PIN_FUNCTIONS[label]
        return {"peripheral": peripheral, "signal": signal}
    for pattern in _PIN_FUNCTION_REGEXES:
        match = pattern.match(label)
        if match:
            return {"peripheral": match.group("peripheral"), "signal": match.group("signal")}
    return None


def infer_pin_function(net_name: str | None) -> dict | None:
    """Infer ``{peripheral, signal}`` from a net label, or ``None`` if unknown.

    Tolerates hierarchical labels (``/sheet/USART1_TX``) and a leading prefix
    (``MCU_I2C1_SDA``) by retrying on the last two underscore-joined tokens.
    """
    if not net_name:
        return None
    label = _net_basename(net_name).upper().replace("-", "_")
    result = _match_function(label)
    if result:
        return result
    if "_" in label:
        tail = "_".join(label.split("_")[-2:])
        if tail != label:
            return _match_function(tail)
    return None


# --- Power-net classification -----------------------------------------------

_GROUND_RE = re.compile(r"^[+-]?(GND|VSS|VSSA|AGND|DGND|PGND|GNDA|EGND)\d*$")
_POWER_RE = re.compile(r"^[+-]?(VDDA|VDD|VBAT|VCCA|VCC|VREF\+|VREFP|VREF|VBUS|VIN|VSYS|PWR)\w*$")
_VOLTAGE_RE = re.compile(r"^[+-]?\d+V\d*$")
_VOLTAGE_DOT_RE = re.compile(r"^[+-]?\d+\.\d+V$")


def classify_power_net(net_name: str | None) -> str | None:
    """Classify a net as ``"power"``, ``"ground"``, or ``None``."""
    if not net_name:
        return None
    label = _net_basename(net_name).upper().replace(" ", "")
    if _GROUND_RE.match(label):
        return "ground"
    if _POWER_RE.match(label) or _VOLTAGE_RE.match(label) or _VOLTAGE_DOT_RE.match(label):
        return "power"
    return None


# --- BoardDescription assembly ----------------------------------------------


def build_board_description(
    components: list[dict],
    nets: list[dict],
    source: str = "<memory>",
    fmt: str = "unknown",
    warnings: list[str] | None = None,
) -> dict:
    """Assemble a normalized BoardDescription from parsed components and nets."""
    warnings = list(warnings or [])

    candidates = [c for c in components if is_mcu_value(c.get("value"))]
    mcu = None
    if not candidates:
        warnings.append("No STM32 MCU detected among components; pin map will be empty.")
    else:
        if len(candidates) > 1:
            refs = ", ".join(c.get("ref", "?") for c in candidates)
            warnings.append(f"Multiple MCU candidates ({refs}); using {candidates[0].get('ref')}.")
        mcu = _build_mcu(candidates[0], nets)

    power = {"power": [], "ground": []}
    for net in nets:
        kind = classify_power_net(net.get("name"))
        if kind:
            power[kind].append(net.get("name"))

    return {
        "source": source,
        "format": fmt,
        "mcu": mcu,
        "components": components,
        "nets": nets,
        "power_nets": power,
        "warnings": warnings,
        "stats": {
            "component_count": len(components),
            "net_count": len(nets),
            "mcu_pin_count": len(mcu["pins"]) if mcu else 0,
        },
    }


def _build_mcu(component: dict, nets: list[dict]) -> dict:
    ref = component.get("ref")
    part = normalize_mcu_part(component.get("value")) or {}
    pins = []
    for net in nets:
        name = net.get("name")
        for node in net.get("nodes", []):
            if node.get("ref") != ref:
                continue
            pins.append(
                {
                    "package_pin": node.get("pin"),
                    "port_pin": node.get("port_pin"),
                    "net": name,
                    "function": infer_pin_function(name),
                }
            )
    pins.sort(key=lambda p: _pin_sort_key(p.get("package_pin")))
    return {
        "ref": ref,
        "part": part.get("part"),
        "part_normalized": part.get("part_normalized"),
        "family": part.get("family"),
        "line": part.get("line"),
        "pins": pins,
    }


def _pin_sort_key(pin: str | None) -> tuple:
    """Sort package pins numerically when possible, else lexicographically."""
    if pin is None:
        return (2, "")
    if pin.isdigit():
        return (0, int(pin))
    return (1, pin)
