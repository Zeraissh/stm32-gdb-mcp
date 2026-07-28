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


def test_no_tool_advertises_a_per_tool_output_schema():
    # The shared envelope lives in tool_response.OUTPUT_SCHEMA and the server
    # instructions; repeating it per tool cost ~460 chars x every tool.
    for tool in _tools().values():
        assert tool.outputSchema is None


def test_tool_annotations_distinguish_read_write_and_external_actions():
    tools = _tools()

    assert tools["read_memory"].annotations.readOnlyHint is True
    assert tools["read_memory"].annotations.destructiveHint is False
    assert tools["flash_firmware"].annotations.readOnlyHint is False
    assert tools["flash_firmware"].annotations.destructiveHint is True
    assert tools["report_issue"].annotations.readOnlyHint is False
    assert tools["report_issue"].annotations.openWorldHint is True
    assert tools["detect_probe"].annotations.readOnlyHint is True
    assert tools["self_check"].annotations.readOnlyHint is False
    assert tools["self_check"].annotations.destructiveHint is False
    assert tools["session_diagnostics"].annotations.readOnlyHint is False


def test_every_advertised_tool_has_risk_annotations():
    assert all(tool.annotations is not None for tool in _tools().values())


def test_generic_dispatch_and_custom_build_are_conservatively_annotated():
    tools = _tools()

    for name in ("call", "batch", "run_scenario"):
        assert tools[name].annotations.destructiveHint is True
        assert tools[name].annotations.openWorldHint is True

    assert tools["build_firmware"].annotations.readOnlyHint is False
    assert tools["build_firmware"].annotations.openWorldHint is True
    assert tools["logging"].annotations.readOnlyHint is False
