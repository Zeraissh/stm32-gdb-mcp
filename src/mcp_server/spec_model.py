"""Deterministic product-spec reducer (spec-to-silicon pipeline, upstream of Pillar D).

Turns a **controlled-vocabulary product spec** -- human/product terms such as a
UART ``framing`` of ``"8N1"``, an ADC ``conversion`` of ``"continuous"``, or an
I2C ``speed`` of ``"fast"`` -- into the per-peripheral ``design`` params that
``design_framework`` consumes (HAL macro values like ``UART_WORDLENGTH_8B``),
and cross-checks the spec against the imported netlist.

This is the one hand-written link the pipeline still lacked a machine guard for,
and it is the most upstream one: a mistranslation here (a wrong baud, a dropped
peripheral, an ``8N1`` that should have been ``8E1``) would propagate through
every deterministic stage below and generate precisely-wrong code. So the split
is the same as every other pillar: the agent's creative job is only "human
requirements doc -> controlled spec dict"; the mechanical, error-prone part
(product terms -> HAL params) is done here, deterministically, and anything the
machine cannot place is surfaced as ``unresolved`` / ``conflict`` -- never
guessed.

Everything is plain dicts so a result serializes straight through the
``content_success`` JSON envelope.
"""

import re

from .board_model import peripherals_in_use
from .framework_solver import classify_peripheral

# Intent keys that mean the same thing on every peripheral kind. They map onto
# the existing NVIC / DMA opt-ins that design_framework already understands, so a
# spec never has to name a HAL macro to ask for an interrupt or a DMA stream.
_COMMON_KEYS = ("dma", "dma_priority", "interrupt", "priority")

_FRAMING_RE = re.compile(r"^(\d)([NEO])([12])$")

_UART_DIRECTION = {
    "tx": "UART_MODE_TX",
    "rx": "UART_MODE_RX",
    "txrx": "UART_MODE_TX_RX",
}
_UART_FLOW = {
    "none": "UART_HWCONTROL_NONE",
    "rts": "UART_HWCONTROL_RTS",
    "cts": "UART_HWCONTROL_CTS",
    "rtscts": "UART_HWCONTROL_RTS_CTS",
}
_UART_PARITY = {"N": "UART_PARITY_NONE", "E": "UART_PARITY_EVEN", "O": "UART_PARITY_ODD"}
_UART_STOP = {"1": "UART_STOPBITS_1", "2": "UART_STOPBITS_2"}

_SPI_ROLE = {"master": "SPI_MODE_MASTER", "slave": "SPI_MODE_SLAVE"}
# Standard SPI mode -> (clock polarity, clock phase).
_SPI_MODE = {
    0: ("SPI_POLARITY_LOW", "SPI_PHASE_1EDGE"),
    1: ("SPI_POLARITY_LOW", "SPI_PHASE_2EDGE"),
    2: ("SPI_POLARITY_HIGH", "SPI_PHASE_1EDGE"),
    3: ("SPI_POLARITY_HIGH", "SPI_PHASE_2EDGE"),
}
_SPI_DATASIZE = {8: "SPI_DATASIZE_8BIT", 16: "SPI_DATASIZE_16BIT"}
_SPI_BITORDER = {"msb": "SPI_FIRSTBIT_MSB", "lsb": "SPI_FIRSTBIT_LSB"}

_I2C_SPEED_NAMED = {"standard": 100000, "fast": 400000}
_I2C_ADDRESSING = {"7bit": "I2C_ADDRESSINGMODE_7BIT", "10bit": "I2C_ADDRESSINGMODE_10BIT"}

_ADC_RESOLUTION = {12: "ADC_RESOLUTION_12B", 10: "ADC_RESOLUTION_10B",
                   8: "ADC_RESOLUTION_8B", 6: "ADC_RESOLUTION_6B"}
_ADC_CONVERSION = {"single": "DISABLE", "continuous": "ENABLE"}


def _norm(value) -> str:
    return str(value).strip().lower()


def _translate_common(config: dict) -> dict:
    """Kind-independent intent keys (interrupt / priority / dma) -> design params."""
    design: dict = {}
    unresolved: list = []
    if "dma" in config:
        design["dma"] = config["dma"]
    if "dma_priority" in config:
        design["dma_priority"] = config["dma_priority"]
    if "interrupt" in config:
        if bool(config["interrupt"]):
            design["nvic"] = True
    if "priority" in config:
        design["nvic_priority"] = config["priority"]
    return {"design": design, "unresolved": unresolved, "notes": []}


def _unknown(key, value) -> dict:
    return {"key": key, "value": value, "reason": f"unknown intent key {key!r} for this peripheral kind"}


def _bad_value(key, value, allowed) -> dict:
    return {"key": key, "value": value,
            "reason": f"{key}={value!r} not understood; expected one of {allowed}"}


def _translate_framing(value):
    """``"8N1"`` -> (design fragment, note, error). HAL counts the parity bit in
    WordLength (CubeMX does the same), so 8E1 -> WordLength_9B + PARITY_EVEN."""
    match = _FRAMING_RE.match(str(value).strip().upper())
    if not match:
        return None, None, _bad_value("framing", value, "a DPS code like '8N1'/'8E1'/'9N1'")
    data_bits = int(match.group(1))
    parity_letter = match.group(2)
    stop_digit = match.group(3)
    frame_bits = data_bits + (0 if parity_letter == "N" else 1)
    if frame_bits not in (7, 8, 9):
        return None, None, {
            "key": "framing", "value": value,
            "reason": f"framing {value!r} needs a {frame_bits}-bit word; HAL supports 7/8/9 only",
        }
    fragment = {
        "word_length": f"UART_WORDLENGTH_{frame_bits}B",
        "parity": _UART_PARITY[parity_letter],
        "stop_bits": _UART_STOP[stop_digit],
    }
    note = None
    if parity_letter != "N":
        note = (f"framing {str(value).upper()}: HAL WordLength includes the parity bit, "
                f"so it resolves to UART_WORDLENGTH_{frame_bits}B")
    return fragment, note, None


def _translate_uart(config: dict) -> dict:
    design: dict = {}
    unresolved: list = []
    notes: list = []
    for key, value in config.items():
        if key == "baud":
            design["baud"] = value
        elif key == "framing":
            fragment, note, error = _translate_framing(value)
            if error:
                unresolved.append(error)
            else:
                design.update(fragment)
                if note:
                    notes.append(note)
        elif key == "direction":
            macro = _UART_DIRECTION.get(_norm(value))
            if macro:
                design["mode"] = macro
            else:
                unresolved.append(_bad_value("direction", value, "tx|rx|txrx"))
        elif key == "flow_control":
            macro = _UART_FLOW.get(_norm(value).replace("_", "").replace("-", ""))
            if macro:
                design["flow_control"] = macro
            else:
                unresolved.append(_bad_value("flow_control", value, "none|rts|cts|rtscts"))
        else:
            unresolved.append(_unknown(key, value))
    return {"design": design, "unresolved": unresolved, "notes": notes}


def _translate_spi(config: dict) -> dict:
    design: dict = {}
    unresolved: list = []
    for key, value in config.items():
        if key == "role":
            macro = _SPI_ROLE.get(_norm(value))
            if macro:
                design["mode"] = macro
            else:
                unresolved.append(_bad_value("role", value, "master|slave"))
        elif key == "spi_mode":
            pair = _SPI_MODE.get(value if isinstance(value, int) else _coerce_int(value))
            if pair:
                design["clk_polarity"], design["clk_phase"] = pair
            else:
                unresolved.append(_bad_value("spi_mode", value, "0|1|2|3"))
        elif key == "data_size":
            macro = _SPI_DATASIZE.get(value if isinstance(value, int) else _coerce_int(value))
            if macro:
                design["data_size"] = macro
            else:
                unresolved.append(_bad_value("data_size", value, "8|16"))
        elif key == "bit_order":
            macro = _SPI_BITORDER.get(_norm(value))
            if macro:
                design["first_bit"] = macro
            else:
                unresolved.append(_bad_value("bit_order", value, "msb|lsb"))
        else:
            unresolved.append(_unknown(key, value))
    return {"design": design, "unresolved": unresolved, "notes": []}


def _translate_i2c(config: dict) -> dict:
    design: dict = {}
    unresolved: list = []
    notes: list = []
    for key, value in config.items():
        if key == "speed":
            hz = _I2C_SPEED_NAMED.get(_norm(value)) if not isinstance(value, int) else value
            if hz:
                # The concrete I2C bus timing is clock- AND family-dependent (a Timing
                # register on F0/F3/F4/L4, a ClockSpeed on F1). Record the target but never
                # fabricate the register value -- that is a design decision / a future
                # i2c-timing solver, surfaced honestly rather than rendered wrong.
                unresolved.append({
                    "key": "speed", "value": value,
                    "reason": f"I2C bus timing is clock- and family-dependent; recorded target {hz} Hz -- "
                              "fill Timing (F0/F3/F4/L4) or ClockSpeed (F1) explicitly, not guessed",
                })
            else:
                unresolved.append(_bad_value("speed", value, "standard|fast|<Hz int>"))
        elif key == "addressing":
            macro = _I2C_ADDRESSING.get(_norm(value).replace("-", "").replace("_", ""))
            if macro:
                design["addressing_mode"] = macro
            else:
                unresolved.append(_bad_value("addressing", value, "7bit|10bit"))
        elif key == "own_address":
            design["own_address"] = value
        else:
            unresolved.append(_unknown(key, value))
    return {"design": design, "unresolved": unresolved, "notes": notes}


def _translate_adc(config: dict) -> dict:
    design: dict = {}
    unresolved: list = []
    for key, value in config.items():
        if key == "resolution":
            macro = _ADC_RESOLUTION.get(value if isinstance(value, int) else _coerce_int(value))
            if macro:
                design["resolution"] = macro
            else:
                unresolved.append(_bad_value("resolution", value, "12|10|8|6"))
        elif key == "conversion":
            macro = _ADC_CONVERSION.get(_norm(value))
            if macro:
                design["continuous"] = macro
            else:
                unresolved.append(_bad_value("conversion", value, "single|continuous"))
        else:
            unresolved.append(_unknown(key, value))
    return {"design": design, "unresolved": unresolved, "notes": []}


def _translate_timer(config: dict) -> dict:
    design: dict = {}
    unresolved: list = []
    for key, value in config.items():
        if key == "update_hz":
            design["update_hz"] = value
        else:
            unresolved.append(_unknown(key, value))
    return {"design": design, "unresolved": unresolved, "notes": []}


_TRANSLATORS = {
    "uart": _translate_uart,
    "spi": _translate_spi,
    "i2c": _translate_i2c,
    "adc": _translate_adc,
    "timer": _translate_timer,
}


def _coerce_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _split_common(config: dict) -> tuple[dict, dict]:
    """Partition a peripheral spec into (kind-specific keys, common intent keys)."""
    common = {k: config[k] for k in _COMMON_KEYS if k in config}
    specific = {k: v for k, v in config.items() if k not in _COMMON_KEYS}
    return specific, common


def build_design(spec: dict | None, board: dict | None = None) -> dict:
    """Reduce a controlled-vocabulary product *spec* into design_framework params.

    Returns ``{"design", "unresolved", "conflicts", "notes", "stats"}``. When a
    *board* is given, every peripheral the spec names is cross-checked against the
    netlist: a peripheral with no pins is a ``conflict`` and is left out of
    ``design`` (so no code is generated for hardware that is not wired). Unknown
    intent keys or unrecognized values are ``unresolved`` -- surfaced, never
    guessed. The peripheral is still designed with whatever keys did resolve.
    """
    spec = dict(spec or {})
    in_use = set(peripherals_in_use(board)) if board else None

    design: dict = {}
    unresolved: list = []
    conflicts: list = []
    notes: list = []
    resolved_keys = 0

    for periph in sorted(spec):
        raw = spec[periph] or {}
        if not isinstance(raw, dict):
            conflicts.append({"peripheral": periph,
                              "reason": f"spec for {periph} must be an object, got {type(raw).__name__}"})
            continue

        if in_use is not None and periph.upper() not in {p.upper() for p in in_use}:
            conflicts.append({"peripheral": periph,
                              "reason": f"spec requires {periph} but the netlist has no {periph} pins"})
            continue

        specific, common = _split_common(raw)
        kind = classify_peripheral(periph)
        translator = _TRANSLATORS.get(kind)

        merged: dict = {}
        if translator:
            out = translator(specific)
        elif specific:
            out = {"design": {}, "notes": [],
                   "unresolved": [{"key": k, "value": v,
                                   "reason": f"{periph} (kind {kind!r}) has no spec translator yet"}
                                  for k, v in specific.items()]}
        else:
            out = {"design": {}, "unresolved": [], "notes": []}

        merged.update(out["design"])
        common_out = _translate_common(common)
        merged.update(common_out["design"])

        for item in out["unresolved"] + common_out["unresolved"]:
            unresolved.append({"peripheral": periph, **item})
        notes.extend(out["notes"])
        resolved_keys += len(merged)
        design[periph] = merged

    return {
        "design": design,
        "unresolved": unresolved,
        "conflicts": conflicts,
        "notes": notes,
        "stats": {
            "peripherals": len(design),
            "resolved_keys": resolved_keys,
            "unresolved": len(unresolved),
            "conflicts": len(conflicts),
        },
    }
