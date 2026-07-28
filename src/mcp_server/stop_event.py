"""Parse GDB/MI responses into a single structured stop event.

The autonomous debug loop needs one call that says *why the target stopped* —
breakpoint, watchpoint, signal/fault, finished stepping, exited, or timeout —
together with the symbolized frame. ``parse_stop_event`` turns the list of
pygdbmi records into that structured form so the model never has to poll and
re-assemble async records itself.
"""

TIMEOUT_REASON = "timeout"

# Synthetic notify record appended by halt_execution when the target was found
# already halted, so no -exec-interrupt was sent (a pending interrupt on an
# already-halted target fires as a spurious SIGINT on the next resume).
ALREADY_HALTED_MESSAGE = "target-already-halted"
ALREADY_HALTED_RECORD = {
    "type": "notify",
    "message": ALREADY_HALTED_MESSAGE,
    "payload": None,
    "stream": "stdout",
}


def was_already_halted(records) -> bool:
    return any(record.get("message") == ALREADY_HALTED_MESSAGE for record in records or [])


def _coerce_line(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _parse_frame(frame):
    if not isinstance(frame, dict):
        return None
    return {
        "func": frame.get("func"),
        "file": frame.get("file") or frame.get("fullname"),
        "line": _coerce_line(frame.get("line")),
        "addr": frame.get("addr"),
    }


def parse_stop_event(records) -> dict:
    """Return ``{stopped, reason, signal, breakpoint_id, frame, raw}``.

    ``records`` is the list of dicts returned by pygdbmi. When no ``*stopped``
    async record is present the event is reported as a timeout so the caller can
    distinguish "still running / nothing happened" from a real halt.
    """
    for record in records or []:
        if record.get("message") != "stopped":
            continue
        payload = record.get("payload") or {}
        return {
            "stopped": True,
            "reason": payload.get("reason"),
            "signal": payload.get("signal-name"),
            "breakpoint_id": payload.get("bkptno"),
            "frame": _parse_frame(payload.get("frame")),
            "raw": payload,
        }

    return {
        "stopped": False,
        "reason": TIMEOUT_REASON,
        "signal": None,
        "breakpoint_id": None,
        "frame": None,
        "raw": None,
    }


def synthesized_stop_event(frame=None) -> dict:
    """A stop event for a target found halted without a ``*stopped`` notification.

    Windows pipe polling has been observed to miss the async notification of a
    hit breakpoint (issue #22); the target is verifiably halted, but WHY is
    unknown, so the reason is explicit about that instead of guessing.
    """
    return {
        "stopped": True,
        "reason": "stopped-no-notification",
        "signal": None,
        "breakpoint_id": None,
        "frame": _parse_frame(frame),
        "raw": None,
        "note": "target is halted but GDB's *stopped notification was not observed; "
                "check breakpoint(action=list) hit counts for the cause",
    }
