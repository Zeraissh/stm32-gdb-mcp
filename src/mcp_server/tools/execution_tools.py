"""Execution-control tools: reset, run/halt/step, wait-for-stop, and the one-call composites."""

from mcp.types import TextContent, Tool

from ..reset_strategy import resolve_reset_command
from ..stop_event import was_already_halted, was_already_running
from ..tool_response import content_error, content_success
from ._helpers import core_state, hub_binding, recover_current_session
from .context import ToolContext
from .registry import register


def _stop_event_next_actions(event: dict) -> list[str]:
    """Guide the model to the natural next loop step for a stop event."""
    reason = event.get("reason")
    if reason in ("signal-received", "exited-signalled"):
        return ["diagnose_fault", "reconstruct_fault_context", "read_call_stack"]
    if reason == "timeout":
        # Timeout means the breakpoint's code path was NOT reached — do not just retry.
        # Halt, see where execution actually is, and check whether the breakpoint was
        # ever hit (hit_count=0 => the precondition/flag to reach it was not satisfied).
        return ["halt_execution", "capture_state", "list_breakpoints"]
    if event.get("stopped"):
        return ["read_frame_variables", "list_source", "read_call_stack"]
    return []


@register(Tool(
    name="reset_target",
    description="Resets the target device. Can optionally halt immediately after reset. "
                "strategy=\"cold\" removes target power through a configured programmable USB "
                "hub before resetting, which is the only strategy that produces a true "
                "power-on reset (RCC_CSR POR flag, cleared .noinit, drained RAM); it fails "
                "rather than silently doing a warm reset when no hub channel is mapped. It "
                "needs no confirm argument -- asking for a cold reset by name IS the "
                "confirmation the hub guard wants -- and it stops the GDB session before the "
                "cut, then re-establishes it.",
    inputSchema={
        "type": "object",
        "properties": {
            "halt": {"type": "boolean", "description": "If true, halts the CPU immediately after reset."},
            "strategy": {"type": "string", "description":
                "Optional reset strategy: default, under_reset, software, or cold (hub power "
                "cycle). \"software\" issues soft_reset_halt, which resets through the debug "
                "unit WITHOUT asserting SYSRESETREQ -- so RCC_CSR does not record it and its "
                "sticky reset flags do not move; use \"default\" when the reset must be visible "
                "there, e.g. when proving a cold boot by watching a sticky flag disappear. "
                "\"under_reset\" is currently an alias of \"default\". The result's `note` says "
                "which of these applied to the call you actually made."},
            "command": {"type": "string", "description": "Optional custom GDB monitor reset command."}
        },
        "required": ["halt"]
    }
))
def reset_target(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    halt = arguments["halt"]
    profile = ctx.debug_profile.get()
    reset_config = profile.get("reset", {})
    resolved = resolve_reset_command(
        ctx.gdb_manager.server_type or profile.get("server_type"),
        halt=halt,
        strategy=arguments.get("strategy") or reset_config.get("strategy"),
        command=arguments.get("command") or reset_config.get("command"),
    )

    data: dict = {"message": "Target reset", "reset": resolved}
    if resolved.get("requires_power_cycle"):
        binding = hub_binding(ctx)
        if binding is None:
            # Downgrading to a warm reset here would report success for something
            # that did not happen -- the exact false-success shape reset_strategy
            # already warns about for 'under_reset'.
            #
            # hub_binding() collapses every "no hub here" reason into None, but the
            # one it still tells apart is the one that actually bites on a rack: a
            # profile with no hub block at all, i.e. a per-session load that never
            # happened. Sending that case to "map a channel" teaches the user to
            # hard-code a channel per session and bypass hub.map entirely.
            load = f'debug_config(action=load, path=..., session="{ctx.session_id}")'
            if not profile.get("hub"):
                remedy = (f'The debug profile for session "{ctx.session_id}" has no hub block, and '
                          f"the profile is per-session: a rack config loaded into another session "
                          f"is invisible here, and a server restart clears it. Load it into this "
                          f'session ({load}), or use strategy="default".')
                actions = [load, 'reset_target(strategy="default")']
            else:
                remedy = ('This session HAS a hub block, but no channel resolved from it. '
                          'hub(action=describe) shows the map beside the live channel list, and '
                          'hub(action=power, state="on") reports the precise reason. Or use '
                          'strategy="default".')
                actions = ["hub(action=describe)", "hub(action=discover, apply=true)",
                           'reset_target(strategy="default")']
            return [content_error(
                "strategy=\"cold\" needs a programmable USB hub channel for this session, and "
                "none is mapped. A cold reset is not a command, it is removing power -- "
                "silently doing a warm reset instead would report a power-on reset that never "
                f"happened. {remedy}",
                code="cold_reset_unavailable",
                suggested_next_actions=actions,
            )]
        if not ctx.last_session.get("server_type"):
            return [content_error(
                "strategy=\"cold\" removes power from the port the debug probe is on, which drops "
                "the debug session -- so it needs a session it can re-establish afterwards, and "
                "there is no prior start_debug_session to repeat.",
                code="no_session",
                suggested_next_actions=["start_debug_session"],
            )]

        manager, channel = binding
        cycle_config = (profile.get("hub") or {}).get("power_cycle") or {}

        # Tear the session down BEFORE cutting power. Killing a GDB server by
        # yanking its probe's VBUS is the wedging scenario this whole feature
        # exists to fix; do not create it while curing it.
        for teardown in (ctx.gdb_client.stop_gdb, ctx.gdb_manager.stop):
            try:
                teardown()
            except Exception:  # noqa: BLE001 - teardown is best effort
                pass

        try:
            data["power_cycle"] = manager.power_cycle(
                channel,
                off_ms=int(cycle_config.get("off_ms", 400)),
                settle_ms=int(cycle_config.get("settle_ms", 1500)),
                confirm=True,  # the caller asked for a cold reset by name
            )
        except Exception as exc:  # noqa: BLE001 - report, never half-reset silently
            return [content_error(
                f"cold reset could not power-cycle hub channel {channel}: {exc}",
                code="cold_reset_failed",
                suggested_next_actions=["hub(action=describe)", "recover_session",
                                        "reset_target(strategy=\"default\")"],
            )]

        # On any normal rig the probe sits on the port we just cut, so it has
        # re-enumerated and the old GDB connection is dead. Measured on hardware:
        # skipping this leaves a session where reads return 0x00000000 and
        # check_session_health still reports target_responsive=true -- a wrong
        # answer that looks exactly like a right one.
        try:
            recovered = recover_current_session(ctx.gdb_client, ctx.gdb_manager,
                                                ctx.last_session, ctx.sess, escalate=False)
        except Exception as exc:  # noqa: BLE001
            return [content_error(
                f"the board was power-cycled, but the debug session could not be re-established: "
                f"{exc}. The target is running from its power-on reset; reconnect before reading "
                f"anything, or reads will return zeros that look like data.",
                code="cold_reset_reattach_failed",
                data=data,
                suggested_next_actions=["recover_session", "self_check"],
            )]

        data["reattached"] = {"port": recovered["port"],
                              "symbols_loaded": recovered["symbols_loaded"]}

        # The power-on reset IS the reset. Issuing another one would overwrite the
        # RCC_CSR evidence and re-run the firmware's early init, destroying the
        # cold-boot state (.noinit, drained RAM) that is the only reason to ask
        # for this strategy.
        if halt:
            resp = ctx.gdb_client.halt_execution()
            data["message"] = "Target power-cycled (power-on reset) and halted"
            data["note"] = (
                "Halted where the core was, NOT at the reset vector: the firmware runs for the "
                "USB re-enumeration window (~1-3 s) before the probe is usable again. No second "
                "reset was issued -- that would have overwritten the RCC_CSR power-on flag and "
                "re-run early init, destroying the cold-boot state. To catch the core AT the "
                "reset vector, start the server with server_args containing "
                "-c \"reset_config srst_only srst_nogate connect_assert_srst\"."
            )
        else:
            resp = None
            data["message"] = "Target power-cycled (power-on reset) and left running"
        return [content_success(data, raw_response=resp)]

    resp = ctx.gdb_client.reset_halt(command=resolved["command"])
    return [content_success(data, raw_response=resp)]


@register(Tool(
    name="continue_execution",
    description="Resumes execution of the target device until the next breakpoint. "
                "Safe to call when the target is already running (reports already_running "
                "instead of erroring), so 'make sure it is running' is expressible.",
    inputSchema={"type": "object", "properties": {}}
))
def continue_execution(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    resp = ctx.gdb_client.continue_execution()
    if was_already_running(resp):
        # Saying "Execution continued" for a resume that was never sent is the
        # same claim-without-the-action halt_execution already avoids below.
        return [content_success(
            {"message": "Target was already running; no resume sent", "already_running": True},
            raw_response=resp,
            core_state=core_state(ctx.gdb_client),
        )]
    return [content_success(
        {"message": "Execution continued"},
        raw_response=resp,
        core_state=core_state(ctx.gdb_client),
    )]


@register(Tool(
    name="halt_execution",
    description="Interrupts/halts the target device execution.",
    inputSchema={"type": "object", "properties": {}}
))
def halt_execution(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    resp = ctx.gdb_client.halt_execution()
    if was_already_halted(resp):
        return [content_success(
            {"message": "Target was already halted; no interrupt sent", "already_halted": True},
            raw_response=resp,
        )]
    return [content_success({"message": "Execution halted"}, raw_response=resp)]


@register(Tool(
    name="run_and_wait",
    description="Resumes the target and waits, returning a structured stop event "
                "(reason, symbolized frame, breakpoint id, signal) or a timeout. "
                "Use this instead of continue_execution + polling to close the debug loop.",
    inputSchema={
        "type": "object",
        "properties": {
            "timeout_sec": {"type": "number", "description": "Max seconds to wait for a stop (default 10)."}
        }
    }
))
def run_and_wait(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    event = ctx.gdb_client.run_and_wait(timeout_sec=arguments.get("timeout_sec", 10.0))
    raw = event.pop("raw_response", None)
    next_actions = _stop_event_next_actions(event)
    return [content_success(event, raw_response=raw, suggested_next_actions=next_actions)]


@register(Tool(
    name="debug_until",
    description="One-call repro step: set an optional conditional/temporary breakpoint at "
                "a location, run, and return the stop event PLUS the decoded backtrace and "
                "innermost-frame locals. Collapses set_breakpoint + run + read frame/vars "
                "into a single round-trip.",
    inputSchema={
        "type": "object",
        "properties": {
            "location": {"type": "string", "description": "Where to break, e.g. 'trigger_divzero' or 'main.c:21'."},
            "condition": {"type": "string", "description": "Optional C condition, e.g. 'g_divisor == 0'."},
            "temporary": {"type": "boolean", "description": "Auto-delete the breakpoint after the first hit (default true)."},
            "ignore_count": {"type": "integer", "description": "Hits to ignore before stopping."},
            "timeout_sec": {"type": "number", "description": "Max seconds to wait (default 10)."}
        },
        "required": ["location"]
    }
))
def debug_until(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    result = ctx.fns.debug_until(
        ctx.gdb_client,
        location=arguments["location"],
        condition=arguments.get("condition"),
        temporary=arguments.get("temporary", True),
        ignore_count=arguments.get("ignore_count"),
        timeout_sec=arguments.get("timeout_sec", 10.0),
    )
    next_actions = ["capture_state", "list_source"] if result["stopped"] else ["halt_execution"]
    return [content_success(result, suggested_next_actions=next_actions)]


@register(Tool(
    name="capture_state",
    description="One-call 'where am I': decoded core registers + a PC/LR/SP summary, the "
                "decoded backtrace, and the innermost-frame locals. The fastest way to get "
                "full halted context.",
    inputSchema={"type": "object", "properties": {}}
))
def capture_state(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    return [content_success(
        ctx.fns.capture_state(ctx.gdb_client),
        suggested_next_actions=["list_source", "disassemble"],
        core_state=core_state(ctx.gdb_client),
    )]


@register(Tool(
    name="run_for_duration",
    description="Runs the target freely for a wall-clock duration, halts it, and returns "
                "one structured report with final frame/context and optional expression "
                "captures. Useful for counters, telemetry buffers, polling loops, and "
                "timing-sensitive bus diagnostics without breakpoint perturbation.",
    inputSchema={
        "type": "object",
        "properties": {
            "duration_sec": {"type": "number", "description": "Seconds to let the target run freely."},
            "then": {"type": "string", "enum": ["halt"], "description": "Action after duration (default halt)."},
            "capture": {
                "type": "object",
                "properties": {
                    "expressions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional GDB/C expressions to capture after halting.",
                    },
                    "table": {
                        "type": "object",
                        "description": "Optional indexed table capture after halting: {index_range:[start,end], columns:[prefix,...]}.",
                    },
                },
            },
            "sample": {
                "type": "object",
                "properties": {
                    "interval_sec": {"type": "number", "description": "Low-rate polling interval in seconds."},
                    "interval_ms": {"type": "number", "description": "Low-rate polling interval in milliseconds."},
                    "expressions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "GDB/C expressions to poll while the target runs.",
                    },
                    "table": {
                        "type": "object",
                        "description": "Optional indexed table to poll: {index_range:[start,end], columns:[prefix,...]}.",
                    },
                    "max_samples": {
                        "type": "integer",
                        "description": "Safety cap on generated samples (default 10000).",
                    },
                },
                "description": "Best-effort low-rate debugger polling while running; does not halt the target.",
            },
            "resume_after": {"type": "boolean", "description": "Resume execution after capture (default false)."},
        },
        "required": ["duration_sec"],
    },
))
def run_for_duration(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    result = ctx.fns.run_for_duration(
        ctx.gdb_client,
        duration_sec=arguments["duration_sec"],
        then=arguments.get("then", "halt"),
        capture=arguments.get("capture"),
        sample=arguments.get("sample"),
        resume_after=arguments.get("resume_after", False),
        # escalate=False on purpose: this is an automatic mid-run recovery, and
        # power-cycling the board would destroy the very run being measured.
        # Escalation stays where the agent explicitly asked to fix a link
        # (recover_session).
        recover=lambda: recover_current_session(ctx.gdb_client, ctx.gdb_manager, ctx.last_session,
                                                ctx.sess, escalate=False),
    )
    next_actions = ["capture_state", "read_memory"]
    if result.get("resume_after"):
        next_actions = ["wait_for_stop", "halt_execution"]
    if not result.get("ran_full_duration", True):
        # It stopped on its own inside the window: the captured state describes
        # that stop, not a free-running target — diagnose it rather than re-run.
        next_actions = ["capture_state", "breakpoint(action=list)", "reconstruct_fault_context"]
    return [content_success(result, suggested_next_actions=next_actions)]


@register(Tool(
    name="wait_for_stop",
    description="Waits for the next stop event WITHOUT resuming the target, returning "
                "a structured stop event or a timeout.",
    inputSchema={
        "type": "object",
        "properties": {
            "timeout_sec": {"type": "number", "description": "Max seconds to wait for a stop (default 10)."}
        }
    }
))
def wait_for_stop(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    event = ctx.gdb_client.wait_for_stop(timeout_sec=arguments.get("timeout_sec", 10.0))
    raw = event.pop("raw_response", None)
    next_actions = _stop_event_next_actions(event)
    return [content_success(event, raw_response=raw, suggested_next_actions=next_actions)]


@register(Tool(
    name="step",
    description="Single-steps the target: kind='over' (over the line), 'into' (into calls), "
                "'out' (run until the function returns), or 'instruction' (one machine "
                "instruction; set over=true to step over a call).",
    inputSchema={
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["over", "into", "out", "instruction"], "description": "Step kind (default 'over')."},
            "over": {"type": "boolean", "description": "For kind='instruction', step over a called function."}
        }
    }
))
def step(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    kind = arguments.get("kind", "over")
    if kind == "over":
        resp = ctx.gdb_client.step_over()
    elif kind == "into":
        resp = ctx.gdb_client.step_into()
    elif kind == "out":
        resp = ctx.gdb_client.step_out()
    elif kind == "instruction":
        resp = ctx.gdb_client.step_instruction(over=arguments.get("over", False))
    else:
        raise ValueError(f"Unknown step kind '{kind}'. Use over, into, out, or instruction.")
    return [content_success({"message": f"Stepped ({kind})", "kind": kind}, raw_response=resp)]


@register(Tool(
    name="run_to_line",
    description="Runs until a given location is reached (function, 'file.c:42', or '*0xADDR').",
    inputSchema={
        "type": "object",
        "properties": {
            "location": {"type": "string", "description": "Where to run to."}
        },
        "required": ["location"]
    }
))
def run_to_line(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    resp = ctx.gdb_client.run_to_line(arguments["location"])
    return [content_success(
        {"message": "Ran to location", "location": arguments["location"]},
        raw_response=resp,
    )]
