"""Debug profile / YAML config / project discovery tools (session setup surface)."""

from mcp.types import TextContent, Tool

from ..debug_config import (
    load_debug_config as load_debug_config_file,
)
from ..debug_config import (
    save_debug_config as save_debug_config_file,
)
from ..debug_config import (
    validate_debug_config as validate_debug_config_data,
)
from ..project_inspector import inspect_project as inspect_project_dir
from ..tool_response import content_success
from .context import ToolContext
from .registry import register


@register(Tool(
    name="set_debug_profile",
    description=(
        "Stores board/session defaults such as MCU, probe, GDB server args, ELF path, and SVD path. "
        "Top-level fields you omit keep their value, but a nested block you pass (hub, rtt, uart, swo, "
        "reset, hil) REPLACES the stored one whole -- pass merge=true to deep-merge into it instead."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "mcu": {"type": "string"},
            "board": {"type": "string"},
            "probe": {"type": "string"},
            "server_type": {"type": "string", "enum": ["openocd", "stlink", "jlink"]},
            "server_args": {"type": "array", "items": {"type": "string"}},
            "elf_path": {"type": "string"},
            "svd_path": {"type": "string"},
            "project_root": {"type": "string"},
            "hub": {
                "type": "object",
                "description": (
                    "Programmable USB hub binding: {port, serial, channel, map, power_cycle, guard}. "
                    "channel is 1-based and selects which port this session's board sits on."
                ),
            },
            "notes": {"type": "string"},
            "merge": {
                "type": "boolean",
                "description": (
                    "Deep-merge nested blocks (hub, rtt, uart, swo, reset, hil) into the stored ones "
                    "instead of replacing them -- use it to add one key, e.g. a hub.map label, without "
                    "dropping the identity hub(action=discover) wrote. Leave it off to replace, which "
                    "is the only way to remove a stale entry."
                ),
            }
        }
    }
))
def set_debug_profile(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    # merge is a control flag, not a profile field: forwarding it would trip the
    # store's unknown-field check (ALLOWED_FIELDS).
    values = {key: value for key, value in arguments.items() if key != "merge"}
    profile = ctx.debug_profile.update(values, merge=bool(arguments.get("merge")))
    svd_path = profile.get("svd_path")
    if svd_path:
        ctx.svd_parser.load(svd_path)
    return [content_success(profile)]


@register(Tool(
    name="get_debug_profile",
    description="Returns the stored board/session defaults.",
    inputSchema={"type": "object", "properties": {}}
))
def get_debug_profile(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    return [content_success(ctx.debug_profile.get())]


@register(Tool(
    name="load_debug_config",
    description=(
        "Loads a YAML debug config and applies compatible fields to the active debug profile. "
        "Fields the file defines REPLACE the stored ones, so re-loading a rack config after re-cabling "
        "never accumulates the previous rig's hub.map channels; fields it omits are left alone."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to a YAML debug config file."}
        },
        "required": ["path"]
    }
))
def load_debug_config(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    result = load_debug_config_file(arguments["path"])
    if result["validation"]["valid"]:
        ctx.debug_profile.update({
            key: value
            for key, value in result["config"].items()
            if key in ctx.debug_profile.ALLOWED_FIELDS
        })
        svd_path = result["config"].get("svd_path")
        if svd_path:
            ctx.svd_parser.load(svd_path)
    return [content_success(result)]


@register(Tool(
    name="save_debug_config",
    description="Saves a YAML debug config file.",
    inputSchema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Destination YAML config path."},
            "config": {"type": "object", "description": "Config object to save."}
        },
        "required": ["path", "config"]
    }
))
def save_debug_config(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    result = save_debug_config_file(arguments["path"], arguments["config"])
    return [content_success(result)]


@register(Tool(
    name="validate_debug_config",
    description="Validates a YAML debug config object without saving it.",
    inputSchema={
        "type": "object",
        "properties": {
            "config": {"type": "object", "description": "Config object to validate."}
        },
        "required": ["config"]
    }
))
def validate_debug_config(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    result = validate_debug_config_data(arguments["config"])
    return [content_success(result)]


@register(Tool(
    name="inspect_project",
    description="Discovers firmware project artifacts such as ELF, map, linker script, SVD, and STM32CubeMX .ioc metadata.",
    inputSchema={
        "type": "object",
        "properties": {
            "project_root": {"type": "string", "description": "Project directory to scan. Uses debug profile project_root if omitted."}
        }
    }
))
def inspect_project(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    profile = ctx.debug_profile.get()
    result = inspect_project_dir(arguments.get("project_root") or profile.get("project_root"), profile)
    return [content_success(result)]
