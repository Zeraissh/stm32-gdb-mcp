"""Layered, evidence-first MCU pin resolution.

Given a BoardDescription and a PinCapabilityDB, enrich the MCU pin list using
physical evidence before name heuristics:

1. ``port_pin`` from the package-pin position map (ST pin data) — physical
   truth, independent of net naming. Tagged ``port_pin_source="package_position"``.
2. Fixed-function pins (NRST/BOOT0) get their ``function`` from position alone.
3. A name-inferred ``function`` corroborated by the DB's AF candidates is tagged
   ``"name+db"``; without corroboration it stays ``"name_only"``.
4. When no function is known, the net label is used as a HINT against the pin's
   legal candidates: exactly one candidate whose signal token suffix-matches the
   label wins (``"db_hint"``); zero or several matches leave ``function=None``.

SYS debug candidates (JTMS-SWDIO etc.) are normalized to SWD/JTAG functions so
``validate_board``'s debug-pin check sees them. Everything absent stays
``None`` — resolution never fabricates.
"""

import copy
import re

# SYS candidate signal -> normalized debug function.
_DEBUG_SIGNALS = {
    "JTMS-SWDIO": ("SWD", "SWDIO"),
    "JTCK-SWCLK": ("SWD", "SWCLK"),
    "JTDO-TRACESWO": ("SWD", "SWO"),
    "JTDI": ("JTAG", "JTDI"),
    "JTRST": ("JTAG", "NJTRST"),
    "NJTRST": ("JTAG", "NJTRST"),
}

# Fixed-function position pins (Type != I/O in ST's data, so no AF candidates).
_FIXED_FUNCTIONS = {
    "NRST": ("SYS", "NRST"),
    "BOOT0": ("SYS", "BOOT0"),
}


def _normalize_label(net_name: str) -> str:
    """Uppercase alnum-only label: ``SW_DIO`` -> ``SWDIO``."""
    return re.sub(r"[^A-Za-z0-9]", "", net_name or "").upper()


def _signal_token(signal: str) -> str:
    """Last ``-``/``_`` token of a candidate signal: ``JTCK-SWCLK`` -> ``SWCLK``."""
    return re.split(r"[-_]", signal)[-1].upper()


def _hint_matches(candidates: list[dict], net_name: str) -> list[dict]:
    """Candidates whose signal token suffix-matches the normalized net label."""
    label = _normalize_label(net_name)
    if not label:
        return []
    return [c for c in candidates
            if c.get("signal") and label.endswith(_signal_token(c["signal"]))]


def _debug_function(signal: str) -> dict | None:
    mapped = _DEBUG_SIGNALS.get(signal)
    return {"peripheral": mapped[0], "signal": mapped[1]} if mapped else None


def _resolve_pin(pin: dict, position_map: dict | None, candidates_of, notes: list) -> None:
    package_pin = pin.get("package_pin")
    physical = position_map.get(str(package_pin)) if position_map else None
    if physical:
        if pin.get("port_pin") is None:
            pin["port_pin"] = physical
            pin["port_pin_source"] = "package_position"
        elif pin["port_pin"] != physical:
            notes.append(
                f"pin {package_pin}: netlist says port_pin {pin['port_pin']} but the package "
                f"position maps to {physical}; keeping the netlist value.")

    port_pin = pin.get("port_pin")
    function = pin.get("function")

    if function is None and port_pin in _FIXED_FUNCTIONS:
        peripheral, signal = _FIXED_FUNCTIONS[port_pin]
        pin["function"] = {"peripheral": peripheral, "signal": signal}
        pin["function_source"] = "package_position"
        return

    candidates = candidates_of(port_pin) if port_pin else None
    if function is not None:
        # An already-tagged function (db_hint/package_position from an earlier
        # pass) keeps its provenance — resolve_board must be idempotent.
        if "function_source" in pin:
            return
        pin["function_source"] = "name_only"
        if candidates:
            pair = (function.get("peripheral"), function.get("signal"))
            if any((c.get("peripheral"), c.get("signal")) == pair for c in candidates):
                pin["function_source"] = "name+db"
        return

    if not candidates:
        return
    matches = _hint_matches(candidates, pin.get("net") or "")
    if len(matches) != 1:
        return
    match = matches[0]
    debug = _debug_function(match["signal"]) if match.get("peripheral") == "SYS" else None
    pin["function"] = debug or {"peripheral": match["peripheral"], "signal": match["signal"]}
    pin["function_source"] = "db_hint"


def resolve_board(board: dict, db) -> dict:
    """Return an enriched copy of *board* with physically-resolved MCU pins."""
    out = copy.deepcopy(board)
    notes: list[str] = []
    mcu = out.get("mcu")
    if mcu and db is not None:
        position_map = db.position_map(mcu.get("line"), mcu.get("family"),
                                       mcu.get("part_normalized"))
        candidates_of = lambda port_pin: db.candidates(  # noqa: E731
            mcu.get("line"), mcu.get("family"), port_pin)
        for pin in mcu.get("pins", []):
            _resolve_pin(pin, position_map, candidates_of, notes)
    out["resolution_notes"] = notes
    return out
