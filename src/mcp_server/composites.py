"""Composite operations that collapse a multi-step debug sequence into one call.

Design law #2 of Phase 2: reproducing a complex logic bug should cost as few
tool round-trips as possible. Each function here drives the existing single-step
gdb_client methods and returns one decoded, low-overhead result bundle, so the
agent gets "set a trap, run, here is the full halted context" or "where am I /
what happened" in a single invocation instead of five.
"""

from .gdb_decode import registers_summary


def _halted_context(gdb_client) -> dict:
    """Decoded backtrace + innermost-frame locals for a halted target."""
    return {
        "backtrace": gdb_client.read_call_stack_decoded(),
        "locals": gdb_client.read_frame_variables_decoded(0),
    }


def debug_until(gdb_client, location, condition=None, temporary=True, ignore_count=None, timeout_sec=10.0) -> dict:
    """Set a (conditional, temporary) breakpoint, run, and return the stop context.

    On a stop, the result bundles the stop event plus the decoded backtrace and
    innermost-frame locals. On timeout the core is left running, so no register/
    memory reads are attempted (they would just time out).
    """
    result = {"location": location}
    if location:
        gdb_client.set_breakpoint(location, condition=condition, temporary=temporary, ignore_count=ignore_count)

    event = gdb_client.run_and_wait(timeout_sec=timeout_sec)
    event.pop("raw_response", None)
    result["stop"] = event
    result["stopped"] = bool(event.get("stopped"))

    if result["stopped"]:
        result.update(_halted_context(gdb_client))
    else:
        result["note"] = "target did not stop within timeout; it is still running — halt before reading state"
    return result


def capture_state(gdb_client) -> dict:
    """One-shot "where am I": decoded registers, backtrace, and top-frame locals."""
    registers = gdb_client.read_core_registers_decoded()
    state = {
        "registers": registers,
        "summary": registers_summary(registers),
    }
    state.update(_halted_context(gdb_client))
    return state


def flash_and_run(gdb_client, file_path, run_to="main", timeout_sec=10.0) -> dict:
    """Flash an ELF, reset-halt, break once at an entry point, and run to it."""
    gdb_client.load_firmware(file_path)
    gdb_client.reset_halt()
    result = debug_until(gdb_client, location=run_to, temporary=True, timeout_sec=timeout_sec)
    result["flashed"] = file_path
    return result
