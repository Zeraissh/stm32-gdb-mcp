"""Decode raw GDB/MI records into clean, low-overhead structures.

Design law #1 of Phase 2: a probe result must reach the agent decoded, concise,
and unambiguous — never as a raw GDB/MI dump it has to re-parse. These pure
functions turn pygdbmi records into plain dicts/lists so tool handlers can return
``data`` the model reads directly, with the raw transcript kept opt-in.
"""


def _find_payload_value(records, key):
    for record in records or []:
        payload = record.get("payload")
        if isinstance(payload, dict) and key in payload:
            return payload[key]
    return None


def _coerce_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def decode_registers(names_records, values_records) -> dict:
    """Map register names to hex values from the two MI register queries."""
    names = _find_payload_value(names_records, "register-names") or []
    values = _find_payload_value(values_records, "register-values") or []

    decoded = {}
    for entry in values:
        number = entry.get("number")
        try:
            index = int(number)
        except (TypeError, ValueError):
            continue
        name = names[index] if index < len(names) and names[index] else f"r{index}"
        decoded[name] = entry.get("value")
    return decoded


def registers_summary(registers: dict) -> str:
    """One-line summary highlighting the registers that matter most at a glance."""
    parts = []
    for key in ("pc", "lr", "sp", "msp", "psp", "xpsr"):
        if registers.get(key) is not None:
            parts.append(f"{key}={registers[key]}")
    return " ".join(parts)


def decode_backtrace(records) -> list:
    """Return a clean list of frames with integer levels/lines."""
    stack = _find_payload_value(records, "stack") or []
    frames = []
    for frame in stack:
        frames.append({
            "level": _coerce_int(frame.get("level")),
            "func": frame.get("func"),
            "file": frame.get("file") or frame.get("fullname"),
            "line": _coerce_int(frame.get("line")),
            "addr": frame.get("addr"),
        })
    return frames


def decode_breakpoints(records) -> list:
    """Return a clean breakpoint list with hit counts.

    ``hit_count == 0`` is the key signal that a breakpoint's code path was never
    reached — i.e. a run_and_wait timeout means the precondition to get there was
    not satisfied, not that you should retry.
    """
    table = _find_payload_value(records, "BreakpointTable")
    if not isinstance(table, dict):
        return []
    out = []
    for item in table.get("body") or []:
        bp = item.get("bkpt") if isinstance(item, dict) and "bkpt" in item else item
        if not isinstance(bp, dict):
            continue
        enabled = bp.get("enabled")
        out.append({
            "number": bp.get("number"),
            "location": bp.get("original-location") or bp.get("func"),
            "func": bp.get("func"),
            "file": bp.get("file"),
            "line": _coerce_int(bp.get("line")),
            "addr": bp.get("addr"),
            "enabled": (enabled == "y") if enabled is not None else None,
            "condition": bp.get("cond"),
            "hit_count": _coerce_int(bp.get("times")),
        })
    return out


def decode_variables(records) -> dict:
    """Return a ``{name: value}`` map of frame locals/arguments."""
    variables = _find_payload_value(records, "variables") or []
    return {entry.get("name"): entry.get("value") for entry in variables if entry.get("name")}


def decode_evaluated_value(records) -> str | None:
    """Extract the first expression value from ``-data-evaluate-expression`` records."""
    for record in records or []:
        if record.get("message") == "error":
            continue
        payload = record.get("payload")
        if isinstance(payload, dict) and payload.get("value") is not None:
            return payload["value"]
    return None


def decode_memory_bytes(records) -> str | None:
    """Extract hex bytes from ``-data-read-memory-bytes`` records."""
    for record in records or []:
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        memory = payload.get("memory")
        if isinstance(memory, list) and memory:
            first = memory[0]
            if isinstance(first, dict) and isinstance(first.get("contents"), str):
                return first["contents"].strip()
        contents = payload.get("contents")
        if isinstance(contents, str) and contents.strip():
            return contents.strip()
    return None
