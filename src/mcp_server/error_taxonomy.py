"""Classify raw GDB/probe errors into structured, actionable outcomes.

Phase 2 priority #2 (reliability) + design law #1 (low comprehension overhead):
instead of bubbling up a generic "tool_execution_error", map the failure to a
stable code, whether it is retryable, and the next action that resolves it. This
turns the running-core footgun we hit on hardware (a bare GDB timeout) into a
clear "halt the target first" signal.
"""

# Ordered (substring, classification) rules; first match wins.
_RULES = [
    (("did not get response from gdb", "timed out", "timeout"), {
        "code": "target_unresponsive",
        "retryable": True,
        "suggested_next_actions": ["halt_execution", "check_session_health"],
        "hint": "The target may be running (reads require a halted core) or the probe is wedged.",
    }),
    (("gdb is not running", "no gdb", "not connected"), {
        "code": "no_session",
        "retryable": False,
        "suggested_next_actions": ["start_debug_session"],
        "hint": "No active debug session.",
    }),
    (("remote communication error", "remote connection closed", "connection reset"), {
        "code": "connection_lost",
        "retryable": True,
        "suggested_next_actions": ["check_session_health", "start_debug_session"],
        "hint": "The link to the GDB server dropped.",
    }),
    (("no symbol", "no symbol table"), {
        "code": "no_symbols",
        "retryable": False,
        "suggested_next_actions": ["flash_firmware", "inspect_project"],
        "hint": "Symbols are not loaded; flash or load an ELF first.",
    }),
    (("cannot access memory", "memory access"), {
        "code": "memory_access",
        "retryable": False,
        "suggested_next_actions": ["halt_execution", "read_core_registers"],
        "hint": "Memory was unreadable — often the core is running or the address is invalid.",
    }),
]

_FALLBACK = {
    "code": "tool_execution_error",
    "retryable": False,
    "suggested_next_actions": ["capture_debug_snapshot"],
    "hint": None,
}


def classify_error(message: str) -> dict:
    lowered = (message or "").lower()
    for needles, classification in _RULES:
        if any(needle in lowered for needle in needles):
            return dict(classification)
    return dict(_FALLBACK)
