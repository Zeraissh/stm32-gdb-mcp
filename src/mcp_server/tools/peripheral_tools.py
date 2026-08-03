"""SVD-backed peripheral register tools (load, read, and bitfield-decode)."""

from mcp.types import TextContent, Tool

from ..tool_response import content_success
from ._helpers import core_state
from .context import ToolContext
from .registry import register


@register(Tool(
    name="load_svd",
    description="Loads an SVD file for peripheral parsing.",
    inputSchema={
        "type": "object",
        "properties": {
            "filepath": {"type": "string", "description": "Path to the .svd file."}
        },
        "required": ["filepath"]
    }
))
def load_svd(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    ctx.svd_parser.load(arguments["filepath"])
    return [content_success({"message": "SVD file loaded successfully", "filepath": arguments["filepath"]})]


@register(Tool(
    name="read_peripheral_register",
    description="Reads a peripheral register using its name from the loaded SVD.",
    inputSchema={
        "type": "object",
        "properties": {
            "peripheral": {"type": "string", "description": "Peripheral name (e.g., 'GPIOA')."},
            "register": {"type": "string", "description": "Register name (e.g., 'ODR')."}
        },
        "required": ["peripheral", "register"]
    }
))
def read_peripheral_register(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    addr = ctx.svd_parser.get_register_address(arguments["peripheral"], arguments["register"])
    resp = ctx.gdb_client.read_memory(hex(addr), 4)  # Assuming 32-bit register
    return [content_success(
        {
            "message": "Peripheral register read",
            "peripheral": arguments["peripheral"],
            "register": arguments["register"],
            "address": hex(addr),
        },
        raw_response=resp,
        core_state=core_state(ctx.gdb_client),
    )]


@register(Tool(
    name="decode_peripheral_register",
    description="Reads and decodes a peripheral register with SVD bitfield names and enumerated values.",
    inputSchema={
        "type": "object",
        "properties": {
            "peripheral": {"type": "string", "description": "Peripheral name (e.g., 'GPIOA')."},
            "register": {"type": "string", "description": "Register name (e.g., 'MODER')."}
        },
        "required": ["peripheral", "register"]
    }
))
def decode_peripheral_register(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    register = ctx.svd_parser.get_register(arguments["peripheral"], arguments["register"])
    resp = ctx.gdb_client.read_typed_memory(hex(register["address_int"]), width_bits=register["size"], count=1)
    value = ctx.gdb_client._extract_first_memory_word(resp)
    decoded = ctx.svd_parser.decode_register_value(arguments["peripheral"], arguments["register"], value)
    decoded["raw_response"] = resp
    return [content_success(decoded, raw_response=resp)]
