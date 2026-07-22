"""Breakpoint and watchpoint tools (hypothesis traps on the running target)."""

from mcp.types import TextContent, Tool

from ..tool_response import content_success
from .context import ToolContext
from .registry import register


@register(Tool(
    name="set_breakpoint",
    description="Sets a breakpoint at a function, line, or address. Supports an optional "
                "condition (break only when true), temporary (auto-delete on first hit), "
                "and ignore_count (skip N hits) so the AI can set a hypothesis trap and resume.",
    inputSchema={
        "type": "object",
        "properties": {
            "location": {"type": "string", "description": "Location to break at, e.g., 'main', 'main.c:42', or '*0x08001000'."},
            "condition": {"type": "string", "description": "Optional C expression; break only when it is non-zero, e.g. 'count > 5'."},
            "temporary": {"type": "boolean", "description": "If true, the breakpoint is deleted after its first hit."},
            "ignore_count": {"type": "integer", "description": "Number of hits to ignore before stopping."}
        },
        "required": ["location"]
    }
))
def set_breakpoint(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    resp = ctx.gdb_client.set_breakpoint(
        arguments["location"],
        condition=arguments.get("condition"),
        temporary=arguments.get("temporary", False),
        ignore_count=arguments.get("ignore_count"),
    )
    return [content_success(
        {
            "message": "Breakpoint set",
            "location": arguments["location"],
            "condition": arguments.get("condition"),
            "temporary": arguments.get("temporary", False),
        },
        raw_response=resp,
        suggested_next_actions=["run_and_wait"],
    )]


@register(Tool(
    name="list_breakpoints",
    description="Lists breakpoints with their HIT COUNTS. A hit_count of 0 means the code "
                "path was never reached — so a run_and_wait timeout means the precondition "
                "(a flag/state) to get there was not satisfied. Use this instead of retrying.",
    inputSchema={"type": "object", "properties": {}}
))
def list_breakpoints(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    bps = ctx.gdb_client.list_breakpoints_decoded()
    never_hit = [b["number"] for b in bps if b.get("hit_count") == 0]
    summary = (f"{len(bps)} breakpoints; never reached (hit_count=0): {never_hit}"
               if never_hit else f"{len(bps)} breakpoints, all reached at least once")
    return [content_success({"breakpoints": bps, "summary": summary})]


@register(Tool(
    name="delete_breakpoint",
    description="Deletes a breakpoint by its ID.",
    inputSchema={
        "type": "object",
        "properties": {
            "breakpoint_id": {"type": "string", "description": "ID of the breakpoint to delete (e.g., '1')."}
        },
        "required": ["breakpoint_id"]
    }
))
def delete_breakpoint(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    resp = ctx.gdb_client.delete_breakpoint(arguments["breakpoint_id"])
    return [content_success(
        {"message": "Breakpoint deleted", "breakpoint_id": arguments["breakpoint_id"]},
        raw_response=resp,
    )]


@register(Tool(
    name="set_watchpoint",
    description="Sets a hardware watchpoint on a memory address or variable.",
    inputSchema={
        "type": "object",
        "properties": {
            "location": {"type": "string", "description": "Variable or address to watch."},
            "access_type": {"type": "string", "enum": ["r", "w", "a"], "description": "Read (r), Write (w), or Access (a)."}
        },
        "required": ["location", "access_type"]
    }
))
def set_watchpoint(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    resp = ctx.gdb_client.set_watchpoint(arguments["location"], arguments["access_type"])
    return [content_success(
        {"message": "Watchpoint set", "location": arguments["location"], "access_type": arguments["access_type"]},
        raw_response=resp,
    )]
