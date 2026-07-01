"""Deterministic DMA association resolver (design synthesis, Pillar D Tier 3).

Turns an opt-in DMA request on a peripheral into the concrete pieces the generated
init needs: the ``DMA_HandleTypeDef`` wiring (``Instance`` + request/channel
selector + direction), the ``__HAL_LINKDMA`` field, the DMA controller clock, and
-- reusing the NVIC backbone -- the DMA stream/channel interrupt vector whose ISR
dispatches into ``HAL_DMA_IRQHandler``.

Honest by construction: the request routing (which controller/stream/channel/request
a ``(peripheral, direction)`` maps to) is a fixed hardware fact encoded in a small
**verified** per-family table (cross-checked against ground-truth CubeMX output);
the DMA stream IRQ vector is *derived* from the resolved stream by the universal
``DMAc_Streams_IRQn`` / ``DMAc_Channels_IRQn`` naming rule; and anything not in the
table -- other peripherals, other families, receive-only mismatches -- is surfaced
as ``dma_unresolved`` with a reason, never a guessed stream.

Standalone pure module (no imports from the rest of the package): everything is
plain dicts so a result serializes straight through the JSON envelope.
"""

# Design keys (in design[name]) that control DMA generation; popped before the
# remaining config is mapped to HAL .Init fields.
DMA_KEYS = ("dma", "dma_priority")

_DEFAULT_PREEMPT = 5
_DEFAULT_SUB = 0

# Per-family DMA architecture. ``unit`` is the addressable resource in the Instance
# name (Stream on F4, Channel on L4); ``select_field`` is the HAL .Init member that
# routes the request, with ``select_prefix`` its macro stem.
_DMA_ARCH = {
    "STM32F4": {"unit": "Stream", "select_field": "Channel", "select_prefix": "DMA_CHANNEL_"},
    "STM32L4": {"unit": "Channel", "select_field": "Request", "select_prefix": "DMA_REQUEST_"},
}

# Verified request routing, keyed family -> peripheral -> direction -> (controller,
# unit, selector). F4 selector is the channel number; L4 selector is the CSELR
# request number. Cross-checked against RM0090 (F4) / RM0394 (L4) + CubeMX output.
_DMA_MAP = {
    "STM32F4": {
        "USART1": {"rx": (2, 2, 4), "tx": (2, 7, 4)},
        "SPI1": {"rx": (2, 0, 3), "tx": (2, 3, 3)},
        "I2C1": {"rx": (1, 0, 1), "tx": (1, 6, 1)},
        "ADC1": {"rx": (2, 4, 0)},
    },
    "STM32L4": {
        "USART1": {"rx": (1, 5, 2), "tx": (1, 4, 2)},
        "SPI1": {"rx": (1, 2, 1), "tx": (1, 3, 1)},
        "I2C1": {"rx": (1, 7, 3), "tx": (1, 6, 3)},
        "ADC1": {"rx": (1, 1, 0)},
    },
}

# Peripheral kinds that support the DMA templates in this tier, and their natural
# transfer directions.
_NATURAL_DIRECTIONS = {
    "uart": ("rx", "tx"),
    "spi": ("rx", "tx"),
    "i2c": ("rx", "tx"),
    "adc": ("rx",),
}

_PRIORITY_MACRO = {
    "low": "DMA_PRIORITY_LOW",
    "medium": "DMA_PRIORITY_MEDIUM",
    "high": "DMA_PRIORITY_HIGH",
    "very_high": "DMA_PRIORITY_VERY_HIGH",
}

# 12-bit ADC samples land in 16-bit words, so the ADC stream defaults to halfword
# alignment; the byte-oriented drivers default to byte alignment.
_DATA_ALIGN = {"adc": "HALFWORD"}


def _natural_directions(kind):
    return _NATURAL_DIRECTIONS.get(kind, ())


def _normalize_directions(dma, kind):
    """Resolve the requested transfer directions from the ``dma`` directive."""
    natural = _natural_directions(kind)
    if dma is True:
        return list(natural)
    if isinstance(dma, str):
        return [dma.lower()]
    if isinstance(dma, (list, tuple)):
        return [str(d).lower() for d in dma]
    # A bare priority (dma_priority set, dma absent) still means "use DMA".
    return list(natural)


def _handle_name(name, kind, direction):
    base = f"hdma_{name.lower()}"
    return base if kind == "adc" else f"{base}_{direction}"


def _link_field(kind, direction):
    if kind == "adc":
        return "DMA_Handle"
    return "hdmarx" if direction == "rx" else "hdmatx"


def _direction_macro(direction):
    return "DMA_PERIPH_TO_MEMORY" if direction == "rx" else "DMA_MEMORY_TO_PERIPH"


def _priority(dma_priority):
    """Return ``(macro, source)`` for the DMA channel priority."""
    if isinstance(dma_priority, str) and dma_priority.lower() in _PRIORITY_MACRO:
        return _PRIORITY_MACRO[dma_priority.lower()], "explicit"
    return "DMA_PRIORITY_LOW", "default"


def _stream_nvic(instance):
    """Derive the DMA stream/channel NVIC vector from the resolved Instance name."""
    irqn = f"{instance}_IRQn"
    return {
        "irqn": irqn,
        "isr": f"{instance}_IRQHandler",
        "handler": "HAL_DMA_IRQHandler",
        "preempt": _DEFAULT_PREEMPT,
        "sub": _DEFAULT_SUB,
        "priority_source": "default",
    }


def _resolve_stream(name, kind, family, direction, priority_macro):
    """Build one DMA stream dict for a direction, or ``None`` when unmapped."""
    table = _DMA_MAP.get(family, {}).get(name, {})
    mapping = table.get(direction)
    if not mapping:
        return None
    arch = _DMA_ARCH[family]
    controller, unit, selector = mapping
    instance = f"DMA{controller}_{arch['unit']}{unit}"
    align = _DATA_ALIGN.get(kind, "BYTE")
    return {
        "direction": direction,
        "handle": _handle_name(name, kind, direction),
        "controller": f"DMA{controller}",
        "instance": instance,
        "clock_macro": f"__HAL_RCC_DMA{controller}_CLK_ENABLE",
        "select_field": arch["select_field"],
        "select_value": f"{arch['select_prefix']}{selector}",
        "direction_macro": _direction_macro(direction),
        "periph_align": f"DMA_PDATAALIGN_{align}",
        "mem_align": f"DMA_MDATAALIGN_{align}",
        "priority_macro": priority_macro,
        "link_field": _link_field(kind, direction),
        "nvic": _stream_nvic(instance),
    }


def _unresolved_reason(name, kind, family):
    if kind not in _NATURAL_DIRECTIONS:
        return f"{name}: DMA templating is not supported for {kind} peripherals in this tier."
    if family not in _DMA_MAP:
        return (f"DMA request mapping for {family or 'this device'} is not in the built-in "
                "table (only STM32F4 / STM32L4 are modelled today).")
    return (f"No DMA request mapping known for {name} on {family or 'this device'} "
            "(built-in table covers USART1 / SPI1 / I2C1 / ADC1).")


def build_dma(name, kind, family, dma=None, dma_priority=None):
    """Build a peripheral's ``dma`` block from its design directives, or ``None``.

    Returns ``None`` when no DMA was requested. Otherwise a dict with the resolved
    streams (each carrying its Instance, request selector, LINKDMA field, clock, and
    the derived stream NVIC vector) plus an honest per-direction ``unresolved`` list
    and, when nothing could be mapped, ``resolved: False`` with a reason.
    """
    requested = bool(dma) or dma_priority is not None
    if not requested:
        return None

    priority_macro, priority_source = _priority(dma_priority)
    directions = _normalize_directions(dma, kind)

    streams = []
    unresolved = []
    for direction in directions:
        stream = _resolve_stream(name, kind, family, direction, priority_macro)
        if stream:
            streams.append(stream)
        else:
            unresolved.append({"direction": direction,
                               "reason": _unresolved_reason(name, kind, family)})

    return {
        "requested": True,
        "priority_source": priority_source,
        "streams": streams,
        "unresolved": unresolved,
        "resolved": bool(streams),
        "unresolved_reason": None if streams else _unresolved_reason(name, kind, family),
    }
