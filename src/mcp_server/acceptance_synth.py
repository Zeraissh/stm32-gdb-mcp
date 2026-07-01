"""Auto-derive an **AcceptanceSpec** from a FrameworkPlan (design synthesis, Pillar D Tier 3).

This welds the design solver (Pillar D) to the acceptance judge (Pillar B1): instead
of a human writing the pass/fail contract, we derive it deterministically from what
the FrameworkPlan *said the init code would do*. The agent flashes the generated
init, runs it, and the derived spec verifies the silicon actually reached the planned
state — closing the loop (Pillar C) with a machine-generated judge.

Derived checks, every one honest (a target it cannot place is surfaced in
``unresolved``, never fabricated):

* ``no_fault`` — always emitted. After ``BSP_Init()`` the MCU must not be in a fault
  state. Needs no target-specific data (the Cortex-M SCB fault registers are
  architecture-standard), so it is always correct and always meaningful.
* ``memory_u32`` clock-enable checks — for each clock the plan enables, assert the
  matching RCC enable bit is set. The register address + bit position are **resolved,
  never guessed**: a ``clock_resolver`` (from the target's SVD, or an explicit map)
  supplies them. A clock the resolver can't place is surfaced in ``unresolved`` — no
  fabricated address ever reaches the spec.
* ``memory_u32`` NVIC ISER checks — for each interrupt the plan enables (peripheral
  vectors + DMA stream vectors), assert its NVIC set-enable bit is set. The ISER block
  is Cortex-M *architecture-standard* (``ISER[n] = 0xE000E100 + 4*n``, IRQ ``k`` -> bit
  ``k % 32``), so once an ``irq_resolver`` maps the device-specific IRQ *name* to its
  *number* the bit placement is exact. An IRQ whose number is unknown is ``unresolved``.
* ``memory_u32`` GPIO MODER checks — for each configured pin, assert its two mode bits
  match the planned role (AF = ``0b10``, analog = ``0b11``) using a masked equality.
  MODER's offset-0 layout is arch-standard on every STM32 port **except F1** (CRL/CRH),
  which is excluded honestly; the port base comes from a ``gpio_resolver``.
* ``stopped_at`` — optional, when the caller names an entry symbol to reach.
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

# The NVIC ISER block is Cortex-M architecture-standard: ISER[n] lives at
# _NVIC_ISER0 + 4*n, and IRQ number k is bit (k % 32) of ISER[k // 32]. So once a
# device-specific IRQ *number* is resolved, the enable-bit placement is exact and
# needs no further target data.
_NVIC_ISER0 = 0xE000E100
_IRQN_SUFFIX = "_IRQn"

# GPIO MODER two-bit mode per abstract pin role. MODER's offset-0, 2-bits-per-pin
# layout (AF = 0b10, analog = 0b11) is arch-standard on every STM32 GPIO port except
# F1, which uses CRL/CRH and is excluded (see _is_f1_family).
_MODER_MODE_BITS = {"af_pp": 0b10, "af_od": 0b10, "analog": 0b11}


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


def _strip_irqn(irq_name: str) -> str:
    return irq_name[: -len(_IRQN_SUFFIX)] if irq_name.endswith(_IRQN_SUFFIX) else irq_name


def _lookup_irq(table, irq_name: str):
    """Return the IRQ number for *irq_name* from *table*, trying with/without _IRQn."""
    if not isinstance(table, dict):
        return None
    for key in (irq_name, _strip_irqn(irq_name)):
        value = table.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def dict_irq_resolver(irq_map: dict | None, line: str | None, family: str | None):
    """Build an IRQ-number resolver from an explicit map.

    ``irq_map`` shape: ``{line_or_family: {irq_name: number}}``; the ``irq_name`` key may
    carry the ``_IRQn`` suffix or not. Returns ``(irq_name) -> number | None``.
    """
    table = None
    if isinstance(irq_map, dict):
        if line and line in irq_map:
            table = irq_map[line]
        elif family and family in irq_map:
            table = irq_map[family]

    def resolver(irq_name: str):
        return _lookup_irq(table, irq_name)

    return resolver


def dict_gpio_resolver(base_map: dict | None, line: str | None, family: str | None):
    """Build a GPIO port-base resolver from an explicit map.

    ``base_map`` shape: ``{line_or_family: {port_letter: base_address}}`` where
    ``base_address`` is the port's MODER address (int or hex string). Returns
    ``(port_letter) -> address | None``.
    """
    table = None
    if isinstance(base_map, dict):
        if line and line in base_map:
            table = base_map[line]
        elif family and family in base_map:
            table = base_map[family]

    def resolver(port: str):
        if not isinstance(table, dict):
            return None
        entry = table.get(port)
        if entry is None and isinstance(port, str):
            entry = table.get(port.upper())
        return entry

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


def svd_irq_resolver(svd_parser):
    """Build an IRQ-number resolver from a loaded SVD (best-effort, never raises).

    Reads the SVD's ``<interrupt>`` table (name -> value) once; a name the SVD does not
    list yields ``None`` so the ISER check is skipped, never fabricated.
    """
    try:
        table = svd_parser.interrupt_numbers()
    except Exception:
        table = {}

    def resolver(irq_name: str):
        return _lookup_irq(table, irq_name)

    return resolver


def svd_gpio_resolver(svd_parser):
    """Build a GPIO port-base resolver from a loaded SVD (best-effort, never raises).

    Uses the ``GPIO<port>.MODER`` register address as the port base (MODER sits at
    offset 0). A port whose GPIO peripheral or MODER register is absent yields ``None``.
    """
    cache: dict = {}

    def resolver(port: str):
        if port in cache:
            return cache[port]
        try:
            address = svd_parser.get_register(f"GPIO{port}", "MODER")["address_int"]
        except Exception:
            address = None
        cache[port] = address
        return address

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


def _nvic_targets(plan: dict) -> list[dict]:
    """Collect the interrupts the plan enables (peripheral + DMA-stream vectors).

    Deduplicated by IRQ name, so a shared vector (e.g. ADC1_2_IRQn) or a peripheral and
    its DMA stream landing on one line is verified once. A conflicting DMA stream is
    skipped -- its init is not emitted, so there is nothing to verify.
    """
    seen: set = set()
    targets: list[dict] = []

    def _add(irq_name, source):
        if not irq_name or irq_name in seen:
            return
        seen.add(irq_name)
        targets.append({"irq": irq_name, "source": source})

    for block in plan.get("peripherals", []):
        nvic = block.get("nvic")
        if nvic:
            for vector in nvic.get("vectors", []):
                _add(vector.get("irqn"), f"{block['name']} interrupt")
        dma = block.get("dma")
        if dma:
            for stream in dma.get("streams", []):
                if stream.get("conflict"):
                    continue
                _add(stream.get("nvic", {}).get("irqn"), f"{block['name']} DMA {stream.get('direction')}")
    return targets


def _gpio_targets(plan: dict) -> list[dict]:
    """Collect configured pins whose two MODER bits are known from their abstract role."""
    targets = []
    for entry in plan.get("gpio", []):
        role = entry.get("role")
        if role in _MODER_MODE_BITS and entry.get("port") is not None and entry.get("pin") is not None:
            targets.append({"port": entry["port"], "pin": int(entry["pin"]), "role": role})
    return targets


def _is_f1_family(mcu: dict) -> bool:
    """True for STM32F1, whose GPIO uses CRL/CRH instead of the arch-standard MODER."""
    family = (mcu.get("family") or "").upper()
    line = (mcu.get("line") or "").upper()
    return family.startswith("STM32F1") or line.startswith("STM32F1")


def derive_acceptance_spec(plan: dict, clock_resolver=None, options: dict | None = None,
                           irq_resolver=None, gpio_resolver=None) -> dict:
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

    resolved_nvic = 0
    if options.get("include_nvic", True):
        nvic_targets = _nvic_targets(plan)
        for target in nvic_targets:
            number = irq_resolver(target["irq"]) if irq_resolver else None
            if number is None:
                if irq_resolver is not None:
                    unresolved.append({
                        "type": "irq_number_unknown",
                        "irq": target["irq"],
                        "detail": f"No IRQ number for {target['irq']} "
                                  "(supply an SVD or irq_map); NVIC ISER check skipped.",
                    })
                continue
            number = int(number)
            address = _NVIC_ISER0 + 4 * (number // 32)
            checks.append({
                "id": f"nvic_{target['irq']}_enabled",
                "kind": "memory_u32",
                "address": _hex32(address),
                "expect": _hex32(1 << (number % 32)),
                "op": "bits_set",
                "description": f"NVIC set-enable for {target['irq']} (IRQ {number}) is set after init.",
            })
            resolved_nvic += 1
        if irq_resolver is None and nvic_targets:
            notes.append("No IRQ resolver available; plan enables interrupts but NVIC ISER "
                         "checks were skipped. Load an SVD or pass irq_map to verify them.")

    resolved_gpio = 0
    if options.get("include_gpio", True):
        gpio_targets = _gpio_targets(plan)
        if gpio_targets and _is_f1_family(mcu):
            unresolved.append({
                "type": "gpio_moder_unsupported",
                "detail": "STM32F1 GPIO uses CRL/CRH, not the arch-standard MODER; "
                          f"{len(gpio_targets)} GPIO mode check(s) skipped.",
            })
        elif gpio_targets:
            for target in gpio_targets:
                base = gpio_resolver(target["port"]) if gpio_resolver else None
                if base is None:
                    if gpio_resolver is not None:
                        unresolved.append({
                            "type": "gpio_base_unknown",
                            "port": target["port"],
                            "detail": f"No GPIO{target['port']} base address "
                                      "(supply an SVD or gpio_map); MODER check skipped.",
                        })
                    continue
                shift = 2 * target["pin"]
                mode_bits = _MODER_MODE_BITS[target["role"]]
                checks.append({
                    "id": f"gpio_P{target['port']}{target['pin']}_mode",
                    "kind": "memory_u32",
                    "address": _normalize_address(base),
                    "mask": _hex32(0b11 << shift),
                    "expect": _hex32(mode_bits << shift),
                    "op": "eq",
                    "description": f"GPIO P{target['port']}{target['pin']} MODER = "
                                   f"{'AF' if mode_bits == 0b10 else 'analog'} after init.",
                })
                resolved_gpio += 1
            if gpio_resolver is None:
                notes.append("No GPIO resolver available; plan configures pins but MODER "
                             "checks were skipped. Load an SVD or pass gpio_map to verify them.")

    spec = {
        "name": options.get("name") or f"auto-init:{mcu.get('line') or mcu.get('family') or 'stm32'}",
        "description": options.get("description")
        or "Auto-derived from the FrameworkPlan: no-fault + RCC clock enables + NVIC ISER "
           "+ GPIO MODER after BSP_Init().",
        "checks": checks,
    }
    return {
        "spec": spec,
        "unresolved": unresolved,
        "notes": notes,
        "stats": {
            "check_count": len(checks),
            "clock_checks": resolved_clocks,
            "nvic_checks": resolved_nvic,
            "gpio_checks": resolved_gpio,
            "unresolved_count": len(unresolved),
        },
    }
