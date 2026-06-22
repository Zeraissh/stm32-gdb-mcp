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


def test_server_exposes_tier3_depth_tools():
    tools = asyncio.run(handle_list_tools())
    tool_names = {tool.name for tool in tools}

    for expected in (
        "step_out", "step_instruction", "run_to_line", "disassemble",
        "list_functions", "list_variables", "lookup_type", "sizeof", "address_of",
        "capture_coredump", "load_coredump", "verify_flash",
        "read_cycle_counter", "sample_pc",
    ):
        assert expected in tool_names


def test_read_cycle_counter_enables_then_reads(monkeypatch):
    import mcp_server.server as server_module

    class FakeClient:
        def __init__(self):
            self.enabled = False

        def enable_cycle_counter(self):
            self.enabled = True

        def read_cycle_counter(self):
            return 4242

    fake = FakeClient()
    monkeypatch.setattr(server_module, "gdb_client", fake)

    payload = _payload(asyncio.run(handle_call_tool("read_cycle_counter", {"enable": True})))

    assert payload["ok"] is True
    assert payload["data"]["cycles"] == 4242
    assert fake.enabled is True


def test_server_exposes_check_session_health_tool():
    tools = asyncio.run(handle_list_tools())
    tool_names = {tool.name for tool in tools}

    assert "check_session_health" in tool_names


def test_check_session_health_reports_status(monkeypatch):
    import mcp_server.server as server_module

    class FakeClient:
        def is_alive(self):
            return True

        def probe_target(self):
            return False

    class FakeManager:
        server_type = "openocd"
        port = 3333

        def is_alive(self):
            return True

    monkeypatch.setattr(server_module, "gdb_client", FakeClient())
    monkeypatch.setattr(server_module, "gdb_manager", FakeManager())

    payload = _payload(asyncio.run(handle_call_tool("check_session_health", {})))

    assert payload["ok"] is True
    assert payload["data"]["gdb_alive"] is True
    assert payload["data"]["target_responsive"] is False
    assert "start_debug_session" in payload["suggested_next_actions"]


def test_server_exposes_configure_debug_freeze_tool():
    tools = asyncio.run(handle_list_tools())
    tool_names = {tool.name for tool in tools}

    assert "configure_debug_freeze" in tool_names


def test_configure_debug_freeze_plans_and_applies_writes(monkeypatch):
    import mcp_server.server as server_module

    class FakeClient:
        def __init__(self):
            self.writes = []

        def read_word(self, address):
            return 0x00000001  # some unrelated bit already set

        def write_memory(self, address, value):
            self.writes.append((address, value))

    fake = FakeClient()
    monkeypatch.setattr(server_module, "gdb_client", fake)

    payload = _payload(asyncio.run(handle_call_tool(
        "configure_debug_freeze", {"family": "stm32l4", "peripherals": ["iwdg", "wwdg"]}
    )))

    assert payload["ok"] is True
    assert payload["data"]["applied"] is True
    plan = payload["data"]["plans"][0]
    # IWDG (bit12) + WWDG (bit11) OR-ed onto the existing bit0.
    assert plan["new_value"] == (0x1 | (1 << 12) | (1 << 11))
    assert fake.writes == [(hex(0xE0042008), hex(0x1 | (1 << 12) | (1 << 11)))]


def test_server_exposes_write_guard_tools():
    tools = asyncio.run(handle_list_tools())
    tool_names = {tool.name for tool in tools}

    assert "set_write_policy" in tool_names
    assert "get_write_audit_log" in tool_names


def test_write_to_protected_region_is_blocked_without_touching_client(monkeypatch):
    import mcp_server.server as server_module

    class FakeClient:
        def __init__(self):
            self.writes = []

        def write_memory(self, address, value):
            self.writes.append((address, value))
            return [{"message": "done"}]

    fake = FakeClient()
    monkeypatch.setattr(server_module, "gdb_client", fake)

    payload = _payload(asyncio.run(
        handle_call_tool("write_memory", {"address": "0x40003000", "value": "0x1"})
    ))

    assert payload["ok"] is False
    assert payload["error"]["code"] == "memory_write_blocked"
    assert fake.writes == []  # guard prevented the hardware write


def test_normal_ram_write_passes_through_guard(monkeypatch):
    import mcp_server.server as server_module

    class FakeClient:
        def __init__(self):
            self.writes = []

        def write_memory(self, address, value):
            self.writes.append((address, value))
            return [{"message": "done"}]

    fake = FakeClient()
    monkeypatch.setattr(server_module, "gdb_client", fake)

    payload = _payload(asyncio.run(
        handle_call_tool("write_memory", {"address": "0x20000000", "value": "0x5"})
    ))

    assert payload["ok"] is True
    assert fake.writes == [("0x20000000", "0x5")]


def test_server_exposes_reconstruct_fault_context_tool():
    tools = asyncio.run(handle_list_tools())
    tool_names = {tool.name for tool in tools}

    assert "reconstruct_fault_context" in tool_names


def test_set_breakpoint_exposes_condition_and_temporary_options():
    tools = asyncio.run(handle_list_tools())
    bp_tool = next(tool for tool in tools if tool.name == "set_breakpoint")
    properties = bp_tool.inputSchema["properties"]

    assert "condition" in properties
    assert "temporary" in properties
    assert "ignore_count" in properties


def test_server_exposes_frame_navigation_tools():
    tools = asyncio.run(handle_list_tools())
    tool_names = {tool.name for tool in tools}

    assert "select_frame" in tool_names
    assert "read_frame_variables" in tool_names
    assert "list_source" in tool_names
    assert "resolve_address" in tool_names


def test_read_frame_variables_returns_decoded_map(monkeypatch):
    import mcp_server.server as server_module

    class FakeClient:
        def __init__(self):
            self.calls = []

        def read_frame_variables_decoded(self, level):
            self.calls.append(level)
            return {"i": "42", "g_divisor": "0"}

    fake = FakeClient()
    monkeypatch.setattr(server_module, "gdb_client", fake)
    payload = _payload(asyncio.run(handle_call_tool("read_frame_variables", {"level": 1})))

    assert payload["ok"] is True
    assert fake.calls == [1]
    assert payload["data"]["variables"] == {"i": "42", "g_divisor": "0"}
    assert payload["raw_response"] is None  # raw is opt-in for token economy


def test_self_check_reports_decoded_identity(monkeypatch):
    import mcp_server.server as server_module

    class FakeClient:
        def read_word(self, address):
            return {0xE000ED00: 0x410FC241, 0xE0042000: 0x10016435}[address]

    class FakeProfile:
        def get(self):
            return {"mcu": "STM32L431"}

    monkeypatch.setattr(server_module, "gdb_client", FakeClient())
    monkeypatch.setattr(server_module, "debug_profile", FakeProfile())

    payload = _payload(asyncio.run(handle_call_tool("self_check", {})))

    assert payload["data"]["ok"] is True
    assert payload["data"]["core"] == "Cortex-M4"


def test_tool_error_is_classified_with_actionable_code(monkeypatch):
    import mcp_server.server as server_module

    class FakeClient:
        def read_core_registers_decoded(self):
            raise RuntimeError("Did not get response from gdb after 1.0 seconds")

    monkeypatch.setattr(server_module, "gdb_client", FakeClient())
    payload = _payload(asyncio.run(handle_call_tool("read_core_registers", {})))

    assert payload["ok"] is False
    assert payload["error"]["code"] == "target_unresponsive"
    assert "halt_execution" in payload["suggested_next_actions"]


def test_server_journals_tool_calls_and_exposes_journal():
    import mcp_server.server as server_module

    server_module.session_journal.clear()
    asyncio.run(handle_call_tool("get_debug_profile", {}))

    payload = _payload(asyncio.run(handle_call_tool("get_session_journal", {})))

    tools_recorded = [e["tool"] for e in payload["data"]["entries"]]
    assert "get_debug_profile" in tools_recorded
    # the journal-reading tool itself must not be journaled
    assert "get_session_journal" not in tools_recorded
    assert payload["data"]["entries"][0]["duration_ms"] is not None


def test_export_debug_report_writes_artifact(tmp_path):
    import json as _json

    import mcp_server.server as server_module

    server_module.session_journal.clear()
    asyncio.run(handle_call_tool("get_debug_profile", {}))

    path = tmp_path / "report.json"
    payload = _payload(asyncio.run(handle_call_tool("export_debug_report", {"path": str(path)})))

    assert payload["ok"] is True
    assert payload["data"]["path"] == str(path)
    report = _json.loads(path.read_text(encoding="utf-8"))
    assert report["run_id"] == server_module.session_journal.run_id
    assert "metrics" in report and "journal" in report


def test_session_metrics_and_timeline_reflect_calls():
    import mcp_server.server as server_module

    server_module.session_journal.clear()
    asyncio.run(handle_call_tool("get_debug_profile", {}))
    asyncio.run(handle_call_tool("get_debug_profile", {}))

    metrics = _payload(asyncio.run(handle_call_tool("get_session_metrics", {})))
    assert metrics["data"]["by_tool"]["get_debug_profile"]["calls"] == 2
    assert metrics["data"]["totals"]["calls"] == 2

    timeline = _payload(asyncio.run(handle_call_tool("get_session_timeline", {})))
    assert any("get_debug_profile" in line for line in timeline["data"]["timeline"])


def test_run_scenario_replays_steps_and_reports(monkeypatch):
    import mcp_server.server as server_module

    class FakeProfile:
        def get(self):
            return {"mcu": "STM32L431"}

    monkeypatch.setattr(server_module, "debug_profile", FakeProfile())

    steps = [
        {"tool": "get_debug_profile", "args": {}},
        {"tool": "get_debug_profile", "args": {}},
    ]
    payload = _payload(asyncio.run(handle_call_tool("run_scenario", {"steps": steps})))

    assert payload["ok"] is True
    assert payload["data"]["ok"] is True
    assert payload["data"]["passed"] == 2
    assert payload["data"]["total"] == 2


def test_server_exposes_composite_tools():
    tools = asyncio.run(handle_list_tools())
    tool_names = {tool.name for tool in tools}

    assert "debug_until" in tool_names
    assert "capture_state" in tool_names
    assert "flash_and_run" in tool_names


def test_read_core_registers_returns_decoded_map_and_summary(monkeypatch):
    import mcp_server.server as server_module

    class FakeClient:
        def read_core_registers_decoded(self):
            return {"pc": "0x8000058", "lr": "0xfffffff9", "sp": "0x200040b0"}

    monkeypatch.setattr(server_module, "gdb_client", FakeClient())
    payload = _payload(asyncio.run(handle_call_tool("read_core_registers", {})))

    assert payload["data"]["registers"]["pc"] == "0x8000058"
    assert "pc=0x8000058" in payload["data"]["summary"]


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
