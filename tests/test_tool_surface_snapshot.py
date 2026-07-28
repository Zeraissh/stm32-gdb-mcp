"""Golden snapshot of the advertised tool surface.

The v0.6.0 decomposition moves every Tool schema out of server.py; this snapshot
pins the complete advertised surface (names, order, descriptions, input schemas
including merged oneOf branches, annotations) plus the compact-mode name list,
so any behavioral drift fails loudly. Tools carry no per-tool outputSchema —
they share one response envelope, documented in the server instructions.

To regenerate after an INTENTIONAL surface change:
    STM32_GDB_MCP_REGEN_GOLDEN=1 python -m pytest tests/test_tool_surface_snapshot.py
"""

import asyncio
import json
import os
from pathlib import Path

from mcp_server.server import handle_list_tools

GOLDEN_PATH = Path(__file__).parent / "data" / "tool_surface_golden.json"


def _dump_tools():
    tools = asyncio.run(handle_list_tools())
    return [tool.model_dump(by_alias=True, exclude_none=True, mode="json") for tool in tools]


def _current_surface(monkeypatch):
    monkeypatch.delenv("STM32_GDB_MCP_COMPACT", raising=False)
    full = _dump_tools()
    monkeypatch.setenv("STM32_GDB_MCP_COMPACT", "1")
    compact_names = [tool["name"] for tool in _dump_tools()]
    monkeypatch.delenv("STM32_GDB_MCP_COMPACT", raising=False)
    return {"tools": full, "compact_names": compact_names}


def test_advertised_tool_surface_matches_golden(monkeypatch):
    surface = _current_surface(monkeypatch)

    if os.environ.get("STM32_GDB_MCP_REGEN_GOLDEN"):
        GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_PATH.write_text(json.dumps(surface, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))

    got_names = [t["name"] for t in surface["tools"]]
    want_names = [t["name"] for t in golden["tools"]]
    assert got_names == want_names, "advertised tool names/order drifted from golden"
    assert surface["compact_names"] == golden["compact_names"], "compact-mode surface drifted"

    for got, want in zip(surface["tools"], golden["tools"], strict=True):
        assert got == want, f"tool '{want['name']}' drifted from golden (schema/description/annotations)"
