"""Derive an honest ok/error verdict from raw GDB/MI records.

GDB reports many hard failures outside the ``^error`` result record: flash and
monitor errors arrive as log/console/target stream text while the surrounding
MI command still "completes". Tools that blindly wrapped those records in
ok:true taught agents to trust a flash that never happened (issue #21). These
helpers centralize the verdict so the client layer can raise instead.
"""

from __future__ import annotations


class GdbCommandError(RuntimeError):
    """A GDB/MI command whose records show the operation actually failed."""


# Failures GDB emits as stream text rather than an ^error record. Matched
# case-insensitively as substrings. Keep this list conservative: a false
# "error" verdict on a healthy operation is worse than a missed one.
STREAM_ERROR_MARKERS = (
    "no such file or directory",
    "no symbol table is loaded",
    "error erasing flash",
    "error writing to flash",
    "error finishing flash operation",
    "failed to erase memory",
    "load failed",
)


def find_mi_error(records) -> str | None:
    """Return the failure message shown by ``records``, or None if they look ok."""
    for record in records or []:
        if record.get("type") == "result" and record.get("message") == "error":
            payload = record.get("payload")
            msg = payload.get("msg") if isinstance(payload, dict) else None
            return str(msg) if msg else "GDB reported an error"
    for record in records or []:
        payload = record.get("payload")
        if isinstance(payload, str):
            lowered = payload.lower()
            if any(marker in lowered for marker in STREAM_ERROR_MARKERS):
                return payload.strip()
    return None


def has_terminal_result(records) -> bool:
    """True when GDB sent a terminal MI result record (``^done``/``^error``/...)."""
    return any(record.get("type") == "result" for record in records or [])


def ensure_ok(records, operation: str, require_result: bool = False):
    """Raise ``GdbCommandError`` when ``records`` show ``operation`` failed.

    ``require_result=True`` additionally demands a terminal MI result record —
    used for long-running transfers (flash download) where returning early on a
    timeout must not read as success.
    """
    error = find_mi_error(records)
    if error:
        raise GdbCommandError(f"{operation} failed: {error}")
    if require_result and not has_terminal_result(records):
        raise GdbCommandError(
            f"{operation} did not report completion (no terminal MI result record) — "
            "treat it as NOT done"
        )
    return records
