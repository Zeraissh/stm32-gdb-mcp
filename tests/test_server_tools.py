import asyncio
import json

from mcp_server.server import handle_call_tool, handle_list_tools


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


def test_server_exposes_swo_log_tools():
    tools = asyncio.run(handle_list_tools())
    tool_names = {tool.name for tool in tools}

    assert "start_swo_logging" in tool_names
    assert "stop_swo_logging" in tool_names
    assert "get_swo_logs" in tool_names
    assert "clear_swo_logs" in tool_names


def _payload(result):
    return json.loads(result[0].text)


def test_get_debug_profile_returns_stable_json_envelope():
    payload = _payload(asyncio.run(handle_call_tool("get_debug_profile", {})))

    assert payload["ok"] is True
    assert isinstance(payload["data"], dict)
    assert payload["error"] is None


def test_unknown_tool_returns_stable_json_error_envelope():
    payload = _payload(asyncio.run(handle_call_tool("does_not_exist", {})))

    assert payload["ok"] is False
    assert payload["error"]["code"] == "tool_execution_error"
    assert "Unknown tool" in payload["error"]["message"]


def test_server_exposes_run_and_wait_tools():
    tools = asyncio.run(handle_list_tools())
    tool_names = {tool.name for tool in tools}

    assert "run_and_wait" in tool_names
    assert "wait_for_stop" in tool_names


def test_server_exposes_frame_navigation_tools():
    tools = asyncio.run(handle_list_tools())
    tool_names = {tool.name for tool in tools}

    assert "select_frame" in tool_names
    assert "read_frame_variables" in tool_names
    assert "list_source" in tool_names
    assert "resolve_address" in tool_names


def test_read_frame_variables_passes_level_to_client(monkeypatch):
    import mcp_server.server as server_module

    class FakeClient:
        def __init__(self):
            self.calls = []

        def read_frame_variables(self, level):
            self.calls.append(level)
            return [{"message": "done"}]

    fake = FakeClient()
    monkeypatch.setattr(server_module, "gdb_client", fake)
    payload = _payload(asyncio.run(handle_call_tool("read_frame_variables", {"level": 1})))

    assert payload["ok"] is True
    assert fake.calls == [1]


def test_run_and_wait_returns_structured_stop_event(monkeypatch):
    import mcp_server.server as server_module

    class FakeClient:
        def run_and_wait(self, timeout_sec):
            assert timeout_sec == 5.0
            return {
                "stopped": True,
                "reason": "breakpoint-hit",
                "signal": None,
                "breakpoint_id": "2",
                "frame": {"func": "main", "file": "main.c", "line": 42, "addr": "0x08001000"},
                "raw_response": [],
            }

    monkeypatch.setattr(server_module, "gdb_client", FakeClient())
    payload = _payload(asyncio.run(handle_call_tool("run_and_wait", {"timeout_sec": 5.0})))

    assert payload["ok"] is True
    assert payload["data"]["reason"] == "breakpoint-hit"
    assert payload["data"]["frame"]["line"] == 42
