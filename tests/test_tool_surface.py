import asyncio
import re

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

    # Per-action required args are what keep a family callable; the arguments
    # themselves are hoisted to the family's top-level properties.
    assert {"action", "channel"} <= set(logging_start["required"])
    assert set(logging_stop["required"]) == {"action", "channel"}
    assert {"action", "location"} <= set(breakpoint_set["required"])
    assert breakpoint_list["required"] == ["action"]
    assert {"port", "baudrate", "file", "command", "args"} <= set(
        tools["logging"].inputSchema["properties"]
    )


def test_every_tool_exposes_session():
    for tool in _tools().values():
        assert tool.inputSchema["properties"]["session"]["type"] == "string"


def test_no_tool_advertises_a_per_tool_output_schema():
    # The shared envelope lives in tool_response.OUTPUT_SCHEMA and the server
    # instructions; repeating it per tool cost ~460 chars x every tool.
    # getattr, not attribute access: the mcp dependency floor (1.7.0) predates
    # the outputSchema field entirely, so it is absent rather than None there.
    for tool in _tools().values():
        assert getattr(tool, "outputSchema", None) is None


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


# --- the arg_help contract ----------------------------------------------------


def _arg_group(arg_help, choice):
    """The 'choice(...)' argument list from an arg_help string, if it has one."""
    match = re.search(rf"\b{re.escape(choice)}\(([^)]*)\)", arg_help)
    return match.group(1) if match else None


def test_merged_family_arg_help_names_real_arguments():
    """Every argument a family's description names must exist in that action's schema.

    These strings are how the model learns to call an action. Naming an argument
    that does not exist (e.g. "watch(expression)" when the schema wants
    location+access_type) makes every such call fail validation and cost a retry.
    """
    from mcp_server.tool_surface import MERGED
    from mcp_server.tools.registry import REGISTRY

    problems = []
    for family, (_disc, mapping, _summary, arg_help) in MERGED.items():
        for choice, tool_name in mapping.items():
            spec = REGISTRY.get(tool_name)
            if spec is None:
                continue
            schema = spec.tool.inputSchema
            properties = set(schema.get("properties", {}))
            required = [p for p in schema.get("required", [])]
            group = _arg_group(arg_help, choice)

            if group is None:
                if required:
                    problems.append(f"{family}({choice}) hides required args {required}")
                continue

            named = set(re.findall(r"[a-z_][a-z0-9_]*", group))
            unreal = named - properties
            if unreal:
                problems.append(f"{family}({choice}) names non-existent args {sorted(unreal)}")

            # Required args must be listed, and outside the [optional] bracket.
            mandatory_part = group.split("[")[0]
            listed = set(re.findall(r"[a-z_][a-z0-9_]*", mandatory_part))
            missing = [p for p in required if p not in listed]
            if missing:
                problems.append(f"{family}({choice}) omits required args {missing}")

    assert not problems, "arg_help drifted from the real schemas:\n  " + "\n  ".join(problems)


def test_merged_tools_hoist_shared_properties_instead_of_repeating_them():
    # Every branch used to repeat the whole property map (with descriptions and
    # session), which made these the largest schemas advertised.
    logging_tool = _tools()["logging"]

    assert {"channel", "port", "baudrate", "limit"} <= set(logging_tool.inputSchema["properties"])
    for branch in logging_tool.inputSchema["oneOf"]:
        assert set(branch["properties"]) == {"action"}, "branch should carry only its discriminator"

    # A property two actions genuinely mean differently stays in its branch, so
    # "where to break" is not collapsed into "what to watch".
    breakpoint_tool = _tools()["breakpoint"]
    watch = _branch(breakpoint_tool, "action", "watch")
    assert "watch" in watch["properties"]["location"]["description"].lower()
    assert "break at" in breakpoint_tool.inputSchema["properties"]["location"]["description"].lower()
