"""Deterministic framework / init-code plan synthesis (design synthesis, Pillar D).

Turns a **BoardDescription** (Pillar A) plus an optional per-peripheral design
config into a machine-readable **FrameworkPlan**: which clocks to enable, how each
pin must be configured, and which peripheral init blocks to emit — all in
dependency order. This is the "framework design + code writing" stage that feeds
the bounded acceptance loop (Pillar C).

Everything derivable from the board model alone is 100% deterministic. Mandatory
HAL ``.Init`` members are filled with the standard defaults CubeMX itself emits
(so the generated init struct is complete and valid, never a half-initialized
struct), and a few values are derived straight from the netlist (UART hardware
flow control from the RTS/CTS pins, SPI NSS management from a hardware NSS pin).
Every field is tagged ``explicit`` / ``derived`` / ``default`` so nothing looks
hand-tuned. Values that genuinely need a human design decision (a baud rate, a
timer period, an I2C bus timing) are **never invented**: they surface in
``unresolved`` and, when rendered, become clearly marked ``TODO`` holes. A senior
engineer gets correct scaffolding, not a plausible-looking guess.

Everything is plain dicts so a plan serializes straight through the
``content_success`` JSON envelope.
"""

import re

from .dma_solver import DMA_KEYS, build_dma
from .interrupt_solver import NVIC_KEYS, build_nvic

# --- Peripheral classification ----------------------------------------------

_UART_RE = re.compile(r"^(LP)?US?ART\d+$")


def classify_peripheral(name: str | None) -> str:
    """Map a peripheral name (``USART1``/``I2C1``/``TIM2``...) to a driver kind."""
    if not name:
        return "other"
    u = name.upper()
    if _UART_RE.match(u):
        return "uart"
    if u.startswith("I2C"):
        return "i2c"
    if u.startswith("SPI"):
        return "spi"
    if u.startswith("TIM"):
        return "timer"
    if u.startswith("ADC"):
        return "adc"
    if u.startswith("DAC"):
        return "dac"
    if u.startswith(("FDCAN", "CAN")):
        return "can"
    if u.startswith(("USB", "OTG")):
        return "usb"
    if u.startswith(("SDMMC", "SDIO")):
        return "sdmmc"
    if u.startswith(("QUADSPI", "QSPI", "OCTOSPI", "OSPI")):
        return "qspi"
    if u in ("SWD", "JTAG"):
        return "debug"
    if u == "SYS":
        return "system"
    if u == "RCC":
        return "clock"
    return "other"


# Kinds that the MCU pre-owns; we never enable their clock or reconfigure their pins.
_INFRA_KINDS = frozenset({"debug", "system", "clock"})


# --- GPIO role inference ----------------------------------------------------

# HAL config for each abstract GPIO role. ``speed=None`` omits the Speed line
# (analog pins take no speed).
_ROLE_HAL: dict[str, dict] = {
    "af_pp": {"mode": "GPIO_MODE_AF_PP", "pull": "GPIO_NOPULL", "speed": "GPIO_SPEED_FREQ_HIGH"},
    "af_od": {"mode": "GPIO_MODE_AF_OD", "pull": "GPIO_NOPULL", "speed": "GPIO_SPEED_FREQ_HIGH"},
    "analog": {"mode": "GPIO_MODE_ANALOG", "pull": "GPIO_NOPULL", "speed": None},
}

# Roles that ride a peripheral alternate function (need an AF number to be complete).
_AF_ROLES = frozenset({"af_pp", "af_od"})


def gpio_role(kind: str, signal: str | None) -> str:
    """Return the abstract GPIO role for a (kind, signal), or ``"skip"``/``"unknown"``.

    ``skip`` = debug/reset/oscillator pins we must not reconfigure. ``unknown`` =
    a recognized peripheral whose electrical role we can't infer deterministically.
    """
    if kind in _INFRA_KINDS:
        return "skip"
    if kind in ("adc", "dac"):
        return "analog"
    if kind == "i2c":
        return "af_od"
    if kind in ("uart", "spi", "timer", "can", "sdmmc", "qspi", "usb"):
        return "af_pp"
    return "unknown"


# --- Port-pin parsing -------------------------------------------------------

_PORT_PIN_RE = re.compile(r"^P([A-K])(\d{1,2})$")


def parse_port_pin(port_pin: str | None) -> dict | None:
    """Parse ``"PA9"`` into ``{"port": "A", "pin": 9}``; ``None`` when not a GPIO."""
    if not port_pin:
        return None
    match = _PORT_PIN_RE.match(port_pin.strip().upper())
    if not match:
        return None
    pin = int(match.group(2))
    if pin > 15:
        return None
    return {"port": match.group(1), "pin": pin}


# --- HAL clock-macro derivation ---------------------------------------------


def gpio_clock_macro(port: str) -> str:
    return f"__HAL_RCC_GPIO{port.upper()}_CLK_ENABLE"


def peripheral_clock_macro(name: str) -> str:
    return f"__HAL_RCC_{name.upper()}_CLK_ENABLE"


# --- Per-kind driver metadata ----------------------------------------------

# handle_prefix + trailing peripheral index -> handle (USART1 -> huart1).
# field map turns a design-config key into a HAL ``.Init`` field name.
_KIND_META = {
    "uart": {
        "hal_type": "UART_HandleTypeDef", "handle_prefix": "huart", "init_suffix": "UART_Init",
        "hal_init_call": "HAL_UART_Init",
        "fields": {"baud": "BaudRate", "word_length": "WordLength", "stop_bits": "StopBits",
                   "parity": "Parity", "mode": "Mode", "flow_control": "HwFlowCtl",
                   "oversampling": "OverSampling"},
    },
    "spi": {
        "hal_type": "SPI_HandleTypeDef", "handle_prefix": "hspi", "init_suffix": "Init",
        "hal_init_call": "HAL_SPI_Init",
        "fields": {"mode": "Mode", "direction": "Direction", "data_size": "DataSize",
                   "clk_polarity": "CLKPolarity", "clk_phase": "CLKPhase", "nss": "NSS",
                   "baud_prescaler": "BaudRatePrescaler", "first_bit": "FirstBit",
                   "ti_mode": "TIMode", "crc": "CRCCalculation"},
    },
    "i2c": {
        "hal_type": "I2C_HandleTypeDef", "handle_prefix": "hi2c", "init_suffix": "Init",
        "hal_init_call": "HAL_I2C_Init",
        "fields": {"clock_speed": "ClockSpeed", "timing": "Timing", "duty_cycle": "DutyCycle",
                   "own_address": "OwnAddress1", "addressing_mode": "AddressingMode",
                   "dual_address": "DualAddressMode", "general_call": "GeneralCallMode",
                   "no_stretch": "NoStretchMode"},
    },
    "timer": {
        "hal_type": "TIM_HandleTypeDef", "handle_prefix": "htim", "init_suffix": "Init",
        "hal_init_call": "HAL_TIM_Base_Init",
        "fields": {"prescaler": "Prescaler", "counter_mode": "CounterMode", "period": "Period",
                   "clock_division": "ClockDivision", "autoreload_preload": "AutoReloadPreload"},
    },
    "adc": {
        "hal_type": "ADC_HandleTypeDef", "handle_prefix": "hadc", "init_suffix": "Init",
        "hal_init_call": "HAL_ADC_Init",
        "fields": {"resolution": "Resolution", "scan_mode": "ScanConvMode",
                   "continuous": "ContinuousConvMode", "data_align": "DataAlign",
                   "ext_trig": "ExternalTrigConv"},
    },
}

# Kinds with no ``.Init`` field map yet: still get a handle + honest pass-through.
_GENERIC_META: dict[str, dict] = {
    "dac": {"hal_type": "DAC_HandleTypeDef", "handle_prefix": "hdac", "hal_init_call": "HAL_DAC_Init"},
    "can": {"hal_type": "CAN_HandleTypeDef", "handle_prefix": "hcan", "hal_init_call": "HAL_CAN_Init"},
    "usb": {"hal_type": "PCD_HandleTypeDef", "handle_prefix": "hpcd", "hal_init_call": "HAL_PCD_Init"},
    "sdmmc": {"hal_type": "SD_HandleTypeDef", "handle_prefix": "hsd", "hal_init_call": "HAL_SD_Init"},
    "qspi": {"hal_type": "QSPI_HandleTypeDef", "handle_prefix": "hqspi", "hal_init_call": "HAL_QSPI_Init"},
    "other": {"hal_type": "void *", "handle_prefix": "h", "hal_init_call": None},
}

_TRAILING_INDEX_RE = re.compile(r"(\d+)$")


def _peripheral_index(name: str) -> str:
    match = _TRAILING_INDEX_RE.search(name)
    return match.group(1) if match else ""


def _kind_meta(kind: str) -> dict:
    if kind in _KIND_META:
        return _KIND_META[kind]
    generic = _GENERIC_META.get(kind, _GENERIC_META["other"])
    return {"hal_type": generic["hal_type"], "handle_prefix": generic["handle_prefix"],
            "init_suffix": "Init", "hal_init_call": generic["hal_init_call"], "fields": {}}


# --- Standard .Init parameters (defaults + derivations + required decisions) --

# For each kind: the canonical HAL ``.Init`` field order, the HAL-standard default
# for every mandatory field (the values CubeMX emits for a fresh peripheral), and
# the fields that are genuine *design decisions* with no safe universal default.
# Filling defaults makes the generated init struct complete and valid instead of
# leaving members uninitialized; every defaulted field is tagged ``default`` so it
# never looks hand-tuned. Required fields, when the engineer does not supply them,
# stay honest ``TODO`` holes — never guessed.
_KIND_PARAMS: dict[str, dict] = {
    "uart": {
        "order": ["BaudRate", "WordLength", "StopBits", "Parity", "Mode", "HwFlowCtl", "OverSampling"],
        "defaults": {
            "WordLength": "UART_WORDLENGTH_8B",
            "StopBits": "UART_STOPBITS_1",
            "Parity": "UART_PARITY_NONE",
            "Mode": "UART_MODE_TX_RX",
            "OverSampling": "UART_OVERSAMPLING_16",
        },
        "required": [{"field": "BaudRate", "keys": ["baud"], "hint": "bit/s, e.g. 115200"}],
    },
    "spi": {
        "order": ["Mode", "Direction", "DataSize", "CLKPolarity", "CLKPhase", "NSS",
                  "BaudRatePrescaler", "FirstBit", "TIMode", "CRCCalculation", "CRCPolynomial"],
        "defaults": {
            "Mode": "SPI_MODE_MASTER",
            "Direction": "SPI_DIRECTION_2LINES",
            "DataSize": "SPI_DATASIZE_8BIT",
            "CLKPolarity": "SPI_POLARITY_LOW",
            "CLKPhase": "SPI_PHASE_1EDGE",
            "BaudRatePrescaler": "SPI_BAUDRATEPRESCALER_16",
            "FirstBit": "SPI_FIRSTBIT_MSB",
            "TIMode": "SPI_TIMODE_DISABLE",
            "CRCCalculation": "SPI_CRCCALCULATION_DISABLE",
            "CRCPolynomial": "10",
        },
        "required": [],
    },
    "i2c": {
        "order": ["Timing", "ClockSpeed", "DutyCycle", "OwnAddress1", "AddressingMode",
                  "DualAddressMode", "OwnAddress2", "GeneralCallMode", "NoStretchMode"],
        "defaults": {
            "OwnAddress1": "0",
            "AddressingMode": "I2C_ADDRESSINGMODE_7BIT",
            "DualAddressMode": "I2C_DUALADDRESS_DISABLE",
            "OwnAddress2": "0",
            "GeneralCallMode": "I2C_GENERALCALL_MODE_DISABLE",
            "NoStretchMode": "I2C_NOSTRETCH_DISABLE",
        },
        "required": [{"field": "Timing/ClockSpeed", "keys": ["timing", "clock_speed"],
                      "hint": "I2C_Timing (v2 peripheral) or ClockSpeed+DutyCycle (v1) -- "
                              "depends on your I2C version and kernel clock"}],
    },
    "timer": {
        "order": ["Prescaler", "CounterMode", "Period", "ClockDivision", "AutoReloadPreload"],
        "defaults": {
            "CounterMode": "TIM_COUNTERMODE_UP",
            "ClockDivision": "TIM_CLOCKDIVISION_DIV1",
            "AutoReloadPreload": "TIM_AUTORELOADPRELOAD_DISABLE",
        },
        "required": [
            {"field": "Prescaler", "keys": ["prescaler"], "hint": "APBx timer clock / desired tick rate - 1"},
            {"field": "Period", "keys": ["period"], "hint": "counter ticks per update event - 1 (ARR)"},
        ],
    },
}


def _derive_uart_params(pins: list[dict]) -> dict:
    """Derive UART hardware flow control from the RTS/CTS pins on the netlist."""
    signals = {(p.get("signal") or "").upper() for p in pins}
    rts, cts = "RTS" in signals, "CTS" in signals
    if rts and cts:
        flow, note = "UART_HWCONTROL_RTS_CTS", "RTS and CTS pins present on the netlist"
    elif rts:
        flow, note = "UART_HWCONTROL_RTS", "only an RTS pin present on the netlist"
    elif cts:
        flow, note = "UART_HWCONTROL_CTS", "only a CTS pin present on the netlist"
    else:
        flow, note = "UART_HWCONTROL_NONE", "no RTS/CTS pins on the netlist"
    return {"HwFlowCtl": {"value": flow, "note": f"flow control: {note}"}}


def _derive_spi_params(pins: list[dict]) -> dict:
    """Derive SPI NSS management from the presence of a hardware NSS/CS pin."""
    signals = {(p.get("signal") or "").upper() for p in pins}
    if "NSS" in signals or "CS" in signals:
        return {"NSS": {"value": "SPI_NSS_HARD_OUTPUT", "note": "hardware NSS pin present (master-mode assumption)"}}
    return {"NSS": {"value": "SPI_NSS_SOFT", "note": "no NSS pin on the netlist"}}


_DERIVERS = {"uart": _derive_uart_params, "spi": _derive_spi_params}

# Design keys that express a timer's desired update/overflow frequency. Recorded as
# intent on the block so solve_timer can turn it into concrete PSC/ARR once the clock
# tree is solved (Prescaler/Period need TIMxCLK, unknown at build time).
_TIMER_TARGET_KEYS = ("update_hz", "frequency_hz", "freq_hz", "target_hz")


def _count_sources(config_fields: list[dict]) -> dict:
    counts = {"explicit": 0, "derived": 0, "default": 0}
    for field in config_fields:
        source = field.get("source")
        if source in counts:
            counts[source] += 1
    return counts


# --- Alternate-function lookup ----------------------------------------------


def _lookup_af(af_map, line, family, port_pin, peripheral, signal):
    """Resolve the AF number for ``peripheral_signal`` on ``port_pin``, or ``None``.

    ``af_map`` shape: ``{line_or_family: {port_pin: {"USART1_TX": 7, ...}}}``. A
    missing entry yields ``None`` — never a fabricated number.
    """
    if not af_map:
        return None
    table = af_map.get(line) if line in (af_map or {}) else None
    if table is None:
        table = af_map.get(family) if family else None
    if not isinstance(table, dict):
        return None
    entry = table.get(port_pin)
    if not isinstance(entry, dict):
        return None
    value = entry.get(f"{peripheral}_{signal}")
    return value if isinstance(value, int) else None


def merge_af_maps(base: dict | None, override: dict | None) -> dict:
    """Deep-merge two ``af_map`` dicts; ``override`` wins per line/port_pin/signal.

    Used to layer an explicitly supplied ``af_map`` on top of one derived from a
    pin-capability DB, so a caller can correct or extend the DB without restating
    every pin. Inputs are never mutated.
    """
    if not base:
        return {k: {pp: dict(sig) for pp, sig in pins.items()}
                for k, pins in (override or {}).items() if isinstance(pins, dict)}
    merged: dict = {k: {pp: dict(sig) for pp, sig in pins.items()}
                    for k, pins in base.items() if isinstance(pins, dict)}
    for scope, pins in (override or {}).items():
        if not isinstance(pins, dict):
            continue
        dst_pins = merged.setdefault(scope, {})
        for port_pin, sig in pins.items():
            if isinstance(sig, dict):
                dst_pins.setdefault(port_pin, {}).update(sig)
    return merged


# --- Value rendering for config fields --------------------------------------


def _render_value(value) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    return str(value)


# --- FrameworkPlan assembly -------------------------------------------------


def build_framework_plan(board: dict, design: dict | None = None, af_map: dict | None = None) -> dict:
    """Derive a deterministic FrameworkPlan from a BoardDescription.

    ``design`` maps a peripheral name to its HAL ``.Init`` parameters, e.g.
    ``{"USART1": {"baud": 115200, "word_length": "UART_WORDLENGTH_8B"}}``.
    ``af_map`` optionally supplies alternate-function numbers (see ``_lookup_af``).
    """
    design = design or {}
    warnings: list[str] = [
        "Generated for STM32 HAL; confirm clock/AF macro names against your target's HAL headers."
    ]
    unresolved: list[dict] = []

    mcu = board.get("mcu")
    if not mcu:
        warnings.append("No MCU in the board description; import a netlist with an MCU first.")
        return _empty_plan(warnings)

    line, family = mcu.get("line"), mcu.get("family")

    ports: dict[str, None] = {}
    gpio: list[dict] = []
    peripherals: dict[str, dict] = {}

    for pin in mcu.get("pins", []):
        function = pin.get("function")
        if not function:
            continue
        peripheral = function.get("peripheral")
        signal = function.get("signal")
        kind = classify_peripheral(peripheral)
        if kind in _INFRA_KINDS:
            continue

        peripherals.setdefault(peripheral, {"kind": kind, "pins": []})
        peripherals[peripheral]["pins"].append({"port_pin": pin.get("port_pin"), "signal": signal})

        role = gpio_role(kind, signal)
        if role == "skip":
            continue
        parsed = parse_port_pin(pin.get("port_pin"))
        if parsed is None:
            unresolved.append({"type": "port_pin_unknown", "peripheral": peripheral,
                               "signal": signal, "port_pin": pin.get("port_pin"),
                               "detail": f"{peripheral}_{signal}: cannot map {pin.get('port_pin')!r} to a GPIO port/pin."})
            continue
        if role == "unknown":
            unresolved.append({"type": "unknown_role", "peripheral": peripheral, "signal": signal,
                               "port_pin": pin.get("port_pin"),
                               "detail": f"{peripheral}_{signal}: GPIO electrical role could not be inferred."})
            continue

        ports.setdefault(parsed["port"], None)
        hal = _ROLE_HAL[role]
        af = _lookup_af(af_map, line, family, pin.get("port_pin"), peripheral, signal) if role in _AF_ROLES else None
        hal_alternate = f"GPIO_AF{af}_{peripheral}" if af is not None else None
        if role in _AF_ROLES and af is None:
            unresolved.append({"type": "af_unknown", "peripheral": peripheral, "signal": signal,
                               "port_pin": pin.get("port_pin"),
                               "detail": f"{peripheral}_{signal} on {pin.get('port_pin')}: alternate-function number unknown (supply af_map)."})
        gpio.append({
            "port_pin": pin.get("port_pin"), "port": parsed["port"], "pin": parsed["pin"],
            "peripheral": peripheral, "signal": signal, "role": role,
            "hal_mode": hal["mode"], "pull": hal["pull"], "speed": hal["speed"],
            "af": af, "hal_alternate": hal_alternate, "net": pin.get("net"),
        })

    gpio.sort(key=lambda g: (g["port"], g["pin"]))

    clocks = [{"kind": "gpio_port", "port": p, "hal_macro": gpio_clock_macro(p)} for p in sorted(ports)]
    clocks += [{"kind": "peripheral", "peripheral": name, "hal_macro": peripheral_clock_macro(name)}
               for name in sorted(peripherals)]

    peripheral_blocks = []
    for name in sorted(peripherals):
        block = _build_peripheral_block(name, peripherals[name], design.get(name), family)
        peripheral_blocks.append(block)
        for todo in block["param_todos"]:
            unresolved.append({"type": "param_unresolved", "peripheral": name, "field": todo["field"],
                               "detail": f"{name}.Init.{todo['field']}: {todo['hint']} "
                                         f"(supply via design[{name!r}])."})
        if not block["param_todos"] and not block["config_fields"]:
            unresolved.append({"type": "no_config", "peripheral": name,
                               "detail": f"{name}: no design config supplied; init parameters left as TODO."})
        nvic = block.get("nvic")
        if nvic and nvic.get("requested") and not nvic.get("resolved"):
            unresolved.append({"type": "nvic_unresolved", "peripheral": name,
                               "detail": f"{name}: interrupt requested but vector unknown -- {nvic['unresolved_reason']}"})
        dma = block.get("dma")
        if dma and dma.get("requested"):
            for miss in dma.get("unresolved", []):
                unresolved.append({"type": "dma_unresolved", "peripheral": name, "direction": miss["direction"],
                                   "detail": f"{name} {miss['direction']}: DMA requested but stream unknown -- {miss['reason']}"})

    # A DMA stream is single-owner hardware: two peripherals on one Instance is
    # impossible, so surface the collision honestly instead of emitting both inits.
    seen_streams: dict[str, str] = {}
    for block in peripheral_blocks:
        dma = block.get("dma")
        if not dma:
            continue
        for stream in dma.get("streams", []):
            instance = stream["instance"]
            owner = f"{block['name']} {stream['direction']}"
            if instance in seen_streams:
                stream["conflict"] = True
                unresolved.append({"type": "dma_conflict", "peripheral": block["name"],
                                   "direction": stream["direction"], "instance": instance,
                                   "detail": f"{owner}: DMA {instance} already used by {seen_streams[instance]}; "
                                             "resolve the collision (change one peripheral's DMA or free the stream)."})
            else:
                seen_streams[instance] = owner

    init_order = ["SystemClock_Config", "MX_GPIO_Init"] + [b["init_fn"] for b in peripheral_blocks]

    return {
        "mcu": {k: mcu.get(k) for k in ("part_normalized", "family", "line")},
        "clocks": clocks,
        "gpio": gpio,
        "peripherals": peripheral_blocks,
        "init_order": init_order,
        "unresolved": unresolved,
        "warnings": warnings,
        "stats": {
            "clock_count": len(clocks),
            "gpio_count": len(gpio),
            "peripheral_count": len(peripheral_blocks),
            "unresolved_count": len(unresolved),
        },
    }


def _build_peripheral_block(name: str, info: dict, config: dict | None, family: str | None = None) -> dict:
    kind = info["kind"]
    meta = _kind_meta(kind)
    handle = f"{meta['handle_prefix']}{_peripheral_index(name).lower() or name.lower()}"
    config = dict(config or {})
    fields_map = meta.get("fields", {})
    params = _KIND_PARAMS.get(kind, {})
    order = params.get("order", [])
    defaults = params.get("defaults", {})
    required = params.get("required", [])

    # Interrupt generation is opt-in: pop the NVIC directives before the remaining
    # config is mapped to HAL .Init fields, then resolve the concrete vector(s).
    nvic_args = {key: config.pop(key) for key in NVIC_KEYS if key in config}
    nvic = build_nvic(name, kind, family, nvic=nvic_args.get("nvic"),
                      nvic_priority=nvic_args.get("nvic_priority"), irqn=nvic_args.get("irqn"))

    # DMA association is opt-in too: pop its directives before the .Init mapping and
    # resolve the concrete stream(s) + the derived stream interrupt vector(s).
    dma_args = {key: config.pop(key) for key in DMA_KEYS if key in config}
    dma = build_dma(name, kind, family, dma=dma_args.get("dma"),
                    dma_priority=dma_args.get("dma_priority"))

    # A timer's target frequency is intent, not a HAL field: record it and keep it out
    # of the unmapped pass-through so solve_timer can resolve PSC/ARR post clock-solve.
    timer_target = None
    if kind == "timer":
        for key in _TIMER_TARGET_KEYS:
            if key in config:
                timer_target = config.pop(key)
                break

    # Explicit engineer values: map each design key to its HAL .Init field, or keep
    # it as an unmapped pass-through comment when there is no known mapping.
    explicit: dict = {}
    unmapped: list[dict] = []
    for key, value in config.items():
        hal_field = fields_map.get(key)
        if hal_field:
            explicit[hal_field] = {"value": value, "source_key": key}
        else:
            unmapped.append({"key": key, "value": value, "rendered": _render_value(value)})

    deriver = _DERIVERS.get(kind)
    derived = deriver(info["pins"]) if deriver else {}

    # Required design decisions the engineer must still make (no safe default). When a
    # timer target is recorded, point the Prescaler/Period TODOs at solve_timer.
    param_todos = []
    for req in required:
        if any(k in config for k in req["keys"]):
            continue
        hint = req["hint"]
        if timer_target is not None and req["field"] in ("Prescaler", "Period"):
            hint = (f"target {timer_target} Hz recorded; run solve_clock_tree then "
                    "solve_timer to fill this from TIMxCLK")
        param_todos.append({"field": req["field"], "hint": hint})

    # Assemble ordered, deduped fields with precedence explicit > derived > default.
    config_fields: list[dict] = []
    seen: set = set()

    def _emit(hal_field, value, source, source_key=None, note=None):
        config_fields.append({
            "field": hal_field, "value": value, "rendered": _render_value(value),
            "source": source, "source_key": source_key, "mapped": True, "note": note,
        })
        seen.add(hal_field)

    for hal_field in list(order) + [f for f in explicit if f not in order]:
        if hal_field in seen:
            continue
        if hal_field in explicit:
            _emit(hal_field, explicit[hal_field]["value"], "explicit",
                  source_key=explicit[hal_field]["source_key"])
        elif hal_field in derived:
            _emit(hal_field, derived[hal_field]["value"], "derived", note=derived[hal_field]["note"])
        elif hal_field in defaults:
            _emit(hal_field, defaults[hal_field], "default")

    return {
        "name": name,
        "kind": kind,
        "instance": name,
        "handle": handle,
        "hal_type": meta["hal_type"],
        "init_fn": f"MX_{name}_{meta['init_suffix']}",
        "hal_init_call": meta["hal_init_call"],
        "clock_macro": peripheral_clock_macro(name),
        "pins": info["pins"],
        "config": config,
        "config_fields": config_fields,
        "unmapped_config": unmapped,
        "param_todos": param_todos,
        "config_sources": _count_sources(config_fields),
        "has_config": bool(config_fields),
        "timer_target_hz": timer_target,
        "nvic": nvic,
        "dma": dma,
    }


def _empty_plan(warnings: list[str]) -> dict:
    return {
        "mcu": None, "clocks": [], "gpio": [], "peripherals": [],
        "init_order": ["SystemClock_Config"], "unresolved": [], "warnings": warnings,
        "stats": {"clock_count": 0, "gpio_count": 0, "peripheral_count": 0, "unresolved_count": 0},
    }


# --- Views for the design_framework / describe_framework tools ---------------


def _nvic_summary(nvic: dict | None) -> dict | None:
    """Compact NVIC view for a peripheral summary, or ``None`` when no interrupt."""
    if not nvic:
        return None
    return {
        "requested": nvic.get("requested", False),
        "resolved": nvic.get("resolved", False),
        "irqns": [v["irqn"] for v in nvic.get("vectors", [])],
        "priority": [nvic.get("preempt"), nvic.get("sub")],
        "priority_source": nvic.get("priority_source"),
    }


def _dma_summary(dma: dict | None) -> dict | None:
    """Compact DMA view for a peripheral summary, or ``None`` when no DMA."""
    if not dma:
        return None
    return {
        "requested": dma.get("requested", False),
        "resolved": dma.get("resolved", False),
        "streams": [{"direction": s["direction"], "instance": s["instance"],
                     "irqn": s["nvic"]["irqn"], "conflict": s.get("conflict", False)}
                    for s in dma.get("streams", [])],
        "unresolved_directions": [u["direction"] for u in dma.get("unresolved", [])],
    }


def summarize_framework(plan: dict) -> dict:
    """Compact, human-oriented overview of a FrameworkPlan."""
    return {
        "mcu": plan.get("mcu"),
        "clocks": [c.get("hal_macro") for c in plan.get("clocks", [])],
        "peripherals": [{"name": b["name"], "kind": b["kind"], "handle": b["handle"],
                         "pin_count": len(b["pins"]), "has_config": b["has_config"],
                         "config_sources": b.get("config_sources", {}),
                         "param_todo_count": len(b.get("param_todos", [])),
                         "nvic": _nvic_summary(b.get("nvic")),
                         "dma": _dma_summary(b.get("dma"))}
                        for b in plan.get("peripherals", [])],
        "init_order": plan.get("init_order", []),
        "unresolved_count": plan.get("stats", {}).get("unresolved_count", 0),
        "stats": plan.get("stats", {}),
        "warnings": plan.get("warnings", []),
    }


def framework_view(plan: dict, what: str = "summary") -> dict | None:
    """Return a filtered view of a FrameworkPlan, or ``None`` for an unknown view."""
    if what == "summary":
        return summarize_framework(plan)
    if what == "clocks":
        return {"clocks": plan.get("clocks", [])}
    if what == "gpio":
        return {"gpio": plan.get("gpio", [])}
    if what == "peripherals":
        return {"peripherals": plan.get("peripherals", [])}
    if what == "unresolved":
        return {"unresolved": plan.get("unresolved", [])}
    if what == "init_order":
        return {"init_order": plan.get("init_order", [])}
    return None
