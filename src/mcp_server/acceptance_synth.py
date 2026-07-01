"""Auto-derive an **AcceptanceSpec** from a FrameworkPlan (design synthesis, Pillar D Tier 3).

This welds the design solver (Pillar D) to the acceptance judge (Pillar B1): instead
of a human writing the pass/fail contract, we derive it deterministically from what
the FrameworkPlan *said the init code would do*. The agent flashes the generated
init, runs it, and the derived spec verifies the silicon actually reached the planned
state — closing the loop (Pillar C) with a machine-generated judge.

Two kinds of derived checks, both honest:

* ``no_fault`` — always emitted. After ``BSP_Init()`` the MCU must not be in a fault
  state. Needs no target-specific data (the Cortex-M SCB fault registers are
  architecture-standard), so it is always correct and always meaningful.
* ``memory_u32`` clock-enable checks — for each clock the plan enables, assert the
  matching RCC enable bit is set. The register address + bit position are **resolved,
  never guessed**: a ``clock_resolver`` (from the target's SVD, or an explicit map)
  supplies them. A clock the resolver can't place is surfaced in ``unresolved`` — no
  fabricated address ever reaches the spec.
"""

import re

# RCC registers that hold peripheral/GPIO clock-enable bits across STM32 families.
_RCC_ENABLE_REGS = (
    "AHB1ENR", "AHB2ENR", "AHB3ENR", "AHB4ENR", "AHBENR",
    "APB1ENR", "APB2ENR", "APB3ENR", "APB4ENR",
    "APB1ENR1", "APB1ENR2", "APB1LENR", "APB1HENR",
    "APBENR1", "APBENR2", "IOPENR",
)

_GPIO_PORT_RE = re.compile(r"^GPIO([A-K])$")


def _hex32(value: int) -> str:
    return f"0x{value & 0xFFFFFFFF:08x}"


def _normalize_address(address) -> str:
    if isinstance(address, int):
        return _hex32(address)
    return str(address)


def _enable_field_candidates(clock_name: str) -> list[str]:
    """Candidate RCC enable-field names for a clock (``USART1`` -> ``USART1EN``)."""
    upper = clock_name.upper()
    candidates = [f"{upper}EN"]
    match = _GPIO_PORT_RE.match(upper)
    if match:
        candidates.append(f"IOP{match.group(1)}EN")  # F1 / L0 GPIO naming
    return candidates


def dict_clock_resolver(register_map: dict | None, line: str | None, family: str | None):
    """Build a resolver from an explicit map.

    ``register_map`` shape: ``{line_or_family: {clock_name: {"address", "bit"}}}``.
    Returns ``(clock_name) -> {"address", "bit"} | None``.
    """
    table = None
    if isinstance(register_map, dict):
        if line and line in register_map:
            table = register_map[line]
        elif family and family in register_map:
            table = register_map[family]

    def resolver(clock_name: str):
        if not isinstance(table, dict):
            return None
        entry = table.get(clock_name)
        if isinstance(entry, dict) and entry.get("address") is not None and entry.get("bit") is not None:
            return {"address": entry["address"], "bit": int(entry["bit"])}
        return None

    return resolver


def svd_clock_resolver(svd_parser, rcc_name: str = "RCC"):
    """Build a resolver that reads clock-enable bits from a loaded SVD (best-effort).

    Scans the RCC enable registers for a field matching the clock's ``<NAME>EN``
    convention and returns its absolute address + bit offset. Never raises — an
    unresolved clock simply yields ``None``.
    """
    cache: dict = {}

    def resolver(clock_name: str):
        if clock_name in cache:
            return cache[clock_name]
        candidates = _enable_field_candidates(clock_name)
        resolved = None
        for reg_name in _RCC_ENABLE_REGS:
            try:
                register = svd_parser.get_register(rcc_name, reg_name)
            except Exception:
                continue
            for field in register.get("fields", []):
                if field.get("name") in candidates:
                    resolved = {"address": register["address_int"], "bit": field["bit_offset"]}
                    break
            if resolved:
                break
        cache[clock_name] = resolved
        return resolved

    return resolver


def _clock_targets(plan: dict) -> list[dict]:
    """Flatten plan clocks into ``{name, kind, hal_macro}`` targets."""
    targets = []
    for clock in plan.get("clocks", []):
        if clock.get("kind") == "gpio_port":
            targets.append({"name": f"GPIO{clock['port']}", "kind": "gpio_port",
                            "hal_macro": clock.get("hal_macro")})
        elif clock.get("kind") == "peripheral":
            targets.append({"name": clock["peripheral"], "kind": "peripheral",
                            "hal_macro": clock.get("hal_macro")})
    return targets


def derive_acceptance_spec(plan: dict, clock_resolver=None, options: dict | None = None) -> dict:
    """Derive an AcceptanceSpec (+ diagnostics) from a FrameworkPlan.

    Returns ``{"spec": {name, description, checks}, "unresolved": [...],
    "notes": [...], "stats": {...}}``. ``spec`` is ready to feed
    ``validate_acceptance_spec`` / ``load_acceptance``.
    """
    options = options or {}
    mcu = plan.get("mcu") or {}
    checks: list[dict] = []
    unresolved: list[dict] = []
    notes: list[str] = []

    if options.get("include_no_fault", True):
        checks.append({
            "id": "no_fault_after_init",
            "kind": "no_fault",
            "description": "MCU is not in a fault state after BSP_Init().",
        })

    stopped_at = options.get("stopped_at")
    if stopped_at:
        checks.append({
            "id": "stopped_at_entry",
            "kind": "stopped_at",
            "symbol": stopped_at,
            "description": f"Execution reached {stopped_at} after init.",
        })

    resolved_clocks = 0
    for target in _clock_targets(plan):
        placement = clock_resolver(target["name"]) if clock_resolver else None
        if not placement:
            unresolved.append({
                "type": "clock_register_unknown",
                "clock": target["name"],
                "detail": f"No RCC enable-bit placement for {target['name']} "
                          "(supply an SVD or register_map); clock check skipped.",
            })
            continue
        bit = int(placement["bit"])
        checks.append({
            "id": f"clk_{target['name']}_enabled",
            "kind": "memory_u32",
            "address": _normalize_address(placement["address"]),
            "expect": _hex32(1 << bit),
            "op": "bits_set",
            "description": f"RCC clock enable for {target['name']} (bit {bit}) is set after init.",
        })
        resolved_clocks += 1

    if clock_resolver is None:
        notes.append("No clock resolver available; emitted no_fault only. "
                     "Load an SVD or pass register_map to also verify clock enables.")

    spec = {
        "name": options.get("name") or f"auto-init:{mcu.get('line') or mcu.get('family') or 'stm32'}",
        "description": options.get("description")
        or "Auto-derived from the FrameworkPlan: no-fault + RCC clock enables after BSP_Init().",
        "checks": checks,
    }
    return {
        "spec": spec,
        "unresolved": unresolved,
        "notes": notes,
        "stats": {
            "check_count": len(checks),
            "clock_checks": resolved_clocks,
            "unresolved_count": len(unresolved),
        },
    }
