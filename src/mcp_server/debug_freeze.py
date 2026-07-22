"""Resolve DBGMCU debug-freeze register writes per STM32 family.

When the core halts under the debugger, peripherals keep running unless frozen
via the DBGMCU freeze registers. The classic failure mode for autonomous
debugging: you halt at a breakpoint, the independent watchdog (IWDG) is not
fed, and the target silently resets out from under the AI — which then sees an
inexplicable "connection lost". Freezing IWDG/WWDG/timers/RTC on halt removes
that whole class of mystery.

The freeze bits and register addresses are family-specific. This maps the most
common peripherals for the families the project targets; ``set_policy``-style
extension is intentionally left to additional table entries.
"""

DBGMCU_BASE = 0xE0042000

# peripheral -> (register_name, bit) ; register_name -> offset from DBGMCU_BASE
_FAMILIES: dict[str, dict[str, dict]] = {
    "stm32f4": {
        "registers": {"apb1_fz": 0x08, "apb2_fz": 0x0C},
        "bits": {
            "iwdg": ("apb1_fz", 12),
            "wwdg": ("apb1_fz", 11),
            "rtc": ("apb1_fz", 10),
            "tim2": ("apb1_fz", 0),
            "tim3": ("apb1_fz", 1),
            "tim4": ("apb1_fz", 2),
            "tim1": ("apb2_fz", 0),
            "tim8": ("apb2_fz", 1),
        },
    },
    "stm32l4": {
        "registers": {"apb1_fzr1": 0x08, "apb1_fzr2": 0x0C, "apb2_fzr": 0x10},
        "bits": {
            "iwdg": ("apb1_fzr1", 12),
            "wwdg": ("apb1_fzr1", 11),
            "rtc": ("apb1_fzr1", 10),
            "tim2": ("apb1_fzr1", 0),
            "tim3": ("apb1_fzr1", 1),
            "tim1": ("apb2_fzr", 11),
        },
    },
}


def supported_families():
    return sorted(_FAMILIES.keys())


def _normalize_family(family: str) -> str:
    key = (family or "").lower()
    if key in _FAMILIES:
        return key
    # Accept full part numbers like "STM32L431CCT6" -> "stm32l4".
    for known in _FAMILIES:
        prefix = known[: len("stm32x")]  # e.g. "stm32l"
        series = known[-2]               # e.g. "4"
        if key.startswith(prefix) and series in key[len(prefix):len(prefix) + 2]:
            return known
    raise ValueError(f"Unsupported DBGMCU freeze family: {family!r}. Known: {supported_families()}")


def resolve_freeze_targets(family: str, peripherals) -> list:
    """Resolve peripherals to ``{peripheral, register, address, bit, mask}`` entries."""
    fam = _normalize_family(family)
    spec = _FAMILIES[fam]
    targets = []
    for peripheral in peripherals:
        key = peripheral.lower()
        if key not in spec["bits"]:
            raise ValueError(f"Unknown peripheral '{peripheral}' for {fam}. Known: {sorted(spec['bits'])}")
        register, bit = spec["bits"][key]
        targets.append({
            "peripheral": key,
            "register": register,
            "address": DBGMCU_BASE + spec["registers"][register],
            "bit": bit,
            "mask": 1 << bit,
        })
    return targets


def plan_freeze_writes(targets, read_word) -> list:
    """Group targets by register and compute read-modify-write plans.

    ``read_word(address) -> int`` reads the current register value; the plan
    sets every requested freeze bit without disturbing the others.
    """
    by_address: dict[int, dict] = {}
    for target in targets:
        entry = by_address.setdefault(target["address"], {"address": target["address"], "mask": 0, "peripherals": []})
        entry["mask"] |= target["mask"]
        entry["peripherals"].append(target["peripheral"])

    plans = []
    for entry in by_address.values():
        old = read_word(entry["address"])
        new = old | entry["mask"]
        plans.append({
            "address": entry["address"],
            "peripherals": entry["peripherals"],
            "old_value": old,
            "new_value": new,
            "mask": entry["mask"],
        })
    return plans
