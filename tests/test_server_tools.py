import asyncio

from mcp_server.server import handle_list_tools


def test_server_exposes_debug_closure_tools():
    tools = asyncio.run(handle_list_tools())
    tool_names = {tool.name for tool in tools}

    assert "capture_debug_snapshot" in tool_names
    assert "diagnose_fault" in tool_names
    assert "read_core_registers" in tool_names
    assert "get_gdb_server_logs" in tool_names
    assert "decode_peripheral_register" in tool_names
    assert "set_debug_profile" in tool_names
    assert "get_debug_profile" in tool_names
    assert "inspect_project" in tool_names
    assert "detect_rtos" in tool_names
    assert "read_current_task" in tool_names
    assert "read_freertos_tasks" in tool_names
    assert "read_freertos_task_lists" in tool_names
    assert "read_freertos_queue" in tool_names
    assert "read_freertos_mutex" in tool_names
    assert "read_freertos_heap" in tool_names
    assert "capture_rtos_snapshot" in tool_names
    assert "start_rtt_logging" in tool_names
    assert "stop_rtt_logging" in tool_names
    assert "get_rtt_logs" in tool_names
    assert "clear_rtt_logs" in tool_names
    assert "start_uart_logging" in tool_names
    assert "stop_uart_logging" in tool_names
    assert "get_uart_logs" in tool_names
    assert "clear_uart_logs" in tool_names
    assert "capture_expressions" in tool_names
    assert "assert_expressions" in tool_names
    assert "compare_expressions_after_action" in tool_names
    assert "load_debug_config" in tool_names
    assert "save_debug_config" in tool_names
    assert "validate_debug_config" in tool_names


def test_reset_target_exposes_strategy_and_custom_command_options():
    tools = asyncio.run(handle_list_tools())
    reset_tool = next(tool for tool in tools if tool.name == "reset_target")
    properties = reset_tool.inputSchema["properties"]

    assert "strategy" in properties
    assert "command" in properties
