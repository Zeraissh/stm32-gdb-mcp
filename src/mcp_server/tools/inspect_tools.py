"""Symbol / source / stack inspection tools (read-only views of the halted core)."""

from mcp.types import TextContent, Tool

from ..gdb_decode import (
    decode_console_text,
    decode_evaluated_value,
    decode_symbol_resolution,
    registers_summary,
)
from ..mi_guard import find_mi_error
from ..stack_analysis import stack_report
from ..tool_response import content_error, content_success
from ._helpers import core_state
from .context import ToolContext
from .registry import register

# GDB's answer to `info functions` on a full firmware ELF runs to tens of
# thousands of characters. Cap it, but never silently: a truncated payload that
# reads as complete is the same class of lie as an empty one that reads as
# success (issue #40).
_MAX_CONSOLE_CHARS = 8000


def _terminal_mi_error(resp) -> str | None:
    """The message from a terminal ``^error`` record only, ignoring stream text."""
    for record in resp or []:
        if record.get("type") == "result" and record.get("message") == "error":
            payload = record.get("payload")
            msg = payload.get("msg") if isinstance(payload, dict) else None
            return str(msg) if msg else "GDB reported an error"
    return None


def _console_payload(resp, **extra) -> dict:
    """Payload carrying GDB's actual console answer, truncation flagged in-band."""
    text = decode_console_text(resp)
    payload = {k: v for k, v in extra.items() if v is not None}
    if len(text) > _MAX_CONSOLE_CHARS:
        payload["text"] = text[:_MAX_CONSOLE_CHARS]
        payload["truncated"] = True
        payload["note"] = (f"output truncated at {_MAX_CONSOLE_CHARS} of {len(text)} chars — "
                           "narrow the query (regex / smaller count) to see the rest")
    else:
        payload["text"] = text
    return payload


def _evaluated_result(resp, key: str, what: str, **extra) -> list[TextContent]:
    """Return the value GDB evaluated under ``key``, or its error.

    ``address_of`` used to answer "Address resolved" and never include an
    address; ``sizeof`` answered "Size evaluated" and never include a size.
    """
    value = decode_evaluated_value(resp)
    if value is None:
        error = find_mi_error(resp)
        return [content_error(
            f"GDB could not evaluate {what}" + (f": {error}" if error else " (no value returned)"),
            code="gdb_error" if error else "no_value_returned",
            raw_response=resp,
            suggested_next_actions=["load_symbols", "inspect_symbol"],
        )]
    return [content_success({key: value, **extra}, raw_response=resp)]


def _console_result(resp, **extra) -> list[TextContent]:
    """Return GDB's console answer, or its error — never a bare 'done' message.

    Only the terminal ``^error`` counts here. find_mi_error also scans stream
    text for markers like "no such file or directory", which is right for GDB's
    own output but wrong once the payload is arbitrary user source code: a
    firmware log line quoting that phrase would turn a successful listing into a
    reported failure.
    """
    error = _terminal_mi_error(resp)
    if error:
        return [content_error(error, code="gdb_error", raw_response=resp,
                              suggested_next_actions=["load_symbols", "inspect_project"])]
    return [content_success(_console_payload(resp, **extra), raw_response=resp)]


@register(Tool(
    name="read_call_stack",
    description="Reads the call stack as a decoded list of frames "
                "{level, func, file, line, addr} plus a one-line summary. "
                "Set include_raw=true to also get the raw GDB output.",
    inputSchema={
        "type": "object",
        "properties": {
            "include_raw": {"type": "boolean", "description": "Include the raw GDB transcript (default false)."}
        }
    }
))
def read_call_stack(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    frames = ctx.gdb_client.read_call_stack_decoded()
    if frames:
        top = frames[0]
        summary = f"{len(frames)} frames; top: {top['func']} at {top['file']}:{top['line']}"
    else:
        summary = "no frames available (target running or no symbols)"
    raw = ctx.gdb_client.read_call_stack() if arguments.get("include_raw") else None
    return [content_success(
        {"frames": frames, "summary": summary},
        raw_response=raw,
        suggested_next_actions=["read_frame_variables", "list_source"],
    )]


@register(Tool(
    name="read_core_registers",
    description="Reads CPU core registers as a decoded {name: hex} map plus a one-line "
                "summary of PC/LR/SP. Set include_raw=true to also get the raw GDB output.",
    inputSchema={
        "type": "object",
        "properties": {
            "include_raw": {"type": "boolean", "description": "Include the raw GDB transcript (default false)."}
        }
    }
))
def read_core_registers(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    registers = ctx.gdb_client.read_core_registers_decoded()
    raw = ctx.gdb_client.read_core_registers() if arguments.get("include_raw") else None
    return [content_success(
        {"registers": registers, "summary": registers_summary(registers)},
        raw_response=raw,
        core_state=core_state(ctx.gdb_client),
    )]


@register(Tool(
    name="select_frame",
    description="Selects a stack frame by level (0 = innermost) for subsequent variable reads.",
    inputSchema={
        "type": "object",
        "properties": {
            "level": {"type": "integer", "description": "Frame level, 0 is the innermost/current frame."}
        },
        "required": ["level"]
    }
))
def select_frame(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    resp = ctx.gdb_client.select_frame(arguments["level"])
    return [content_success({"message": "Frame selected", "level": arguments["level"]}, raw_response=resp)]


@register(Tool(
    name="read_frame_variables",
    description="Returns a decoded {name: value} map of locals and arguments for a stack "
                "frame, plus a count summary. Set include_raw=true for the raw GDB output.",
    inputSchema={
        "type": "object",
        "properties": {
            "level": {"type": "integer", "description": "Optional frame level to select first (0 = innermost)."},
            "include_raw": {"type": "boolean", "description": "Include the raw GDB transcript (default false)."}
        }
    }
))
def read_frame_variables(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    variables = ctx.gdb_client.read_frame_variables_decoded(arguments.get("level"))
    raw = ctx.gdb_client.read_frame_variables(arguments.get("level")) if arguments.get("include_raw") else None
    return [content_success(
        {
            "level": arguments.get("level"),
            "variables": variables,
            "summary": f"{len(variables)} variables in scope",
        },
        raw_response=raw,
        suggested_next_actions=["list_source", "read_variable"],
    )]


@register(Tool(
    name="list_source",
    description="Lists source lines around a location (function, 'file.c:42', or '*0xADDR').",
    inputSchema={
        "type": "object",
        "properties": {
            "location": {"type": "string", "description": "Where to list around. Omit to continue from current."},
            "count": {"type": "integer", "description": "Approximate number of lines (default 10)."}
        }
    }
))
def list_source(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    resp = ctx.gdb_client.list_source(arguments.get("location"), arguments.get("count", 10))
    return _console_result(resp, location=arguments.get("location"))


@register(Tool(
    name="resolve_address",
    description="Maps an address or expression (e.g. '$pc', '0x08001234') to its source "
                "file:line and nearest symbol, returning {resolved, symbol, offset, "
                "section, file, line}. resolved=false means GDB found no symbol there.",
    inputSchema={
        "type": "object",
        "properties": {
            # Advertise the alias rather than only tolerating it: a schema that
            # rejects the name callers reach for, while the handler accepts it,
            # is the same contract/behaviour gap this batch is about.
            "expr": {"type": "string", "description": "Address or expression to resolve, e.g. '$pc' or '0x08001234'. Required (or pass it as 'address')."},
            "address": {"type": "string", "description": "Alias for 'expr'."}
        }
    }
))
def resolve_address(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    # 'address' is the name callers reach for first; accept it rather than
    # answering "Missing required argument: 'expr'" to an otherwise correct call.
    expr = arguments.get("expr") or arguments.get("address")
    if not expr:
        return [content_error(
            "resolve_address needs 'expr' — the address or expression to resolve, "
            "e.g. inspect_symbol(what='resolve', expr='0x08001234').",
            code="missing_argument",
        )]
    resp = ctx.gdb_client.resolve_address(expr)
    resolution = decode_symbol_resolution(resp)
    resolution["expr"] = expr
    if not resolution["resolved"]:
        # "Address resolved" over an empty payload is the same lie as ok:true over
        # a failed read: the caller cannot tell it from a real hit (issue #40).
        return [content_error(
            f"No symbol or line information for {expr} — "
            f"{resolution.get('text') or 'GDB returned nothing'}",
            code="symbol_not_found",
            raw_response=resp,
            suggested_next_actions=["load_symbols", "list_functions"],
        )]
    return [content_success(
        resolution,
        raw_response=resp,
        suggested_next_actions=["list_source", "read_frame_variables"],
    )]


@register(Tool(
    name="analyze_stack",
    description="Reports stack used/free bytes and a clear overflow verdict for the halted "
                "core. stack_top defaults to the initial MSP (first word of the vector table "
                "at vector_table_addr). Give stack_size or stack_limit for the overflow check "
                "(else only usage is reported). The key tool for diagnosing stack overflows.",
    inputSchema={
        "type": "object",
        "properties": {
            "stack_top": {"type": "string", "description": "Top-of-stack address (hex). Default: initial MSP from the vector table."},
            "stack_limit": {"type": "string", "description": "Lowest valid stack address (hex)."},
            "stack_size": {"type": "string", "description": "Stack size in bytes (used as stack_top - stack_size if stack_limit omitted)."},
            "vector_table_addr": {"type": "string", "description": "Vector table base for the initial MSP (default '0x08000000')."}
        }
    }
))
def analyze_stack(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    sp = ctx.gdb_client.read_register_value("$sp")
    if arguments.get("stack_top") is not None:
        stack_top = int(str(arguments["stack_top"]), 0)
    else:
        vt = int(str(arguments.get("vector_table_addr", "0x08000000")), 0)
        stack_top = ctx.gdb_client.read_word(vt)  # initial MSP = first vector
    stack_limit = None
    if arguments.get("stack_limit") is not None:
        stack_limit = int(str(arguments["stack_limit"]), 0)
    elif arguments.get("stack_size") is not None:
        stack_limit = stack_top - int(str(arguments["stack_size"]), 0)
    report = stack_report(sp, stack_top, stack_limit)
    return [content_success(
        report,
        suggested_next_actions=["read_call_stack", "reconstruct_fault_context", "read_freertos_tasks"],
    )]


@register(Tool(
    name="disassemble",
    description="Disassembles N instructions at a location (default $pc).",
    inputSchema={
        "type": "object",
        "properties": {
            "location": {"type": "string", "description": "Where to disassemble from (default '$pc')."},
            "instructions": {"type": "integer", "description": "Number of instructions (default 8)."}
        }
    }
))
def disassemble(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    resp = ctx.gdb_client.disassemble(arguments.get("location", "$pc"), arguments.get("instructions", 8))
    return _console_result(resp, location=arguments.get("location", "$pc"))


@register(Tool(
    name="list_functions",
    description="Lists functions in the loaded symbols, optionally filtered by a regex.",
    inputSchema={
        "type": "object",
        "properties": {
            "regex": {"type": "string", "description": "Optional regex to filter function names."}
        }
    }
))
def list_functions(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    resp = ctx.gdb_client.list_functions(arguments.get("regex"))
    return _console_result(resp, regex=arguments.get("regex"))


@register(Tool(
    name="list_variables",
    description="Lists global/static variables in the loaded symbols, optionally filtered by a regex.",
    inputSchema={
        "type": "object",
        "properties": {
            "regex": {"type": "string", "description": "Optional regex to filter variable names."}
        }
    }
))
def list_variables(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    resp = ctx.gdb_client.list_variables(arguments.get("regex"))
    return _console_result(resp, regex=arguments.get("regex"))


@register(Tool(
    name="lookup_type",
    description="Shows the type/layout of an expression or type name (GDB ptype).",
    inputSchema={
        "type": "object",
        "properties": {
            "expr": {"type": "string", "description": "Expression or type name, e.g. 'my_struct' or 'g_state'."}
        },
        "required": ["expr"]
    }
))
def lookup_type(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    resp = ctx.gdb_client.lookup_type(arguments["expr"])
    return _console_result(resp, expr=arguments["expr"])


@register(Tool(
    name="sizeof",
    description="Evaluates sizeof(expr) against the loaded symbols.",
    inputSchema={
        "type": "object",
        "properties": {
            "expr": {"type": "string", "description": "Type or expression to size, e.g. 'struct foo'."}
        },
        "required": ["expr"]
    }
))
def sizeof(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    resp = ctx.gdb_client.sizeof(arguments["expr"])
    return _evaluated_result(resp, "size", f"sizeof({arguments['expr']})", expr=arguments["expr"])


@register(Tool(
    name="address_of",
    description="Resolves the address of a symbol (&symbol).",
    inputSchema={
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Symbol name, e.g. 'g_state'."}
        },
        "required": ["symbol"]
    }
))
def address_of(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    resp = ctx.gdb_client.address_of(arguments["symbol"])
    return _evaluated_result(resp, "address", f"&{arguments['symbol']}", symbol=arguments["symbol"])
