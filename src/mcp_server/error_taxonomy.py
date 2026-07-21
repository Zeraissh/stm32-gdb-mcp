"""Classify raw GDB/probe errors into structured, actionable outcomes.

Phase 2 priority #2 (reliability) + design law #1 (low comprehension overhead):
instead of bubbling up a generic "tool_execution_error", map the failure to a
stable code, whether it is retryable, and the next action that resolves it. This
turns the running-core footgun we hit on hardware (a bare GDB timeout) into a
clear "halt the target first" signal.
"""

# Ordered (substring, classification) rules; first match wins.
_RULES = [
    (("[winerror 2]", "no such file or directory", "not recognized as an internal or external command",
      "executable not found", "requires st-util or st-link_gdbserver.exe on path"), {
        "code": "tool_missing",
        "retryable": False,
        "suggested_next_actions": ["run stm32-gdb-mcp-check-env", "install missing host tool"],
        "hint": "A required GDB or GDB-server executable is not installed or not on PATH.",
    }),
    (("debug authentication", "debug auth", "device is locked", "debug access is disabled",
      "rdp level 2"), {
        "code": "debug_auth_required",
        "retryable": False,
        "suggested_next_actions": ["check device security/debug authentication state"],
        "hint": "The MCU security state blocks debug access. Authentication or an explicit security-state change is required.",
    }),
    (("can't find openocd.cfg", "no config files specified", "debug adapter has to be specified",
      "adapter driver", "unknown config", "invalid command name", "unknown target",
      "unexpected idcode"), {
        "code": "invalid_target_config",
        "retryable": False,
        "suggested_next_actions": ["suggest_server_args", "load_debug_config"],
        "hint": "The selected probe/target configuration does not match the requested debug target.",
    }),
    (("unable to connect to target", "target examination failed", "examination failed",
      "init mode failed", "failed to read idcode", "target not examined yet",
      "failed to examine target"), {
        "code": "target_unreachable",
        "retryable": False,
        "suggested_next_actions": ["check target power/SWD wiring/reset state", "suggest_server_args"],
        "hint": "The probe opened, but the MCU debug port could not be reached or examined.",
    }),
    (("did not get response from gdb", "timed out", "timeout"), {
        "code": "target_unresponsive",
        "retryable": False,
        "suggested_next_actions": ["halt_execution", "check_session_health"],
        "hint": "The target may be running (reads require a halted core) or the probe is wedged.",
    }),
    (("gdb is not running", "no gdb", "not connected"), {
        "code": "no_session",
        "retryable": False,
        "suggested_next_actions": ["start_debug_session", "recover_session"],
        "hint": "No active debug session — start_debug_session (it auto-retries a busy probe), "
                "or recover_session to restart from the last config. If start_debug_session isn't "
                "in your tool list, invoke it via call(tool='start_debug_session', args={...}).",
    }),
    (("remote communication error", "remote connection closed", "connection reset"), {
        "code": "connection_lost",
        "retryable": True,
        "suggested_next_actions": ["check_session_health", "start_debug_session"],
        "hint": "The link to the GDB server dropped.",
    }),
    (("open failed", "libusb_error_busy", "resource busy", "device or resource busy",
      "already in use", "cannot claim interface"), {
        "code": "probe_busy",
        "retryable": True,
        "suggested_next_actions": ["recover_session", "check_session_health"],
        "hint": "The debug probe is busy or its USB interface is still claimed by another process.",
    }),
    (("unable to open", "libusb", "no device found"), {
        "code": "probe_unavailable",
        "retryable": True,
        "suggested_next_actions": ["recover_session", "check_session_health"],
        "hint": "The debug probe is missing or its USB connection is temporarily unavailable.",
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
