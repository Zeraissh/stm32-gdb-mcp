"""Execution-control tools: reset, run/halt/step, wait-for-stop, and the one-call composites."""

from mcp.types import TextContent, Tool

from ..reset_strategy import resolve_reset_command
from ..tool_response import content_success
from ._helpers import recover_current_session
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
    description="Resets the target device. Can optionally halt immediately after reset.",
    inputSchema={
        "type": "object",
        "properties": {
            "halt": {"type": "boolean", "description": "If true, halts the CPU immediately after reset."},
            "strategy": {"type": "string", "description": "Optional reset strategy, e.g. default, under_reset, or software."},
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
    resp = ctx.gdb_client.reset_halt(command=resolved["command"])
    return [content_success({"message": "Target reset", "reset": resolved}, raw_response=resp)]


@register(Tool(
    name="continue_execution",
    description="Resumes execution of the target device until the next breakpoint.",
    inputSchema={"type": "object", "properties": {}}
))
def continue_execution(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    resp = ctx.gdb_client.continue_execution()
    return [content_success({"message": "Execution continued"}, raw_response=resp)]


@register(Tool(
    name="halt_execution",
    description="Interrupts/halts the target device execution.",
    inputSchema={"type": "object", "properties": {}}
))
def halt_execution(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    resp = ctx.gdb_client.halt_execution()
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
    return [content_success(ctx.fns.capture_state(ctx.gdb_client), suggested_next_actions=["list_source", "disassemble"])]


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
        recover=lambda: recover_current_session(ctx.gdb_client, ctx.gdb_manager, ctx.last_session, ctx.sess),
    )
    next_actions = ["capture_state", "read_memory"]
    if result.get("resume_after"):
        next_actions = ["wait_for_stop", "halt_execution"]
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
