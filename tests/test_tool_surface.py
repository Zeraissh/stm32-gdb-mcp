import asyncio

from mcp_server.server import handle_list_tools


def _tools():
    return {tool.name: tool for tool in asyncio.run(handle_list_tools())}


def _branch(tool, discriminator, choice):
    return next(
        branch
        for branch in tool.inputSchema["oneOf"]
        if branch["properties"][discriminator].get("const") == choice
    )


def test_merged_tools_expose_action_specific_schemas():
    tools = _tools()

    logging_start = _branch(tools["logging"], "action", "start")
    logging_stop = _branch(tools["logging"], "action", "stop")
    breakpoint_set = _branch(tools["breakpoint"], "action", "set")
    breakpoint_list = _branch(tools["breakpoint"], "action", "list")

    assert {"action", "channel"} <= set(logging_start["required"])
    assert {"port", "baudrate", "file", "command", "args"} <= set(logging_start["properties"])
    assert set(logging_stop["required"]) == {"action", "channel"}
    assert {"action", "location"} <= set(breakpoint_set["required"])
    assert breakpoint_list["required"] == ["action"]


def test_every_tool_exposes_session():
    for tool in _tools().values():
        assert tool.inputSchema["properties"]["session"]["type"] == "string"


def test_every_tool_exposes_shared_output_schema():
    for tool in _tools().values():
        assert tool.outputSchema["type"] == "object"
        assert {"ok", "data", "error", "raw_response", "suggested_next_actions"} <= set(
            tool.outputSchema["properties"]
        )


def test_tool_annotations_distinguish_read_write_and_external_actions():
    tools = _tools()

    assert tools["read_memory"].annotations.readOnlyHint is True
    assert tools["read_memory"].annotations.destructiveHint is False
    assert tools["flash_firmware"].annotations.readOnlyHint is False
    assert tools["flash_firmware"].annotations.destructiveHint is True
    assert tools["report_issue"].annotations.readOnlyHint is False
    assert tools["report_issue"].annotations.openWorldHint is True
