"""Deterministic timer base-frequency solver (design synthesis, Pillar D Tier 3).

Turns a target update/overflow frequency into concrete TIMx Prescaler (PSC) and
Period (ARR) register values, using the timer input clock (TIMxCLK) derived from
the solved clock tree. Pure integer datasheet math: an exactly achievable target
gives PSC/ARR with zero error; an inexact one gives the closest pair plus the
achieved frequency and ppm error; an unrepresentable target or an unknown timer
bus is surfaced honestly, never guessed.

Timer clocking rule: ``TIMxCLK = PCLKx`` when the APBx prescaler is 1, else
``PCLKx * 2``. Counter update frequency: ``f = TIMxCLK / ((PSC+1) * (ARR+1))``.

Everything is plain dicts so a solution serializes straight through the JSON
envelope. This module imports only pure helpers from ``framework_solver`` (no
cycle: ``framework_solver`` never imports this one).
"""

import math

from . import device_packs
from .framework_solver import _KIND_PARAMS, _count_sources

_PSC_MAX = 65535

_TIMER_ORDER = _KIND_PARAMS["timer"]["order"]


# --- Device data (honest unknowns for unmodelled families) ------------------


def resolve_timer_bus(line, family, timer):
    """Return ``"apb1"``/``"apb2"`` for a timer, or ``None`` when the family is unmodelled."""
    apb2 = device_packs.timer_apb2(family)
    if apb2 is None:
        return None
    name = (timer or "").upper()
    if not name.startswith("TIM"):
        return None
    return "apb2" if name in apb2 else "apb1"


def timer_arr_bits(line, family, timer):
    """Return 32 for known 32-bit timers (TIM2/TIM5), else 16 (safe default)."""
    name = (timer or "").upper()
    return 32 if name in device_packs.timer_bits32(family) else 16


def timer_input_clock(clock_solution, bus):
    """TIMxCLK from a clock solution: PCLKx when the APB prescaler is 1, else x2."""
    if not clock_solution or bus not in ("apb1", "apb2"):
        return None
    if bus == "apb1":
        pclk, presc = clock_solution.get("pclk1_hz"), clock_solution.get("apb1_presc")
    else:
        pclk, presc = clock_solution.get("pclk2_hz"), clock_solution.get("apb2_presc")
    if not isinstance(pclk, int) or not isinstance(presc, int) or presc < 1:
        return None
    return pclk if presc == 1 else pclk * 2


def _arr_max(arr_bits):
    return (1 << arr_bits) - 1


# --- Pure PSC/ARR math ------------------------------------------------------


def _result(timer_clock_hz, target_hz, psc, arr, arr_max, achieved_hz, exact):
    error_hz = achieved_hz - target_hz
    return {
        "feasible": True,
        "psc": psc, "arr": arr,
        "prescaler": psc, "period": arr,
        "timer_clock_hz": timer_clock_hz,
        "target_hz": target_hz,
        "achieved_hz": achieved_hz,
        "error_hz": error_hz,
        "error_ppm": (error_hz / target_hz) * 1e6 if target_hz else 0.0,
        "exact": exact,
        "arr_bits": (arr_max + 1).bit_length() - 1,
        "unresolved": [],
        "notes": [],
    }


def _infeasible(timer_clock_hz, target_hz, arr_max, code, detail):
    return {
        "feasible": False,
        "psc": None, "arr": None, "prescaler": None, "period": None,
        "timer_clock_hz": timer_clock_hz, "target_hz": target_hz,
        "achieved_hz": None, "error_hz": None, "error_ppm": None,
        "exact": False, "arr_bits": (arr_max + 1).bit_length() - 1,
        "unresolved": [{"type": code, "detail": detail}], "notes": [],
    }


def solve_timer_dividers(timer_clock_hz, target_hz, psc_max=_PSC_MAX, arr_max=65535):
    """Solve PSC/ARR so ``TIMxCLK / ((PSC+1)*(ARR+1))`` best matches ``target_hz``.

    Prefers an exact factorization with the smallest PSC (largest ARR = finest
    counter resolution). Falls back to the closest achievable pair, reporting the
    achieved frequency and ppm error. Infeasible targets are surfaced, not forced.
    """
    if not isinstance(timer_clock_hz, int) or timer_clock_hz <= 0:
        return _infeasible(timer_clock_hz, target_hz, arr_max, "invalid_timer_clock",
                           "Timer input clock must be a positive integer (Hz).")
    if not isinstance(target_hz, (int, float)) or isinstance(target_hz, bool) or target_hz <= 0:
        return _infeasible(timer_clock_hz, target_hz, arr_max, "invalid_target",
                           "Target frequency must be a positive number (Hz).")

    if target_hz > timer_clock_hz:
        return _infeasible(timer_clock_hz, target_hz, arr_max, "target_too_fast",
                           f"target {target_hz} Hz exceeds TIMxCLK {timer_clock_hz} Hz "
                           "(a timer cannot update faster than its input clock).")

    n_real = timer_clock_hz / target_hz
    max_product = (psc_max + 1) * (arr_max + 1)
    if n_real > max_product:
        res = _infeasible(timer_clock_hz, target_hz, arr_max, "target_too_slow",
                          f"target {target_hz} Hz needs a total divider of {n_real:.0f}, beyond the "
                          f"{(arr_max + 1).bit_length() - 1}-bit (PSC+1)*(ARR+1) maximum {max_product}.")
        res["unresolved"][0]["suggestion"] = "use a 32-bit timer (TIM2/TIM5) or pass arr_bits=32"
        return res

    notes = []
    if isinstance(target_hz, int) and timer_clock_hz % target_hz == 0:
        n = timer_clock_hz // target_hz
        psc1_min = max(1, -(-n // (arr_max + 1)))  # ceil(n / (arr_max+1))
        for psc1 in range(psc1_min, min(psc_max + 1, n) + 1):
            if n % psc1 == 0:
                arr1 = n // psc1
                achieved = timer_clock_hz / (psc1 * arr1)
                return _result(timer_clock_hz, target_hz, psc1 - 1, arr1 - 1, arr_max, achieved, True)
        notes.append("no exact PSC/ARR factorization within divider bounds; using the closest pair.")

    psc1 = max(1, math.ceil(n_real / (arr_max + 1)))
    arr1 = max(1, min(round(n_real / psc1), arr_max + 1))
    achieved = timer_clock_hz / (psc1 * arr1)
    result = _result(timer_clock_hz, target_hz, psc1 - 1, arr1 - 1, arr_max, achieved, False)
    result["notes"] = notes
    return result


# --- Plan integration -------------------------------------------------------


def _fmt_hz(hz):
    if hz is None:
        return "?"
    if isinstance(hz, float):
        return str(int(hz)) if hz.is_integer() else f"{hz:.3f}"
    return str(hz)


def _set_derived_field(block, field, value, note):
    entry = {"field": field, "value": value, "rendered": str(value),
             "source": "derived", "source_key": None, "mapped": True, "note": note}
    fields = block.setdefault("config_fields", [])
    for i, existing in enumerate(fields):
        if existing["field"] == field:
            fields[i] = entry
            return
    idx = _TIMER_ORDER.index(field) if field in _TIMER_ORDER else len(_TIMER_ORDER)
    pos = len(fields)
    for j, existing in enumerate(fields):
        order = _TIMER_ORDER.index(existing["field"]) if existing["field"] in _TIMER_ORDER else 999
        if order > idx:
            pos = j
            break
    fields.insert(pos, entry)


def _inject_timer_fields(block, sol):
    tail = "" if sol["exact"] else f" ({sol['error_ppm']:+.1f} ppm)"
    note = (f"target {_fmt_hz(sol['target_hz'])} Hz @ TIMxCLK {sol['timer_clock_hz']} Hz "
            f"-> {_fmt_hz(sol['achieved_hz'])} Hz{tail}")
    _set_derived_field(block, "Prescaler", sol["psc"], note)
    _set_derived_field(block, "Period", sol["arr"], note)
    block["param_todos"] = [t for t in block.get("param_todos", []) if t["field"] not in ("Prescaler", "Period")]
    block["config_sources"] = _count_sources(block["config_fields"])
    block["has_config"] = bool(block["config_fields"])


def _unsolved(name, code, detail, bus, target):
    return {"timer": name, "feasible": False, "bus": bus, "target_hz": target,
            "psc": None, "arr": None, "unresolved": [{"type": code, "detail": detail}], "notes": []}


def _clear_solved_timer_unresolved(plan, results):
    solved = {r["timer"] for r in results if r.get("feasible")}
    if not solved:
        return
    kept = [u for u in plan.get("unresolved", [])
            if not (u.get("type") == "param_unresolved" and u.get("peripheral") in solved
                    and u.get("field") in ("Prescaler", "Period"))]
    plan["unresolved"] = kept
    stats = plan.get("stats")
    if isinstance(stats, dict):
        stats["unresolved_count"] = len(kept)


def solve_timers_in_plan(plan, only=None, target_overrides=None, timer_clock_hz=None,
                         bus_override=None, arr_bits_override=None):
    """Solve every TIM block carrying a recorded target against ``plan['clock_config']``.

    Injects the solved PSC/ARR into each block as ``derived`` fields, clears the
    matching ``param_unresolved`` entries, and returns a per-timer report. Timers
    that cannot be solved (no clock solution, unknown bus, infeasible target) are
    reported with a typed reason and left untouched.
    """
    clock = plan.get("clock_config")
    mcu = plan.get("mcu") or {}
    line, family = mcu.get("line"), mcu.get("family")
    overrides = target_overrides or {}
    results = []

    for block in plan.get("peripherals", []):
        if block.get("kind") != "timer":
            continue
        name = block["name"]
        if only and name != only:
            continue
        target = overrides.get(name, block.get("timer_target_hz"))
        if target is None:
            continue
        bus = bus_override or resolve_timer_bus(line, family, name)
        tclk = timer_clock_hz
        if tclk is None:
            if not clock:
                results.append(_unsolved(name, "no_clock_solution",
                    "No clock solution on the plan; run solve_clock_tree first or pass timer_clock_hz.",
                    bus, target))
                continue
            if bus is None:
                results.append(_unsolved(name, "bus_unknown",
                    f"APB bus for {name} is unknown for this device; pass bus='apb1'|'apb2' or timer_clock_hz.",
                    bus, target))
                continue
            tclk = timer_input_clock(clock, bus)
            if tclk is None:
                results.append(_unsolved(name, "no_clock_solution",
                    "Clock solution lacks PCLK/prescaler data for this bus; pass timer_clock_hz.", bus, target))
                continue
        arr_bits = arr_bits_override or timer_arr_bits(line, family, name)
        sol = solve_timer_dividers(tclk, target, arr_max=_arr_max(arr_bits))
        sol["timer"] = name
        sol["bus"] = bus
        sol["arr_bits"] = arr_bits
        if sol["feasible"]:
            _inject_timer_fields(block, sol)
        results.append(sol)

    _clear_solved_timer_unresolved(plan, results)
    solved = [r for r in results if r.get("feasible")]
    return {
        "results": results,
        "solved_count": len(solved),
        "timer_count": len([b for b in plan.get("peripherals", []) if b.get("kind") == "timer"]),
    }
