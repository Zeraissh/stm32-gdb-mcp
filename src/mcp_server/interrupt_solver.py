"""Deterministic NVIC interrupt resolver (design synthesis, Pillar D Tier 3).

Turns an opt-in interrupt request on a peripheral into the concrete pieces the
generated init needs: the CMSIS ``IRQn`` vector name(s), the ``HAL_NVIC_SetPriority``
/ ``HAL_NVIC_EnableIRQ`` calls, and the interrupt service routine that dispatches
into the matching HAL handler.

Honest by construction: the regularly-named vectors (``USARTx_IRQn`` / ``SPIx_IRQn``
and the ``I2Cx_EV/ER`` pair) are derived by universal rule; the irregular ones
(shared timer/ADC/DAC vectors) come from a small built-in per-family table; and
anything not covered is surfaced as ``nvic_unresolved`` with a reason and an
``irqn=`` escape hatch, never guessed. Interrupt priority is an engineer decision,
so a working default is emitted with a review note when none is supplied.

The irregular vectors and the I2C EV/ER-split family set come from the device-pack
registry (``device_packs``), so a new family is a verified pack, not a hardcoded
guess. Otherwise a pure module: everything is plain dicts so a result serializes
straight through the JSON envelope.
"""

from . import device_packs

# Design keys (in design[name]) that control interrupt generation; popped before
# the remaining config is mapped to HAL .Init fields.
NVIC_KEYS = ("nvic", "nvic_priority", "irqn")

_DEFAULT_PREEMPT = 5
_DEFAULT_SUB = 0

# HAL top-level IRQ handler per driver kind (the ISR dispatches into this).
_HAL_IRQ_HANDLER = {
    "uart": "HAL_UART_IRQHandler",
    "spi": "HAL_SPI_IRQHandler",
    "i2c": "HAL_I2C_EV_IRQHandler",   # the error vector uses HAL_I2C_ER_IRQHandler
    "timer": "HAL_TIM_IRQHandler",
    "adc": "HAL_ADC_IRQHandler",
    "dac": "HAL_DAC_IRQHandler",
}


def _handler_for(kind, role):
    if kind == "i2c":
        return "HAL_I2C_ER_IRQHandler" if role == "error" else "HAL_I2C_EV_IRQHandler"
    return _HAL_IRQ_HANDLER.get(kind)


def _role_from_name(irqn, kind):
    if kind == "i2c":
        upper = irqn.upper()
        if "_ER" in upper:
            return "error"
        if "_EV" in upper:
            return "event"
    return "global"


def _vector(irqn, kind, role, source):
    return {
        "irqn": irqn,
        "handler": _handler_for(kind, role),
        "isr": irqn.replace("_IRQn", "_IRQHandler"),
        "role": role,
        "source": source,
    }


def resolve_vectors(name, kind, family, irqn_override=None):
    """Resolve the NVIC vector(s) for a peripheral, or ``[]`` when unknown.

    Precedence: explicit ``irqn`` override > built-in family table > universal
    regular rule > unresolved. The universal rule covers only the vectors whose
    naming is family-agnostic (uart/spi single vector, i2c EV/ER pair).
    """
    if irqn_override:
        names = [irqn_override] if isinstance(irqn_override, str) else list(irqn_override)
        return [_vector(n, kind, _role_from_name(n, kind), "override") for n in names if n]

    if kind in ("uart", "spi"):
        return [_vector(f"{name}_IRQn", kind, "global", "regular")]
    if kind == "i2c":
        if device_packs.i2c_dual(family):
            return [_vector(f"{name}_EV_IRQn", kind, "event", "regular"),
                    _vector(f"{name}_ER_IRQn", kind, "error", "regular")]
        return []

    table = device_packs.nvic_table(family)
    if name in table:
        return [_vector(n, kind, "global", "table") for n in table[name]]
    return []


def _priority(nvic, nvic_priority):
    """Return ``(preempt, sub, source)`` from the design directives."""
    if isinstance(nvic, dict) and ("preempt" in nvic or "sub" in nvic):
        preempt = nvic.get("preempt", _DEFAULT_PREEMPT)
        sub = nvic.get("sub", _DEFAULT_SUB)
        if isinstance(preempt, int) and isinstance(sub, int):
            return preempt, sub, "explicit"
    if isinstance(nvic_priority, int) and not isinstance(nvic_priority, bool):
        return nvic_priority, _DEFAULT_SUB, "explicit"
    if isinstance(nvic_priority, (list, tuple)) and len(nvic_priority) == 2 \
            and all(isinstance(v, int) for v in nvic_priority):
        return nvic_priority[0], nvic_priority[1], "explicit"
    return _DEFAULT_PREEMPT, _DEFAULT_SUB, "default"


def _unresolved_reason(name, kind, family):
    if kind == "i2c":
        return (f"I2C interrupt vector naming for {family or 'this device'} is family-specific "
                "(some families merge EV/ER into one vector); supply irqn=.")
    if kind in ("timer", "adc", "dac"):
        return (f"{name} interrupt vector is device-specific for {family or 'this device'} "
                "(shared/combined vectors); supply irqn= (e.g. 'TIM1_UP_TIM10_IRQn').")
    return f"No interrupt vector known for {name} on {family or 'this device'}; supply irqn=."


def build_nvic(name, kind, family, nvic=None, nvic_priority=None, irqn=None):
    """Build a peripheral's ``nvic`` block from its design directives, or ``None``.

    Returns ``None`` when no interrupt was requested. Otherwise a dict with the
    priority, the resolved vectors, and -- when nothing could be resolved -- an
    honest ``unresolved_reason`` plus ``resolved: False``.
    """
    requested = bool(nvic) or nvic_priority is not None or irqn is not None
    if not requested:
        return None
    preempt, sub, source = _priority(nvic, nvic_priority)
    vectors = resolve_vectors(name, kind, family, irqn)
    return {
        "requested": True,
        "preempt": preempt,
        "sub": sub,
        "priority_source": source,
        "vectors": vectors,
        "resolved": bool(vectors),
        "unresolved_reason": None if vectors else _unresolved_reason(name, kind, family),
    }
