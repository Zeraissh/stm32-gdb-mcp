import pytest
from mcp.types import Tool

from mcp_server import server as server_module  # noqa: F401  (triggers load_all)
from mcp_server.tools.registry import REGISTRY, TOOL_ORDER, register


def test_every_registered_tool_is_in_tool_order():
    unknown = set(REGISTRY) - set(TOOL_ORDER)
    assert not unknown, f"registered tools missing from TOOL_ORDER: {sorted(unknown)}"


def test_tool_order_has_no_duplicates():
    assert len(TOOL_ORDER) == len(set(TOOL_ORDER))


def test_registered_schema_name_matches_registry_key():
    for name, spec in REGISTRY.items():
        assert spec.tool.name == name


def test_duplicate_registration_is_rejected():
    existing = next(iter(REGISTRY))
    with pytest.raises(ValueError, match="duplicate tool registration"):
        register(Tool(name=existing, description="dup", inputSchema={"type": "object", "properties": {}}))(
            lambda ctx, args: []
        )
