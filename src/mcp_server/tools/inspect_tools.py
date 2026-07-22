"""Symbol / source / stack inspection tools (read-only views of the halted core)."""

from mcp.types import TextContent, Tool

from ..gdb_decode import registers_summary
from ..stack_analysis import stack_report
from ..tool_response import content_success
from .context import ToolContext
from .registry import register


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
    return [content_success(
        {"message": "Source listed", "location": arguments.get("location")},
        raw_response=resp,
    )]


@register(Tool(
    name="resolve_address",
    description="Maps an address or expression (e.g. '$pc', '0x08001234') to its source "
                "file:line and nearest symbol.",
    inputSchema={
        "type": "object",
        "properties": {
            "expr": {"type": "string", "description": "Address or expression to resolve, e.g. '$pc' or '0x08001234'."}
        },
        "required": ["expr"]
    }
))
def resolve_address(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    resp = ctx.gdb_client.resolve_address(arguments["expr"])
    return [content_success(
        {"message": "Address resolved", "expr": arguments["expr"]},
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
    return [content_success({"message": "Disassembled"}, raw_response=resp)]


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
    return [content_success({"message": "Functions listed"}, raw_response=resp)]


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
    return [content_success({"message": "Variables listed"}, raw_response=resp)]


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
    return [content_success({"message": "Type looked up", "expr": arguments["expr"]}, raw_response=resp)]


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
    return [content_success({"message": "Size evaluated", "expr": arguments["expr"]}, raw_response=resp)]


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
    return [content_success({"message": "Address resolved", "symbol": arguments["symbol"]}, raw_response=resp)]
