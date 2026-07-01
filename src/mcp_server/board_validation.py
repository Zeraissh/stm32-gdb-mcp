"""Validate a BoardDescription against structural rules and, optionally, an MCU
pin-capability (alternate-function) database.

Two layers of checks:

* **Structural** (always run, need no external data, 100% correct): a package pin
  wired to more than one net (a short / netlist error), a peripheral *signal*
  routed to more than one pin (only one routing is legal), a port pin driven by
  more than one net, and missing critical nets (power, ground, debug, reset).
* **Alternate-function legality** (only when a ``PinCapabilityDB`` is supplied):
  each net's inferred function must be a legal AF for that port pin. Unknown pins
  degrade to an "unverified" count — a missing DB entry NEVER produces a false
  conflict.

The capability DB is intentionally pluggable and user-supplied (derive it from the
STM32CubeMX MCU database). This module ships the adapter and schema, not an
authoritative table, so validation is never wrong for lack of trustworthy data.

DB JSON shape::

    {"<mcu line or family>": {"<port pin>": [{"peripheral": "...", "signal": "...", "af": 7}, ...]}}

The optional ``"af"`` field (an integer alternate-function number) is not used by
legality checks; it lets the same CubeMX-derived DB double as the authoritative
source for GPIO alternate-function *numbers* during framework synthesis (see
``af_map``). Entries without ``"af"`` still validate normally.
"""

import json
from collections import defaultdict


class PinCapabilityDB:
    """Answers whether a port pin supports a ``{peripheral, signal}`` alternate function."""

    def __init__(self, data: dict | None):
        self._data = data or {}

    def _pins_for(self, line: str | None, family: str | None):
        if line and line in self._data:
            return self._data[line]
        if family and family in self._data:
            return self._data[family]
        return None

    def supports(self, line, family, port_pin, peripheral, signal) -> bool | None:
        """Return True/False when known, or ``None`` when the pin/line is not in the DB."""
        pins = self._pins_for(line, family)
        if pins is None or not port_pin or port_pin not in pins:
            return None
        for entry in pins[port_pin]:
            if entry.get("peripheral") == peripheral and entry.get("signal") == signal:
                return True
        return False

    def af_map(self) -> dict:
        """Project the DB into an ``af_map`` for framework synthesis.

        Returns ``{line_or_family: {port_pin: {"<peripheral>_<signal>": af}}}`` built
        only from entries that carry an integer ``"af"``. Entries without an ``af``
        are omitted, so a pin whose number is unknown stays unresolved rather than
        being assigned a fabricated value.
        """
        result: dict = {}
        for scope, pins in self._data.items():
            if not isinstance(pins, dict):
                continue
            pin_map: dict = {}
            for port_pin, entries in pins.items():
                sig_map: dict = {}
                for entry in entries or []:
                    if not isinstance(entry, dict):
                        continue
                    af = entry.get("af")
                    peripheral, signal = entry.get("peripheral"), entry.get("signal")
                    if isinstance(af, int) and not isinstance(af, bool) and peripheral and signal:
                        sig_map[f"{peripheral}_{signal}"] = af
                if sig_map:
                    pin_map[port_pin] = sig_map
            if pin_map:
                result[scope] = pin_map
        return result


def load_capability_db(path: str) -> PinCapabilityDB:
    with open(path, encoding="utf-8") as handle:
        return PinCapabilityDB(json.load(handle))


def _mcu_pins(board: dict) -> list[dict]:
    mcu = board.get("mcu")
    return mcu.get("pins", []) if mcu else []


def _detect_pin_double_assignment(pins: list[dict]) -> list[dict]:
    nets_by_pin: dict = defaultdict(list)
    for pin in pins:
        package_pin, net = pin.get("package_pin"), pin.get("net")
        if package_pin is None:
            continue
        if net not in nets_by_pin[package_pin]:
            nets_by_pin[package_pin].append(net)
    conflicts = []
    for package_pin, nets in nets_by_pin.items():
        if len(nets) > 1:
            conflicts.append({
                "type": "pin_double_assignment",
                "severity": "error",
                "package_pin": package_pin,
                "nets": nets,
                "detail": f"Package pin {package_pin} is wired to {len(nets)} nets: {', '.join(map(str, nets))}.",
            })
    return conflicts


def _detect_port_pin_double_assignment(pins: list[dict]) -> list[dict]:
    nets_by_port: dict = defaultdict(list)
    for pin in pins:
        port_pin, net = pin.get("port_pin"), pin.get("net")
        if not port_pin:
            continue
        if net not in nets_by_port[port_pin]:
            nets_by_port[port_pin].append(net)
    conflicts = []
    for port_pin, nets in nets_by_port.items():
        if len(nets) > 1:
            conflicts.append({
                "type": "port_pin_double_assignment",
                "severity": "error",
                "port_pin": port_pin,
                "nets": nets,
                "detail": f"Port pin {port_pin} is driven by {len(nets)} nets: {', '.join(map(str, nets))}.",
            })
    return conflicts


def _detect_duplicate_signal(pins: list[dict]) -> list[dict]:
    pins_by_signal: dict = defaultdict(list)
    for pin in pins:
        function = pin.get("function")
        if not function:
            continue
        key = (function.get("peripheral"), function.get("signal"))
        package_pin = pin.get("package_pin")
        if package_pin not in pins_by_signal[key]:
            pins_by_signal[key].append(package_pin)
    conflicts = []
    for (peripheral, signal), package_pins in pins_by_signal.items():
        if len(package_pins) > 1:
            conflicts.append({
                "type": "duplicate_peripheral_signal",
                "severity": "error",
                "peripheral": peripheral,
                "signal": signal,
                "package_pins": package_pins,
                "detail": f"{peripheral}_{signal} is routed to multiple pins: {', '.join(map(str, package_pins))}.",
            })
    return conflicts


def _detect_illegal_af(pins: list[dict], db: PinCapabilityDB, line, family) -> tuple[list[dict], int]:
    conflicts, unverified = [], 0
    for pin in pins:
        function, port_pin = pin.get("function"), pin.get("port_pin")
        if not function or not port_pin:
            continue
        peripheral, signal = function.get("peripheral"), function.get("signal")
        verdict = db.supports(line, family, port_pin, peripheral, signal)
        if verdict is False:
            conflicts.append({
                "type": "illegal_af",
                "severity": "error",
                "port_pin": port_pin,
                "package_pin": pin.get("package_pin"),
                "peripheral": peripheral,
                "signal": signal,
                "net": pin.get("net"),
                "detail": f"{port_pin} does not support {peripheral}_{signal} on {line or family}.",
            })
        elif verdict is None:
            unverified += 1
    return conflicts, unverified


def _detect_missing_critical(board: dict, pins: list[dict]) -> list[dict]:
    warnings = []
    power = board.get("power_nets") or {}
    if not power.get("power"):
        warnings.append({"type": "no_power_net", "detail": "No power net detected (VDD/VCC/+3V3/...)."})
    if not power.get("ground"):
        warnings.append({"type": "no_ground_net", "detail": "No ground net detected (GND/VSS)."})

    functions = {(p["function"]["peripheral"], p["function"]["signal"]) for p in pins if p.get("function")}
    peripherals = {peripheral for peripheral, _ in functions}
    if not ({"SWD", "JTAG"} & peripherals):
        warnings.append({"type": "no_debug_pins", "detail": "No SWD/JTAG debug pins found; on-chip debug may be unavailable."})
    if ("SYS", "NRST") not in functions:
        warnings.append({"type": "no_reset_pin", "detail": "No NRST reset net found."})
    return warnings


def validate_board(board: dict, capability_db: PinCapabilityDB | None = None) -> dict:
    """Validate a BoardDescription; return a structured conflict/warning report."""
    mcu = board.get("mcu")
    pins = _mcu_pins(board)

    conflicts = (
        _detect_pin_double_assignment(pins)
        + _detect_port_pin_double_assignment(pins)
        + _detect_duplicate_signal(pins)
    )

    unverified, af_checked = 0, False
    if capability_db is not None and mcu:
        af_conflicts, unverified = _detect_illegal_af(pins, capability_db, mcu.get("line"), mcu.get("family"))
        conflicts += af_conflicts
        af_checked = True

    warnings = _detect_missing_critical(board, pins)
    if not mcu:
        warnings.append({"type": "no_mcu", "detail": "No MCU in the board description; import a netlist with an MCU first."})

    unassigned = [
        {"package_pin": p.get("package_pin"), "port_pin": p.get("port_pin"), "net": p.get("net")}
        for p in pins
        if not p.get("function")
    ]

    errors = [c for c in conflicts if c["severity"] == "error"]
    return {
        "ok": not errors,
        "conflicts": conflicts,
        "warnings": warnings,
        "unassigned_pins": unassigned,
        "af_checked": af_checked,
        "stats": {
            "conflict_count": len(conflicts),
            "error_count": len(errors),
            "warning_count": len(warnings),
            "unassigned_count": len(unassigned),
            "unverified_af_pins": unverified,
        },
    }
