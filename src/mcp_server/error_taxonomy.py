"""Classify raw GDB/probe errors into structured, actionable outcomes.

Phase 2 priority #2 (reliability) + design law #1 (low comprehension overhead):
instead of bubbling up a generic "tool_execution_error", map the failure to a
stable code, whether it is retryable, and the next action that resolves it. This
turns the running-core footgun we hit on hardware (a bare GDB timeout) into a
clear "halt the target first" signal.
"""

import re

# An attach that never examined the target. Named once because the link-failure
# rule below has to stay OUT of the way of it: start_debug_session classifies
# the exception PLUS the whole OpenOCD log, and that log can easily contain a
# register-read complaint alongside the real cause.
_ATTACH_FAILURE_MARKERS = (
    "unable to connect to target", "target examination failed", "examination failed",
    "init mode failed", "failed to read idcode", "target not examined yet",
    "failed to examine target",
)

# Ordered (substrings, classification[, excluding-substrings]) rules; first match
# wins, and a rule is skipped when any of its excluding substrings is present.
#
# The first three rules key off operation labels that mi_guard.ensure_ok puts in
# the message ("load_symbols(...) failed: ..."). They must stay ahead of the
# generic host-tool rules below: GDB says "No such file or directory" for a bad
# ELF path too, and routing that to "install a missing host tool" sent agents
# chasing their toolchain instead of their path (issue #21/#22).
_RULES: list[tuple] = [
    # Ahead of everything: this is a read that must not be reported as target
    # state. Its own wording is unique, so ordering costs nothing (issue #37).
    (("core register read is implausible", "core register read returned no registers"), {
        "code": "register_read_implausible",
        "retryable": True,
        "suggested_next_actions": ["halt_execution", "self_check", "check_session_health"],
        "hint": "The register read did not come back from a halted core — treat it as a failed "
                "read, not as target state. Halt the target and re-read; if it persists, the "
                "GDB/probe link is unhealthy (recover_session).",
    }),
    # Also ahead of the operation-label rules, and for the same reason they exist:
    # the label says which call wrapped the failure, not what failed. A dead
    # GDB/target link during load_symbols was reported as elf_load_failed with
    # retryable:false, so the agent was sent to check its file path — while the
    # cure was stop_debug_session + start_debug_session (issue #36).
    (("remote failure reply", "could not read registers", "remote i/o error",
      "reply contains invalid hex digit", "packet error", "malformed packet"), {
        "code": "connection_lost",
        "retryable": True,
        "suggested_next_actions": ["recover_session", "stop_debug_session", "start_debug_session"],
        "hint": "The GDB<->target link failed mid-operation. This is a session fault, not a bad "
                "file path or a bad ELF — restart the session rather than re-checking arguments.",
    }, _ATTACH_FAILURE_MARKERS),
    (("load_symbols(",), {
        "code": "elf_load_failed",
        "retryable": False,
        "suggested_next_actions": ["inspect_project", "debug_profile(action=set, elf_path=...)"],
        "hint": "GDB could not load that ELF/AXF. Check the path exists (use forward slashes) "
                "and that it is the linked output, not an intermediate object.",
    }),
    (("flash download(",), {
        "code": "flash_failed",
        "retryable": True,
        "suggested_next_actions": ["reset_target", "self_check", "flash_firmware"],
        "hint": "The firmware download did not complete. The flash may be write-protected "
                "(RDP/option bytes), the target may need a reset-halt first, or the adapter "
                "speed may be too high.",
    }),
    (("verify_flash(",), {
        "code": "flash_mismatch",
        "retryable": False,
        "suggested_next_actions": ["flash_firmware", "verify_flash"],
        "hint": "Target flash does not match the ELF — the device is running different code "
                "than the one being debugged. Re-flash before trusting any symbol-based result.",
    }),
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
    (_ATTACH_FAILURE_MARKERS, {
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
    for rule in _RULES:
        needles, classification = rule[0], rule[1]
        excluding = rule[2] if len(rule) > 2 else ()
        if any(needle in lowered for needle in needles) and not any(x in lowered for x in excluding):
            return dict(classification)
    return dict(_FALLBACK)


# OpenOCD prints the probe's measured VTREF as "Target voltage: 3.167938" before
# it attempts to examine the target. Anything above ~1.6 V is a powered STM32.
_VOLTAGE_RE = re.compile(r"target voltage:\s*([0-9]+\.?[0-9]*)", re.IGNORECASE)
_POWERED_VOLTS = 1.6

_POWER_ADVICE = "check target power/SWD wiring/reset state"


def measured_target_voltage(server_log: str) -> float | None:
    """The last VTREF the probe reported in ``server_log``, if it reported one."""
    matches = _VOLTAGE_RE.findall(server_log or "")
    for raw in reversed(matches):
        try:
            return float(raw)
        except ValueError:
            continue
    return None


def refine_target_unreachable(classification: dict, server_log: str) -> dict:
    """Drop the "check target power" advice when the log proves the board is powered.

    Every failed attach in the reported session carried a measured 3.17 V and
    still advised checking power — pointing the agent at the one hypothesis its
    own payload excluded, twice telling the user their board might be dead
    (issue #35). The voltage is already in hand; branch on it.
    """
    if classification.get("code") != "target_unreachable":
        return classification

    volts = measured_target_voltage(server_log)
    if volts is None or volts < _POWERED_VOLTS:
        return classification

    refined = dict(classification)
    refined["measured_target_voltage"] = volts
    refined["suggested_next_actions"] = [
        action for action in classification.get("suggested_next_actions", [])
        if action != _POWER_ADVICE
    ] or ["suggest_server_args"]
    refined["hint"] = (
        f"Target voltage {volts:.2f} V measured — the board IS powered, so this is not a supply "
        "problem. The probe opened but the MCU debug port did not answer. Likely causes, in "
        "order: the core is in a low-power mode that gates SWD (STM32 Stop/Standby) — retry with "
        "connect-under-reset, i.e. server_args containing "
        "-c 'reset_config srst_only srst_nogate connect_assert_srst'; another process still holds "
        "the probe (a stale openocd, an IDE, a prior session); or the firmware repurposed the SWD "
        "pins. Note a failed attach says NOTHING about whether the CPU is executing."
    )
    return refined
