"""Composite operations that collapse a multi-step debug sequence into one call.

Design law #2 of Phase 2: reproducing a complex logic bug should cost as few
tool round-trips as possible. Each function here drives the existing single-step
gdb_client methods and returns one decoded, low-overhead result bundle, so the
agent gets "set a trap, run, here is the full halted context" or "where am I /
what happened" in a single invocation instead of five.
"""

import time

from .debug_experiments import capture_expressions
from .gdb_decode import registers_summary
from .sampling import sample_expressions, sample_interval_from_config
from .stop_event import parse_stop_event, was_already_halted


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


def flash_and_run(
    gdb_client,
    file_path,
    run_to="main",
    timeout_sec=10.0,
    reset_command="monitor reset halt",
) -> dict:
    """Flash an ELF, reset-halt, break once at an entry point, and run to it."""
    gdb_client.load_firmware(file_path)
    gdb_client.reset_halt(command=reset_command)
    result = debug_until(gdb_client, location=run_to, temporary=True, timeout_sec=timeout_sec)
    result["flashed"] = file_path
    return result


def run_for_duration(
    gdb_client,
    duration_sec: float,
    then: str = "halt",
    capture: dict | None = None,
    sample: dict | None = None,
    resume_after: bool = False,
    recover=None,
    sleep=None,
    monotonic=None,
) -> dict:
    """Run naturally for a wall-clock duration, halt, then return captured state."""
    if duration_sec < 0:
        raise ValueError("duration_sec must be >= 0")
    if then != "halt":
        raise ValueError("then must be 'halt'")

    sleep_fn = sleep or time.sleep
    monotonic_fn = monotonic or time.monotonic
    started = monotonic_fn()
    run_response = gdb_client.continue_execution()
    # If the target stopped the instant it resumed (pending interrupt, instant
    # breakpoint, fault), sleeping through the duration and reporting it as a
    # clean free-run would be a lie (issue #22) — capture the stop instead.
    stopped_early = None
    run_event = parse_stop_event(run_response)
    if run_event["stopped"]:
        stopped_early = run_event
    sample_result = None
    if stopped_early is None:
        if sample:
            sample_result = sample_expressions(
                gdb_client,
                duration_sec=duration_sec,
                interval_sec=sample_interval_from_config(sample),
                expressions=sample.get("expressions"),
                table=sample.get("table"),
                max_samples=sample.get("max_samples", 10_000),
                sleep=sleep_fn,
                monotonic=monotonic_fn,
            )
        else:
            sleep_fn(float(duration_sec))
    elapsed = monotonic_fn() - started

    halt_method = "halt_execution"
    halt_error = None
    if stopped_early is not None:
        halt_method = "already_stopped"
        halt_response = []
    else:
        try:
            halt_response = gdb_client.halt_execution()
        except Exception as exc:
            halt_error = str(exc)
            if recover is None:
                raise
            recover()
            halt_response = gdb_client.halt_execution()
            halt_method = "recover_session+halt_execution"
        # A halt of our own making stops with signal-received/SIGINT; any other
        # reason (or a target found already halted) means it stopped on its own
        # inside the window — a breakpoint, watchpoint, or fault we must report.
        halt_event = parse_stop_event(halt_response)
        if was_already_halted(halt_response):
            stopped_early = halt_event if halt_event["stopped"] else {"stopped": True, "reason": "unknown"}
        elif halt_event["stopped"] and halt_event.get("reason") not in (None, "signal-received"):
            stopped_early = halt_event

    context = _halted_context(gdb_client)
    result = {
        "duration_sec": float(duration_sec),
        "elapsed_sec": round(elapsed, 3),
        "ran_full_duration": stopped_early is None,
        "run": {"method": "continue_execution", "raw_response": run_response},
        "halt": {"method": halt_method, "raw_response": halt_response},
        "final_frame": context["backtrace"][0] if context["backtrace"] else None,
        "backtrace": context["backtrace"],
        "locals": context["locals"],
        "capture": {},
        "resume_after": bool(resume_after),
    }
    if stopped_early is not None:
        result["stopped_early"] = stopped_early
    if sample_result is not None:
        result["sample"] = sample_result
    if halt_error is not None:
        result["halt"]["first_error"] = halt_error

    requested = capture or {}
    expressions = requested.get("expressions") or []
    table = requested.get("table")
    if expressions or table:
        result["capture"]["expressions"] = capture_expressions(gdb_client, expressions, table=table)

    if resume_after:
        result["resume"] = {
            "method": "continue_execution",
            "raw_response": gdb_client.continue_execution(),
        }

    return result
