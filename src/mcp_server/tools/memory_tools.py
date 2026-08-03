"""Memory, variable, and expression tools (guarded writes, typed access, tracking,
expression experiments, DWT cycle counting, and statistical PC profiling)."""

from mcp.types import TextContent, Tool

from ..debug_experiments import (
    assert_expressions as run_expression_assertions,
)
from ..debug_experiments import (
    capture_expressions as run_expression_capture,
)
from ..debug_experiments import (
    compare_expressions_after_action as run_expression_comparison,
)
from ..gdb_decode import decode_evaluated_value, decode_memory_bytes
from ..mi_guard import find_mi_error
from ..tool_response import content_error, content_success
from ._helpers import core_state
from .context import ToolContext
from .registry import register


@register(Tool(
    name="read_variable",
    description="Reads the value of a C variable currently in scope.",
    inputSchema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Name of the variable to read."}
        },
        "required": ["name"]
    }
))
def read_variable(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    resp = ctx.gdb_client.read_variable(arguments["name"])
    value = decode_evaluated_value(resp)
    if value is None:
        # GDB usually says exactly what went wrong ("No symbol \"foo\" in current
        # context."). Overwriting that with a guess about target state sent agents
        # to halt the core and reload symbols for what was a quoting bug (issue #38).
        gdb_error = find_mi_error(resp)
        if gdb_error:
            return [content_error(
                f"GDB could not evaluate {arguments['name']!r}: {gdb_error}",
                code="gdb_error",
                raw_response=resp,
                suggested_next_actions=["inspect_symbol", "load_symbols"],
            )]
        return [content_error(
            f"No value returned for expression {arguments['name']!r}. Target may be running or symbols may be missing.",
            code="no_value_returned",
            raw_response=resp,
            suggested_next_actions=["halt_execution", "load_symbols", "expressions"],
            core_state=core_state(ctx.gdb_client),
        )]
    return [content_success(
        {"message": "Variable read", "name": arguments["name"], "value": value},
        raw_response=resp,
        core_state=core_state(ctx.gdb_client),
    )]


@register(Tool(
    name="read_memory",
    description="Reads a block of memory from the target.",
    inputSchema={
        "type": "object",
        "properties": {
            "address": {"type": "string", "description": "Hex address to read from, e.g., '0x20000000'."},
            "length": {"type": "integer", "description": "Number of bytes to read."}
        },
        "required": ["address", "length"]
    }
))
def read_memory(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    resp = ctx.gdb_client.read_memory(arguments["address"], arguments["length"])
    contents = decode_memory_bytes(resp)
    if contents is None:
        gdb_error = find_mi_error(resp)
        if gdb_error:
            return [content_error(
                f"GDB could not read memory at {arguments['address']}: {gdb_error}",
                code="gdb_error",
                raw_response=resp,
                suggested_next_actions=["self_check", "capture_state"],
            )]
        return [content_error(
            f"No memory bytes returned for address {arguments['address']}. Target may be running or inaccessible.",
            code="no_value_returned",
            raw_response=resp,
            suggested_next_actions=["halt_execution", "self_check", "capture_state"],
            core_state=core_state(ctx.gdb_client),
        )]
    return [content_success(
        {
            "message": "Memory read",
            "address": arguments["address"],
            "length": arguments["length"],
            "bytes": contents,
        },
        raw_response=resp,
        core_state=core_state(ctx.gdb_client),
    )]


@register(Tool(
    name="write_memory",
    description="Writes a value to a specific memory address.",
    inputSchema={
        "type": "object",
        "properties": {
            "address": {"type": "string", "description": "Hex address to write to, e.g., '0x20000000'."},
            "value": {"type": "string", "description": "Value to write, e.g., '0xFF' or '1234'."}
        },
        "required": ["address", "value"]
    }
))
def write_memory(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    address = arguments["address"]
    value = arguments["value"]
    decision = ctx.memory_guard.evaluate(int(address, 0), width_bits=32)
    ctx.memory_guard.audit("write_memory", address, value, decision)
    if decision["action"] == "blocked":
        return [content_error(
            f"Write to {address} blocked: {decision['reason']}",
            code="memory_write_blocked",
            raw_response=decision,
            suggested_next_actions=["set_write_policy", "get_write_audit_log"],
        )]
    if decision["action"] == "simulated":
        return [content_success(
            {"message": "Memory write simulated (dry_run)", "address": address, "value": value, "guard": decision},
        )]
    resp = ctx.gdb_client.write_memory(address, value)
    return [content_success(
        {"message": "Memory written", "address": address, "value": value, "guard": decision},
        raw_response=resp,
    )]


@register(Tool(
    name="set_write_policy",
    description="Configures memory-write guardrails: mode ('enforce' or 'dry_run'), and "
                "optional allow/protected regions. Protected regions (option bytes, IWDG, "
                "WWDG) block writes by default; dry_run simulates every write.",
    inputSchema={
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["enforce", "dry_run"], "description": "Guard mode."},
            "add_allow": {
                "type": "array",
                "description": "Regions to explicitly allow, overriding protection.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "start": {"type": "integer"},
                        "end": {"type": "integer"}
                    }
                }
            },
            "add_protected": {
                "type": "array",
                "description": "Additional regions to protect from writes.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "start": {"type": "integer"},
                        "end": {"type": "integer"}
                    }
                }
            }
        }
    }
))
def set_write_policy(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    policy = ctx.memory_guard.set_policy(
        mode=arguments.get("mode"),
        add_allow=arguments.get("add_allow"),
        add_protected=arguments.get("add_protected"),
    )
    return [content_success({"message": "Write policy updated", "policy": policy})]


@register(Tool(
    name="get_write_audit_log",
    description="Returns the append-only audit log of every memory-write decision "
                "(written, blocked, or simulated).",
    inputSchema={
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "Return only the most recent N entries."}
        }
    }
))
def get_write_audit_log(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    log = ctx.memory_guard.get_audit_log(limit=arguments.get("limit"))
    return [content_success({"audit_log": log, "count": len(log)})]


@register(Tool(
    name="capture_expressions",
    description="Reads GDB expressions and returns parsed values. Optionally builds an indexed table from "
                "array-like expression prefixes while preserving the raw per-expression values.",
    inputSchema={
        "type": "object",
        "properties": {
            "expressions": {"type": "array", "items": {"type": "string"}, "description": "GDB/C expressions to evaluate."},
            "table": {
                "type": "object",
                "description": "Optional indexed table request, e.g. columns=['count'], index_range=[2,6].",
                "properties": {
                    "index_range": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Inclusive [start, end] index range.",
                    },
                    "columns": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Expression prefixes; each cell reads prefix[index].",
                    },
                },
            },
        },
    }
))
def capture_expressions(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    result = run_expression_capture(
        ctx.gdb_client,
        expressions=arguments.get("expressions"),
        table=arguments.get("table"),
    )
    return [content_success(result)]


@register(Tool(
    name="assert_expressions",
    description="Reads GDB expressions and evaluates assertions with operators ==, !=, >, >=, <, <=.",
    inputSchema={
        "type": "object",
        "properties": {
            "assertions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "expression": {"type": "string"},
                        "operator": {"type": "string", "enum": ["==", "!=", ">", ">=", "<", "<="]},
                        "expected": {}
                    },
                    "required": ["expression", "expected"]
                }
            }
        },
        "required": ["assertions"]
    }
))
def assert_expressions(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    result = run_expression_assertions(ctx.gdb_client, arguments["assertions"])
    return [content_success(result)]


@register(Tool(
    name="compare_expressions_after_action",
    description="Captures expressions, performs one debug action, captures them again, and reports changes.",
    inputSchema={
        "type": "object",
        "properties": {
            "expressions": {"type": "array", "items": {"type": "string"}},
            "action": {"type": "string", "enum": ["step_over", "step_into", "continue", "halt", "reset_halt"]}
        },
        "required": ["expressions", "action"]
    }
))
def compare_expressions_after_action(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    gdb_client = ctx.gdb_client
    action_name = arguments["action"]
    action_map = {
        "step_over": gdb_client.step_over,
        "step_into": gdb_client.step_into,
        "continue": gdb_client.continue_execution,
        "halt": gdb_client.halt_execution,
        "reset_halt": lambda: gdb_client.reset_halt("monitor reset halt"),
    }
    action = action_map[action_name]
    result = run_expression_comparison(gdb_client, arguments["expressions"], action_name, action)
    return [content_success(result)]


@register(Tool(
    name="read_typed_memory",
    description="Reads memory with an explicit element width and count.",
    inputSchema={
        "type": "object",
        "properties": {
            "address": {"type": "string", "description": "Hex address to read from, e.g., '0x20000000'."},
            "width_bits": {"type": "integer", "enum": [8, 16, 32, 64], "description": "Element width in bits."},
            "count": {"type": "integer", "description": "Number of elements to read."}
        },
        "required": ["address", "width_bits", "count"]
    }
))
def read_typed_memory(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    resp = ctx.gdb_client.read_typed_memory(arguments["address"], arguments["width_bits"], arguments["count"])
    return [content_success(
        {
            "message": "Typed memory read",
            "address": arguments["address"],
            "width_bits": arguments["width_bits"],
            "count": arguments["count"],
        },
        raw_response=resp,
        core_state=core_state(ctx.gdb_client),
    )]


@register(Tool(
    name="write_typed_memory",
    description="Writes memory with an explicit C integer width.",
    inputSchema={
        "type": "object",
        "properties": {
            "address": {"type": "string", "description": "Hex address to write to, e.g., '0x20000000'."},
            "value": {"type": "string", "description": "Value to write, e.g., '0x12345678'."},
            "width_bits": {"type": "integer", "enum": [8, 16, 32, 64], "description": "Element width in bits."}
        },
        "required": ["address", "value", "width_bits"]
    }
))
def write_typed_memory(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    resp = ctx.gdb_client.write_typed_memory(arguments["address"], arguments["value"], arguments["width_bits"])
    return [content_success(
        {
            "message": "Typed memory written",
            "address": arguments["address"],
            "value": arguments["value"],
            "width_bits": arguments["width_bits"],
        },
        raw_response=resp,
    )]


@register(Tool(
    name="track_variable",
    description="Background variable tracking: action='start' (needs variable + interval_ms), "
                "'stop', or 'get' (returns the sampled data for trend/leak analysis).",
    inputSchema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["start", "stop", "get"], "description": "What to do."},
            "variable": {"type": "string", "description": "For 'start': the variable/expression to sample."},
            "interval_ms": {"type": "integer", "description": "For 'start': polling interval in ms."}
        },
        "required": ["action"]
    }
))
def track_variable(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    action = arguments["action"]
    if action == "start":
        ctx.variable_tracker.start(arguments["variable"], arguments["interval_ms"])
        return [content_success({
            "message": "Variable tracking started",
            "variable": arguments["variable"],
            "interval_ms": arguments["interval_ms"],
        })]
    if action == "stop":
        ctx.variable_tracker.stop()
        return [content_success({"message": "Tracking stopped"})]
    if action == "get":
        return [content_success({"data": ctx.variable_tracker.get_data()})]
    raise ValueError(f"Unknown track_variable action '{action}'. Use start, stop, or get.")


@register(Tool(
    name="read_cycle_counter",
    description="Enables (if needed) and reads the DWT cycle counter (DWT_CYCCNT) for "
                "on-chip timing measurements.",
    inputSchema={
        "type": "object",
        "properties": {
            "enable": {"type": "boolean", "description": "If true, enable and zero the counter before reading."}
        }
    }
))
def read_cycle_counter(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    if arguments.get("enable"):
        ctx.gdb_client.enable_cycle_counter()
    cycles = ctx.gdb_client.read_cycle_counter()
    return [content_success({"message": "Cycle counter read", "cycles": cycles})]


@register(Tool(
    name="sample_pc",
    description="Statistical PC profiler: enables DWT and samples the program counter while "
                "the core RUNS (non-intrusive, over SWD — no SWO pin or firmware change). "
                "Returns a symbolized hot-spot histogram (top functions by %), not raw hex — "
                "the fast way to find where firmware spends time or what loop it is stuck in. "
                "A high 'unsampleable' count means the core is halted/asleep.",
    inputSchema={
        "type": "object",
        "properties": {
            "count": {"type": "integer", "description": "Number of samples (default 128; more = better statistics, slower)."},
            "enable": {"type": "boolean", "description": "Enable DWT trace before sampling (default true)."}
        }
    }
))
def sample_pc(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    profile = ctx.gdb_client.profile_pc(
        count=arguments.get("count", 128),
        enable=arguments.get("enable", True),
    )
    top = profile["hotspots"][0]["function"] if profile["hotspots"] else None
    if profile["sampled"] == 0:
        # Nothing sampled: the core was halted or asleep the whole time, not running.
        msg = ("No PC samples hit running code — the core is halted, in WFI/sleep, or "
               "stuck in a low-power state. Resume it (continue_execution) or check why "
               "it is not running before profiling.")
        actions = ["continue_execution", "capture_state"]
    else:
        msg = (f"Profiled {profile['total_samples']} PC samples while running; "
               f"hottest: {top} ({profile['hotspots'][0]['percent']}%).")
        actions = ["frame", "disassemble"]
    return [content_success({"message": msg, **profile},
                            suggested_next_actions=actions)]
