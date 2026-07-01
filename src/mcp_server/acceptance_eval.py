"""Evaluate an AcceptanceSpec against live silicon state and return a deterministic verdict.

The evaluator is the *judge* of the spec-to-silicon closed loop. It reads concrete, observable
values through a small **reader protocol** and compares them exactly — it never guesses. If a
target cannot be read, that check is reported as ``error`` (not ``pass``/``fail``) with the
exception message, so an unreadable target can never silently pass.

Reader protocol (duck-typed — the evaluator only calls these):

* ``read_u32(address) -> int``           — a 32-bit memory word
* ``read_variable(name) -> int``         — a C global / expression, as an integer
* ``read_register(name) -> int``         — a core / convenience register (pc, sp, xpsr, ...)
* ``read_fault_registers() -> dict``     — Cortex-M SCB fault registers
* ``symbolize(address) -> str``          — best-effort function name for an address

``GdbAcceptanceReader`` adapts a live ``gdb_client`` to this protocol; tests inject a fake.
"""

from .fault_analysis import diagnose_fault_registers


def _coerce_int(value) -> int:
    if isinstance(value, bool):  # avoid True == 1 / False == 0 foot-guns in specs
        raise ValueError("boolean is not a valid integer operand")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value, 0)
    raise ValueError(f"cannot interpret {value!r} as an integer")


def _compare(actual: int, expected: int, op: str) -> bool:
    if op == "eq":
        return actual == expected
    if op == "ne":
        return actual != expected
    if op == "lt":
        return actual < expected
    if op == "le":
        return actual <= expected
    if op == "gt":
        return actual > expected
    if op == "ge":
        return actual >= expected
    if op == "bits_set":
        return (actual & expected) == expected
    if op == "bits_clear":
        return (actual & expected) == 0
    raise ValueError(f"unknown op {op!r}")


def _hex(value: int) -> str:
    return f"0x{value & 0xFFFFFFFF:08x}"


def _status(passed: bool) -> str:
    return "pass" if passed else "fail"


def _eval_memory_u32(check: dict, reader) -> tuple:
    address = check["address"]
    raw = reader.read_u32(address)
    mask = _coerce_int(check["mask"]) if check.get("mask") is not None else None
    actual = raw & mask if mask is not None else raw
    expected = _coerce_int(check["expect"])
    op = check["op"]
    masked = f" & {_hex(mask)}" if mask is not None else ""
    detail = f"[{address}]{masked} = {_hex(actual)} {op} {_hex(expected)}"
    return _status(_compare(actual, expected, op)), _hex(expected), _hex(actual), detail


def _eval_variable(check: dict, reader) -> tuple:
    name = check["name"]
    actual = reader.read_variable(name)
    expected = _coerce_int(check["expect"])
    op = check["op"]
    detail = f"{name} = {actual} {op} {expected}"
    return _status(_compare(actual, expected, op)), expected, actual, detail


def _eval_core_register(check: dict, reader) -> tuple:
    register = check["register"]
    raw = reader.read_register(register)
    mask = _coerce_int(check["mask"]) if check.get("mask") is not None else None
    actual = raw & mask if mask is not None else raw
    expected = _coerce_int(check["expect"])
    op = check["op"]
    masked = f" & {_hex(mask)}" if mask is not None else ""
    detail = f"${register}{masked} = {_hex(actual)} {op} {_hex(expected)}"
    return _status(_compare(actual, expected, op)), _hex(expected), _hex(actual), detail


def _eval_no_fault(check: dict, reader) -> tuple:
    registers = reader.read_fault_registers()
    diagnosis = diagnose_fault_registers(registers)
    classes = diagnosis.get("fault_classes") or []
    actual = ", ".join(classes) if classes else "none"
    return _status(not classes), "no active fault", actual, diagnosis.get("summary", "")


def _eval_stopped_at(check: dict, reader) -> tuple:
    symbol = check["symbol"]
    pc = reader.read_register("pc")
    resolved = reader.symbolize(pc)
    actual = resolved or _hex(pc)
    detail = f"pc={_hex(pc)} -> {actual!r}, expected {symbol!r}"
    return _status(resolved == symbol), symbol, actual, detail


_EVALUATORS = {
    "memory_u32": _eval_memory_u32,
    "variable": _eval_variable,
    "core_register": _eval_core_register,
    "no_fault": _eval_no_fault,
    "stopped_at": _eval_stopped_at,
}


def _expected_hint(check: dict):
    if check["kind"] == "no_fault":
        return "no active fault"
    if check["kind"] == "stopped_at":
        return check.get("symbol")
    return check.get("expect")


def evaluate_acceptance(spec: dict, reader) -> dict:
    """Evaluate every check in a (normalized) *spec* against *reader*; return a verdict report."""
    results = []
    passed = failed = errored = 0
    for check in spec.get("checks", []):
        evaluator = _EVALUATORS[check["kind"]]
        try:
            status, expected, actual, detail = evaluator(check, reader)
        except Exception as exc:  # a single unreadable target must not fail the whole run
            status, expected, actual, detail = "error", _expected_hint(check), None, str(exc)

        if status == "pass":
            passed += 1
        elif status == "fail":
            failed += 1
        else:
            errored += 1

        results.append({
            "id": check["id"],
            "kind": check["kind"],
            "status": status,
            "description": check.get("description", ""),
            "expected": expected,
            "actual": actual,
            "detail": detail,
        })

    return {
        "ok": failed == 0 and errored == 0,
        "results": results,
        "stats": {"total": len(results), "passed": passed, "failed": failed, "errored": errored},
    }


class GdbAcceptanceReader:
    """Adapt a live ``gdb_client`` to the acceptance reader protocol."""

    def __init__(self, gdb_client):
        self._gdb = gdb_client

    def read_u32(self, address) -> int:
        return self._gdb.read_word(address) & 0xFFFFFFFF

    def read_register(self, name: str) -> int:
        expr = name if name.startswith("$") else f"${name}"
        return self._gdb.read_register_value(expr) & 0xFFFFFFFF

    def read_fault_registers(self) -> dict:
        return self._gdb.read_fault_registers()

    def symbolize(self, address: int) -> str:
        return self._gdb.symbolize_pc(address)

    def read_variable(self, name: str) -> int:
        response = self._gdb.read_variable(name)
        for record in response:
            payload = record.get("payload")
            if isinstance(payload, dict) and payload.get("value") is not None:
                token = str(payload["value"]).split()[0].strip()
                return int(token, 0)
        raise ValueError(f"could not read an integer value for {name!r}")
