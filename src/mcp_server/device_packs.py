"""Device packs: verified per-family fact tables for design synthesis (Pillar F).

The deterministic solvers (clock / DMA / NVIC / timer) each need a handful of
*device-specific facts* -- a PLL profile, a DMA request-routing table, the
irregular NVIC vectors, which timers sit on APB2. Those facts are datasheet /
reference-manual truth: the machine layer must never guess them. GPIO
alternate-function numbers already solve this by being data-driven (a CubeMX
``db_path`` / ``af_map``); this module generalizes that pattern to the other
three tables.

A **device pack** is a validated JSON object of verified facts for one family
(schema ``stm32-device-pack/v1``). STM32F4 / STM32L4 ship as built-in packs (the
facts formerly hardcoded in the solvers, relocated verbatim). New families arrive
as user-supplied packs via ``register_pack`` / ``load_pack`` -- so coverage grows
by supplying *verifiable data*, never by trusting the model's memory. A family
with no pack stays honestly ``unresolved``; a malformed pack is rejected with a
list of problems, never half-loaded.

Pure module: no imports from the rest of the package, everything plain dicts.
"""

import copy
import json

SCHEMA = "stm32-device-pack/v1"
_MHZ = 1_000_000

# Sections a pack may contribute (all optional, but at least one is required).
_SECTIONS = ("clock", "dma", "nvic", "timer")

# --- built-in clock profiles (relocated from clock_solver, values identical) --

_F407_FLASH = ((30 * _MHZ, 0), (60 * _MHZ, 1), (90 * _MHZ, 2),
               (120 * _MHZ, 3), (150 * _MHZ, 4), (168 * _MHZ, 5))
_F411_FLASH = ((30 * _MHZ, 0), (64 * _MHZ, 1), (90 * _MHZ, 2), (100 * _MHZ, 3))
_F401_FLASH = ((30 * _MHZ, 0), (60 * _MHZ, 1), (84 * _MHZ, 2))


def _f4_profile(max_sysclk, max_pclk1, max_pclk2, flash_latency, *, family="STM32F4"):
    """Build an F4-style profile (PLLP feeds SYSCLK; M/N/Q plain ints)."""
    return {
        "family": family,
        "sysclk_pll_field": "P",
        "hsi_hz": 16 * _MHZ,
        "pll": {
            "m": (2, 63), "n": (50, 432), "sysclk_div_set": (2, 4, 6, 8),
            "q": (2, 15), "vco_in_hz": (1 * _MHZ, 2 * _MHZ),
            "vco_out_hz": (100 * _MHZ, 432 * _MHZ), "ideal_vco_in_hz": 2 * _MHZ,
        },
        "max_sysclk_hz": max_sysclk, "max_hclk_hz": max_sysclk,
        "max_pclk1_hz": max_pclk1, "max_pclk2_hz": max_pclk2,
        "flash_latency": flash_latency,
        "voltage_note": "flash wait-states for 2.7-3.6 V (VOS scale 1)",
    }


def _l4_profile():
    """Mainstream STM32L4 (<= 80 MHz): PLLR feeds SYSCLK; PLLP/PLLQ are macro fields."""
    return {
        "family": "STM32L4",
        "sysclk_pll_field": "R",
        "hsi_hz": 16 * _MHZ,
        "pll": {
            "m": (1, 8), "n": (8, 86), "sysclk_div_set": (2, 4, 6, 8),
            "q": (2, 4, 6, 8), "vco_in_hz": (4 * _MHZ, 16 * _MHZ),
            "vco_out_hz": (64 * _MHZ, 344 * _MHZ), "ideal_vco_in_hz": 16 * _MHZ,
        },
        "max_sysclk_hz": 80 * _MHZ, "max_hclk_hz": 80 * _MHZ,
        "max_pclk1_hz": 80 * _MHZ, "max_pclk2_hz": 80 * _MHZ,
        "flash_latency": ((16 * _MHZ, 0), (32 * _MHZ, 1), (48 * _MHZ, 2),
                          (64 * _MHZ, 3), (80 * _MHZ, 4)),
        "voltage_note": "flash wait-states for range 1 (1.2 V); L4+ (L4R/L4S) differ",
    }


def _f4_pack():
    f407 = _f4_profile(168 * _MHZ, 42 * _MHZ, 84 * _MHZ, _F407_FLASH)
    f401 = _f4_profile(84 * _MHZ, 42 * _MHZ, 84 * _MHZ, _F401_FLASH)
    f411 = _f4_profile(100 * _MHZ, 50 * _MHZ, 100 * _MHZ, _F411_FLASH)
    return {
        "schema": SCHEMA,
        "family": "STM32F4",
        "clock": {"profiles": [
            {"match_lines": ["STM32F407", "STM32F405", "STM32F415", "STM32F417"], "profile": f407},
            {"match_lines": ["STM32F401"], "profile": f401},
            {"match_lines": ["STM32F411"], "profile": f411},
        ]},
        "dma": {
            "arch": {"unit": "Stream", "select_field": "Channel", "select_prefix": "DMA_CHANNEL_"},
            "map": {
                "USART1": {"rx": (2, 2, 4), "tx": (2, 7, 4)},
                "SPI1": {"rx": (2, 0, 3), "tx": (2, 3, 3)},
                "I2C1": {"rx": (1, 0, 1), "tx": (1, 6, 1)},
                "ADC1": {"rx": (2, 4, 0)},
            },
        },
        "nvic": {"i2c_dual": True, "irq": {
            "TIM2": ["TIM2_IRQn"], "TIM3": ["TIM3_IRQn"], "TIM4": ["TIM4_IRQn"], "TIM5": ["TIM5_IRQn"],
            "TIM6": ["TIM6_DAC_IRQn"], "TIM7": ["TIM7_IRQn"],
            "ADC1": ["ADC_IRQn"], "ADC2": ["ADC_IRQn"], "ADC3": ["ADC_IRQn"],
            "DAC": ["TIM6_DAC_IRQn"], "DAC1": ["TIM6_DAC_IRQn"],
        }},
        "timer": {"apb2": ["TIM1", "TIM8", "TIM9", "TIM10", "TIM11"], "bits32": ["TIM2", "TIM5"]},
    }


def _l4_pack():
    return {
        "schema": SCHEMA,
        "family": "STM32L4",
        "clock": {
            "exclude_lines": ["STM32L4R", "STM32L4S", "STM32L4P", "STM32L4Q"],
            "profiles": [{"match_prefix": "STM32L4", "match_family": "STM32L4", "profile": _l4_profile()}],
        },
        "dma": {
            "arch": {"unit": "Channel", "select_field": "Request", "select_prefix": "DMA_REQUEST_"},
            "map": {
                "USART1": {"rx": (1, 5, 2), "tx": (1, 4, 2)},
                "SPI1": {"rx": (1, 2, 1), "tx": (1, 3, 1)},
                "I2C1": {"rx": (1, 7, 3), "tx": (1, 6, 3)},
                "ADC1": {"rx": (1, 1, 0)},
            },
        },
        "nvic": {"i2c_dual": True, "irq": {
            "TIM2": ["TIM2_IRQn"], "TIM3": ["TIM3_IRQn"], "TIM4": ["TIM4_IRQn"], "TIM5": ["TIM5_IRQn"],
            "TIM6": ["TIM6_DAC_IRQn"], "TIM7": ["TIM7_IRQn"],
            "ADC1": ["ADC1_2_IRQn"], "ADC2": ["ADC1_2_IRQn"],
            "DAC": ["TIM6_DAC_IRQn"], "DAC1": ["TIM6_DAC_IRQn"],
        }},
        "timer": {"apb2": ["TIM1", "TIM8", "TIM15", "TIM16", "TIM17"], "bits32": ["TIM2", "TIM5"]},
    }


_BUILTIN = {"STM32F4": _f4_pack(), "STM32L4": _l4_pack()}
_EXTERNAL: dict = {}

# I2C EV/ER split is a naming *rule* known for more families than we ship full
# packs for; a pack's ``nvic.i2c_dual`` extends this seed.
_I2C_DUAL_SEED = frozenset({"STM32F1", "STM32F2", "STM32F4", "STM32F7", "STM32L1", "STM32L4"})


# --- registry -----------------------------------------------------------------


def _all_families():
    """Deduped family list, external shadowing built-in of the same name."""
    return list(dict.fromkeys(list(_BUILTIN) + list(_EXTERNAL)))


def get_pack(family):
    """Return the effective pack for a family (external wins), or ``None``."""
    if family in _EXTERNAL:
        return _EXTERNAL[family]
    return _BUILTIN.get(family)


def _section(family, key):
    pack = get_pack(family)
    return pack.get(key) if pack else None


def _normalize(pack):
    """Deep-copy a pack and coerce DMA map triples to tuples (built-in shape)."""
    pack = copy.deepcopy(pack)
    dma = pack.get("dma")
    if isinstance(dma, dict):
        for dirs in (dma.get("map") or {}).values():
            if isinstance(dirs, dict):
                for direction, triple in list(dirs.items()):
                    if isinstance(triple, list):
                        dirs[direction] = tuple(triple)
    return pack


# --- solver-facing accessors --------------------------------------------------


def dma_arch(family):
    dma = _section(family, "dma")
    return dma.get("arch") if isinstance(dma, dict) else None


def dma_map(family):
    dma = _section(family, "dma")
    return (dma.get("map") or {}) if isinstance(dma, dict) else {}


def dma_families():
    """Families that carry a non-empty DMA request table."""
    return sorted(f for f in _all_families()
                  if isinstance(_section(f, "dma"), dict) and (_section(f, "dma").get("map")))


def nvic_table(family):
    nvic = _section(family, "nvic")
    return (nvic.get("irq") or {}) if isinstance(nvic, dict) else {}


def i2c_dual(family):
    nvic = _section(family, "nvic")
    if isinstance(nvic, dict) and "i2c_dual" in nvic:
        return bool(nvic["i2c_dual"])
    return family in _I2C_DUAL_SEED


def timer_apb2(family):
    """Set of APB2 timers, or ``None`` when the family is unmodelled (honest)."""
    timer = _section(family, "timer")
    if not isinstance(timer, dict) or "apb2" not in timer:
        return None
    return set(timer["apb2"])


def timer_bits32(family):
    timer = _section(family, "timer")
    return set(timer.get("bits32") or []) if isinstance(timer, dict) else set()


def clock_resolution_data():
    """Flattened clock match entries + known-unmodelled exclusion prefixes."""
    entries, exclusions = [], []
    for family in _all_families():
        clock = _section(family, "clock")
        if not isinstance(clock, dict):
            continue
        exclusions.extend(clock.get("exclude_lines") or [])
        entries.extend(clock.get("profiles") or [])
    return {"entries": entries, "exclusions": exclusions}


# --- validation + loading -----------------------------------------------------


def _validate_dma(dma):
    if not isinstance(dma, dict):
        return ["dma must be an object."]
    problems = []
    arch = dma.get("arch")
    if not isinstance(arch, dict):
        problems.append("dma.arch must be an object.")
    else:
        for key in ("unit", "select_field", "select_prefix"):
            if not isinstance(arch.get(key), str):
                problems.append(f"dma.arch.{key} must be a string.")
    mapping = dma.get("map")
    if not isinstance(mapping, dict):
        problems.append("dma.map must be an object.")
    else:
        for periph, dirs in mapping.items():
            if not isinstance(dirs, dict):
                problems.append(f"dma.map.{periph} must be an object.")
                continue
            for direction, triple in dirs.items():
                if direction not in ("rx", "tx"):
                    problems.append(f"dma.map.{periph}.{direction}: direction must be 'rx' or 'tx'.")
                if not (isinstance(triple, (list, tuple)) and len(triple) == 3
                        and all(isinstance(x, int) for x in triple)):
                    problems.append(f"dma.map.{periph}.{direction} must be [controller, unit, selector] ints.")
    return problems


def _validate_nvic(nvic):
    if not isinstance(nvic, dict):
        return ["nvic must be an object."]
    problems = []
    if "i2c_dual" in nvic and not isinstance(nvic["i2c_dual"], bool):
        problems.append("nvic.i2c_dual must be a boolean.")
    irq = nvic.get("irq", {})
    if not isinstance(irq, dict):
        problems.append("nvic.irq must be an object.")
    else:
        for name, vectors in irq.items():
            if not (isinstance(vectors, list) and vectors and all(isinstance(v, str) for v in vectors)):
                problems.append(f"nvic.irq.{name} must be a non-empty list of IRQn strings.")
    return problems


def _validate_timer(timer):
    if not isinstance(timer, dict):
        return ["timer must be an object."]
    problems = []
    for key in ("apb2", "bits32"):
        if key in timer and not (isinstance(timer[key], list)
                                 and all(isinstance(x, str) for x in timer[key])):
            problems.append(f"timer.{key} must be a list of timer-name strings.")
    return problems


def _validate_clock(clock):
    if not isinstance(clock, dict):
        return ["clock must be an object."]
    problems = []
    excl = clock.get("exclude_lines")
    if excl is not None and not (isinstance(excl, list) and all(isinstance(x, str) for x in excl)):
        problems.append("clock.exclude_lines must be a list of strings.")
    profiles = clock.get("profiles")
    if not isinstance(profiles, list) or not profiles:
        problems.append("clock.profiles must be a non-empty list.")
    else:
        for i, entry in enumerate(profiles):
            if not isinstance(entry, dict):
                problems.append(f"clock.profiles[{i}] must be an object.")
                continue
            if not isinstance(entry.get("profile"), dict):
                problems.append(f"clock.profiles[{i}].profile must be an object.")
            if not any(k in entry for k in ("match_lines", "match_prefix", "match_family")):
                problems.append(f"clock.profiles[{i}] needs a match_lines/match_prefix/match_family selector.")
    return problems


_SECTION_VALIDATORS = {"clock": _validate_clock, "dma": _validate_dma,
                       "nvic": _validate_nvic, "timer": _validate_timer}


def validate_pack(pack):
    """Return a list of human-readable problems (empty list == valid)."""
    if not isinstance(pack, dict):
        return ["pack must be a JSON object."]
    problems = []
    if pack.get("schema") != SCHEMA:
        problems.append(f"schema must be '{SCHEMA}' (got {pack.get('schema')!r}).")
    family = pack.get("family")
    if not isinstance(family, str) or not family.upper().startswith("STM32"):
        problems.append("family must be a string like 'STM32G4'.")
    for key, validate in _SECTION_VALIDATORS.items():
        if key in pack:
            problems.extend(validate(pack[key]))
    if not any(key in pack for key in _SECTIONS):
        problems.append("pack has no clock/dma/nvic/timer section -- nothing to contribute.")
    return problems


def register_pack(pack, allow_override=False):
    """Validate and register an external pack. Returns a list of problems (empty == ok)."""
    problems = validate_pack(pack)
    if problems:
        return problems
    family = pack["family"]
    if family in _BUILTIN and not allow_override:
        return [f"{family} is a built-in pack; pass allow_override=true to shadow it."]
    _EXTERNAL[family] = _normalize(pack)
    return []


def load_pack(path):
    """Read + validate a pack JSON file. Returns ``(pack_or_None, problems)``."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return None, [f"pack file not found: {path}"]
    except (OSError, json.JSONDecodeError) as exc:
        return None, [f"pack could not be read as JSON: {exc}"]
    return data, validate_pack(data)


def reset_external():
    """Drop all registered external packs (built-ins remain). For tests / reload."""
    _EXTERNAL.clear()


def coverage():
    """Snapshot of what is currently modelled -- for the load_device_pack tool."""
    families = _all_families()
    return {
        "families": families,
        "builtin": sorted(_BUILTIN),
        "external": sorted(_EXTERNAL),
        "dma_families": dma_families(),
        "sections": {f: sorted(k for k in _SECTIONS if _section(f, k)) for f in families},
    }
