"""Board tools: netlist import, board views/validation, and device-fact packs."""

import os

from mcp.types import TextContent, Tool

from .. import device_packs
from ..board_model import board_view, summarize_board
from ..board_validation import load_capability_db
from ..board_validation import validate_board as validate_board_report
from ..netlist_parser import load_netlist_file, parse_netlist
from ..tool_response import content_error, content_success
from .context import ToolContext
from .registry import register


@register(Tool(
    name="import_netlist",
    description="Parse a schematic netlist (KiCad .net today) into a machine-readable "
                "BoardDescription: the MCU part/family/line, a per-pin map (package pin -> "
                "port pin -> net -> inferred peripheral function), and the power/ground nets. "
                "This is the input contract for automated framework design. Pass 'path' or "
                "'text'. The result is stored on the session; read views with describe_board.",
    inputSchema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to a netlist file (e.g. board.net)."},
            "text": {"type": "string", "description": "Netlist contents inline (alternative to path)."},
            "format": {"type": "string", "description": "Netlist format: auto (default) or kicad."},
            "session": {"type": "string", "description": "Target session id (default 'default')."}
        }
    }
))
def import_netlist(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    text = arguments.get("text")
    path = arguments.get("path")
    if not text and not path:
        return [content_error(
            "import_netlist needs 'path' or 'text'.", code="missing_argument",
            suggested_next_actions=["import_netlist(path='board.net')"])]
    fmt = arguments.get("format", "auto")
    try:
        parsed = (parse_netlist(text, fmt=fmt, source="<text>") if text
                  else load_netlist_file(path or "", fmt=fmt))
    except (ValueError, OSError) as e:
        return [content_error(
            str(e), code="netlist_parse_error",
            suggested_next_actions=["import_netlist with format=kicad"])]
    ctx.board["current"] = parsed
    return [content_success(
        summarize_board(parsed),
        suggested_next_actions=["describe_board (what=pins)", "describe_board (what=peripherals)"])]


@register(Tool(
    name="describe_board",
    description="Read the BoardDescription imported by import_netlist. what=summary (MCU + "
                "peripherals + counts), pins (full MCU pin map), nets, power (power/ground "
                "nets), or peripherals (distinct peripherals in use).",
    inputSchema={
        "type": "object",
        "properties": {
            "what": {"type": "string", "description": "summary|pins|nets|power|peripherals (default summary)."},
            "session": {"type": "string", "description": "Target session id (default 'default')."}
        }
    }
))
def describe_board(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    parsed = ctx.board.get("current")
    if not parsed:
        return [content_error(
            "No board imported for this session. Run import_netlist first.", code="no_board",
            suggested_next_actions=["import_netlist(path='board.net')"])]
    what = arguments.get("what", "summary")
    view = board_view(parsed, what)
    if view is None:
        return [content_error(
            f"Unknown view '{what}'.", code="invalid_argument",
            suggested_next_actions=["describe_board (what=summary|pins|nets|power|peripherals)"])]
    return [content_success(view, suggested_next_actions=["describe_board (what=pins)"])]


@register(Tool(
    name="validate_board",
    description="Validate the imported BoardDescription: detect a package pin wired to "
                "multiple nets (short), a peripheral signal routed to multiple pins, a port "
                "pin driven by multiple nets, and missing power/ground/debug/reset nets. With "
                "a pin-capability DB (db_path or the STM32_GDB_MCP_PIN_DB env) it also checks "
                "alternate-function legality; unknown pins degrade to 'unverified', never a "
                "false conflict. Run import_netlist first.",
    inputSchema={
        "type": "object",
        "properties": {
            "db_path": {"type": "string", "description": "Optional JSON pin-capability DB (CubeMX-derived) for AF-legality checks."},
            "session": {"type": "string", "description": "Target session id (default 'default')."}
        }
    }
))
def validate_board(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    parsed = ctx.board.get("current")
    if not parsed:
        return [content_error(
            "No board imported for this session. Run import_netlist first.", code="no_board",
            suggested_next_actions=["import_netlist(path='board.net')"])]
    capability_db = None
    db_path = arguments.get("db_path") or os.environ.get("STM32_GDB_MCP_PIN_DB")
    if db_path:
        try:
            capability_db = load_capability_db(db_path)
        except (OSError, ValueError) as e:
            return [content_error(
                f"Failed to load pin-capability DB: {e}", code="db_load_error",
                suggested_next_actions=["validate_board without db_path"])]
    report = validate_board_report(parsed, capability_db)
    actions = ["describe_board (what=pins)"] if not report["ok"] else ["describe_board (what=peripherals)"]
    if not report["af_checked"]:
        actions.append("validate_board(db_path=...) to also check alternate-function legality")
    return [content_success(report, suggested_next_actions=actions)]


@register(Tool(
    name="load_device_pack",
    description="Register a verified device-fact pack (Pillar F) so the deterministic solvers cover a new "
                "STM32 family: its DMA request routing, irregular NVIC vectors, clock PLL profile, and "
                "timer bus/width. Facts are DATA, never guessed -- STM32F4/L4 ship built-in; add a family "
                "by supplying a validated pack (schema 'stm32-device-pack/v1') via path= (a JSON file) or "
                "inline pack=. Call with no arguments to report current coverage. Honest by design: a "
                "malformed pack is rejected with the list of problems and never half-loaded; shadowing a "
                "built-in family needs allow_override=true.",
    inputSchema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to a device-pack JSON file to load and register."},
            "pack": {"type": "object", "description": "Inline device-pack object (takes precedence over path)."},
            "allow_override": {"type": "boolean", "description": "Permit shadowing a built-in family pack (default false)."}
        }
    }
))
def load_device_pack(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    pack_arg = arguments.get("pack")
    path = arguments.get("path")
    allow_override = bool(arguments.get("allow_override"))
    if pack_arg is None and not path:
        # No pack supplied -> report which families the deterministic solvers currently cover.
        return [content_success({
            "action": "coverage",
            "coverage": device_packs.coverage(),
        }, suggested_next_actions=[
            "load_device_pack(path='pack.json')", "load_device_pack(pack={...})"])]
    if pack_arg is not None and not isinstance(pack_arg, dict):
        return [content_error(
            "pack must be a device-pack object.", code="invalid_argument",
            suggested_next_actions=["load_device_pack(path='pack.json')"])]
    if pack_arg is None:
        pack_arg, read_problems = device_packs.load_pack(path)
        if pack_arg is None:
            return [content_error(
                "Could not read device pack: " + "; ".join(read_problems), code="pack_unreadable",
                raw_response={"problems": read_problems},
                suggested_next_actions=["load_device_pack(path=<valid json file>)"])]
    problems = device_packs.register_pack(pack_arg, allow_override=allow_override)
    if problems:
        return [content_error(
            "Device pack rejected: " + "; ".join(problems), code="invalid_pack",
            raw_response={"problems": problems},
            suggested_next_actions=["Fix the reported problems and retry"])]
    return [content_success({
        "action": "registered",
        "family": pack_arg.get("family"),
        "sections": sorted(k for k in ("clock", "dma", "nvic", "timer") if k in pack_arg),
        "coverage": device_packs.coverage(),
    }, suggested_next_actions=[
        "design_framework", "solve_clock_tree", "synthesize_acceptance"])]
