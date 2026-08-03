"""Decode raw GDB/MI records into clean, low-overhead structures.

Design law #1 of Phase 2: a probe result must reach the agent decoded, concise,
and unambiguous — never as a raw GDB/MI dump it has to re-parse. These pure
functions turn pygdbmi records into plain dicts/lists so tool handlers can return
``data`` the model reads directly, with the raw transcript kept opt-in.
"""

import re


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


def implausible_register_set(registers: dict) -> str | None:
    """Return why ``registers`` cannot have come from a halted Cortex-M, or None.

    A debug tool that answers "pc=0x0 lr=0x0 sp=0x0 xpsr=0x0" with ok:true is
    worse than one that errors: the agent that hit this spent two analysis rounds
    on a firmware fault that did not exist, steered by a backtrace synthesised
    from those zeros (issue #37). These are architectural invariants, not
    heuristics — on a halted Cortex-M the Thumb bit (xPSR bit 24) is always set,
    because a core with T=0 would have taken a UsageFault rather than be sitting
    there answering register reads.
    """
    if not registers:
        return "core register read returned no registers at all"

    # GDB names registers from -data-list-register-names, and the casing is not
    # something to bet an invariant on ("xpsr" on arm-none-eabi, "xPSR" elsewhere).
    lowered = {str(name).lower(): value for name, value in registers.items()}

    def _as_int(name):
        raw = lowered.get(name)
        if raw is None:
            return None
        try:
            return int(str(raw), 0)
        except (TypeError, ValueError):
            return None

    xpsr = _as_int("xpsr")
    if xpsr is not None and not xpsr & (1 << 24):
        return (f"core register read is implausible: xPSR={lowered.get('xpsr')} has the Thumb bit "
                "(bit 24) clear, which cannot happen on a halted Cortex-M")

    # pc=0 alone is not proof: -data-list-register-values reads the SELECTED
    # frame, and a failed unwind legitimately reports pc=0 for an outer frame.
    # Paired with sp=0 it cannot be a real core — no Cortex-M runs on a null stack.
    pc, sp = _as_int("pc"), _as_int("sp")
    if pc == 0 and (sp is None or sp == 0):
        return ("core register read is implausible: pc=0x0 with no stack pointer, which cannot be "
                "the halt state of a running Cortex-M image")
    return None


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
        if isinstance(payload, dict):
            value = payload.get("value")
            if value is not None and (not isinstance(value, str) or value.strip()):
                return value
    return None


def decode_console_text(records) -> str:
    """Join GDB's console stream records into the text a human would have seen.

    The answer to every ``info ...``/``list``/``ptype``/``x/i`` command arrives as
    console records; ``log`` records are only the command echo. Handlers that
    returned just ``{"message": "Source listed"}`` were throwing this away and
    asserting success over an empty payload (issue #40).
    """
    parts = [
        record["payload"]
        for record in records or []
        if record.get("type") == "console" and isinstance(record.get("payload"), str)
    ]
    return "".join(parts).strip()


def decode_symbol_resolution(records) -> dict:
    """Parse ``info line`` + ``info symbol`` output into a structured resolution.

    Handles the shapes GDB actually emits:

    * ``Line 412 of "boot.c" starts at address 0x8000c74 <Boot_ValidateStaging+148> ...``
    * ``Boot_ValidateStaging + 148 in section .text`` (optionally ``of /path/fw.elf``)
    * ``No line number information available for address 0x8000c74 <Boot_ValidateStaging+148>``
      — no file/line, but the symbol is still in there and is worth keeping
    * ``No symbol matches 0x08000c74.`` — genuinely unresolvable, and must be
      reported as such rather than as a bare "Address resolved".
    """
    text = decode_console_text(records)
    out: dict = {
        "resolved": False,
        "symbol": None,
        "offset": None,
        "section": None,
        "file": None,
        "line": None,
        "text": text or None,
    }
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        line_match = re.match(r'Line (\d+) of "([^"]+)"', line)
        if line_match:
            out["line"] = int(line_match.group(1))
            out["file"] = line_match.group(2)

        # "Boot_ValidateStaging + 148 in section .text [of /path/fw.elf]" — the
        # authoritative answer, so it overwrites anything scraped from info line.
        # Non-greedy .+? rather than \S+?: a C++ symbol has spaces in it
        # ("Foo::bar(int, int)"), and dropping it would report resolved=false.
        section_match = re.match(r"^(.+?)(?:\s+\+\s+(\d+))?\s+in section (\S+)", line)
        if section_match:
            out["symbol"] = section_match.group(1)
            out["offset"] = int(section_match.group(2) or 0)
            out["section"] = section_match.group(3)
            continue

        if out["symbol"] is None:
            # "... <Boot_ValidateStaging+148>" appears in both info line forms,
            # including the one that has no line table to offer.
            angle_match = re.search(r"<([^+>\s]+)(?:\+(\d+))?>", line)
            if angle_match:
                out["symbol"] = angle_match.group(1)
                out["offset"] = int(angle_match.group(2) or 0)

    out["resolved"] = bool(out["symbol"] or out["file"])
    return out


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
                contents = first["contents"].strip()
                if contents:
                    return contents
        contents = payload.get("contents")
        if isinstance(contents, str) and contents.strip():
            return contents.strip()
    return None
