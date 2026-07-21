import asyncio
import json
import threading
import time

from mcp_server import device_packs
from mcp_server.server import handle_call_tool, handle_list_tools
from mcp_server.tool_response import content_success


def test_server_provides_workflow_instructions():
    from mcp_server.server import server

    instructions = server.instructions
    assert instructions and "self_check" in instructions
    assert "HALTED" in instructions
    assert "recover_session" in instructions


def test_server_exposes_debug_closure_tools():
    tools = asyncio.run(handle_list_tools())
    tool_names = {tool.name for tool in tools}

    # First-class tools that survive consolidation.
    assert "diagnose_fault" in tool_names
    assert "decode_peripheral_register" in tool_names
    assert "inspect_project" in tool_names
    assert "detect_rtos" in tool_names
    assert "read_freertos" in tool_names

    # Action-dispatched families replace their single-purpose tools.
    assert "logging" in tool_names
    assert "expressions" in tool_names
    assert "debug_config" in tool_names
    assert "debug_profile" in tool_names
    assert "read_registers" in tool_names
    assert "snapshot" in tool_names
    assert "session_diagnostics" in tool_names

    # The merged-away singles are no longer advertised (still reachable via `call`).
    for gone in ("start_logging", "stop_logging", "get_logs", "clear_logs",
                 "capture_expressions", "assert_expressions", "compare_expressions_after_action",
                 "load_debug_config", "save_debug_config", "validate_debug_config",
                 "get_debug_profile", "set_debug_profile", "read_core_registers",
                 "capture_debug_snapshot", "capture_rtos_snapshot", "get_gdb_server_logs"):
        assert gone not in tool_names


def test_merged_family_routes_to_underlying_handler():
    # debug_profile(action=set|get) must behave exactly like the old tools.
    asyncio.run(handle_call_tool("debug_profile", {"action": "set", "mcu": "STM32F411"}))
    got = _payload(asyncio.run(handle_call_tool("debug_profile", {"action": "get"})))
    assert got["ok"] is True and got["data"]["mcu"] == "STM32F411"

    # missing discriminator -> clear error, not a crash.
    err = _payload(asyncio.run(handle_call_tool("debug_profile", {})))
    assert err["ok"] is False and err["error"]["code"] == "missing_argument"

    # the underlying name still works directly (back-compat for `call` / older agents).
    direct = _payload(asyncio.run(handle_call_tool("get_debug_profile", {})))
    assert direct["ok"] is True


def test_reset_target_exposes_strategy_and_custom_command_options():
    tools = asyncio.run(handle_list_tools())
    reset_tool = next(tool for tool in tools if tool.name == "reset_target")
    properties = reset_tool.inputSchema["properties"]

    assert "strategy" in properties
    assert "command" in properties


def test_unified_logging_tool_dispatches_by_action_and_channel():
    tools = asyncio.run(handle_list_tools())
    names = {t.name for t in tools}

    # one 'logging' tool with an action discriminator replaces start/stop/get/clear_logs
    log = next(t for t in tools if t.name == "logging")
    assert set(log.inputSchema["properties"]["action"]["enum"]) == {"start", "stop", "get", "clear"}

    # the 12 old per-channel logging tools and the 4 per-verb ones are gone (consolidated)
    for old in ("start_rtt_logging", "start_swo_logging", "start_uart_logging",
                "get_uart_logs", "clear_rtt_logs",
                "start_logging", "stop_logging", "get_logs", "clear_logs"):
        assert old not in names


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
    assert payload["error"]["code"] == "unknown_tool"
    assert "Unknown tool" in payload["error"]["message"]
    assert "call" in payload["suggested_next_actions"]


def test_renamed_tool_error_points_to_new_name():
    # issue #5: agent called the old logging name after consolidation.
    payload = _payload(asyncio.run(handle_call_tool("start_uart_logging", {"port": "COM3"})))
    assert payload["error"]["code"] == "unknown_tool"
    assert 'start_logging(channel="uart")' in payload["error"]["message"]


def test_missing_required_argument_gives_clear_error():
    # issue #5: read_variable without 'name' produced a cryptic KeyError.
    payload = _payload(asyncio.run(handle_call_tool("read_variable", {})))
    assert payload["ok"] is False
    assert payload["error"]["code"] == "missing_argument"
    assert "name" in payload["error"]["message"]


def test_read_variable_returns_structured_value(monkeypatch):
    import mcp_server.server as server_module

    class FakeClient:
        def read_variable(self, name):
            assert name == "rx_count"
            return [{"type": "result", "payload": {"value": "7"}}]

    monkeypatch.setattr(server_module, "gdb_client", FakeClient())
    payload = _payload(asyncio.run(handle_call_tool("read_variable", {"name": "rx_count"})))

    assert payload["ok"] is True
    assert payload["data"]["value"] == "7"


def test_read_variable_without_decoded_value_returns_error(monkeypatch):
    import mcp_server.server as server_module

    class FakeClient:
        def read_variable(self, name):
            return [{"type": "console", "payload": "noise"}]

    monkeypatch.setattr(server_module, "gdb_client", FakeClient())
    payload = _payload(asyncio.run(handle_call_tool("read_variable", {"name": "rx_count"})))

    assert payload["ok"] is False
    assert payload["error"]["code"] == "no_value_returned"


def test_read_memory_returns_structured_bytes(monkeypatch):
    import mcp_server.server as server_module

    class FakeClient:
        def read_memory(self, address, length):
            assert address == "0x20000000"
            assert length == 4
            return [{"type": "result", "payload": {"memory": [{"contents": "DEADBEEF"}]}}]

    monkeypatch.setattr(server_module, "gdb_client", FakeClient())
    payload = _payload(asyncio.run(handle_call_tool("read_memory", {"address": "0x20000000", "length": 4})))

    assert payload["ok"] is True
    assert payload["data"]["bytes"] == "DEADBEEF"


def test_read_memory_without_bytes_returns_error(monkeypatch):
    import mcp_server.server as server_module

    class FakeClient:
        def read_memory(self, address, length):
            return [{"type": "console", "payload": "noise"}]

    monkeypatch.setattr(server_module, "gdb_client", FakeClient())
    payload = _payload(asyncio.run(handle_call_tool("read_memory", {"address": "0x20000000", "length": 4})))

    assert payload["ok"] is False
    assert payload["error"]["code"] == "no_value_returned"


def test_named_sessions_have_isolated_state():
    # set_debug_profile on a named session must not touch the default session.
    asyncio.run(handle_call_tool("set_debug_profile", {"mcu": "STM32F407"}))  # default
    asyncio.run(handle_call_tool("set_debug_profile", {"session": "rackA", "mcu": "STM32L431"}))

    default = _payload(asyncio.run(handle_call_tool("get_debug_profile", {})))
    racka = _payload(asyncio.run(handle_call_tool("get_debug_profile", {"session": "rackA"})))

    assert default["data"]["mcu"] == "STM32F407"
    assert racka["data"]["mcu"] == "STM32L431"  # isolated, not overwritten


def test_named_session_gets_distinct_port_and_serial(monkeypatch):
    import mcp_server.server as server_module

    started = {}

    class FakeManager:
        port = 3343

        def is_alive(self):
            return False

        def start(self, server_type, args):
            started["args"] = args
            return 3343

    class FakeClient:
        def start_gdb(self):
            pass

        def connect(self, host, port):
            started["connect_port"] = port
            return [{"m": "ok"}]

        def load_symbols(self, path):
            pass

    # the named session must get its objects; patch the SessionManager's session
    sess = server_module.session_manager.get("rackC")
    monkeypatch.setattr(sess, "gdb_manager", FakeManager())
    monkeypatch.setattr(sess, "gdb_client", FakeClient())

    _payload(asyncio.run(handle_call_tool("start_debug_session", {
        "session": "rackC", "server_type": "openocd",
        "server_args": ["-f", "interface/stlink.cfg", "-f", "target/stm32l4x.cfg"],
        "serial": "066BFF",
    })))

    flat = " ".join(started["args"])
    assert f"gdb_port {sess.gdb_port}" in flat   # distinct port for the named session
    assert "telnet_port disabled" in flat        # avoid 4444 collision with another instance
    assert "tcl_port disabled" in flat           # avoid 6666 collision
    assert "adapter serial 066BFF" in flat       # selects this board's probe
    assert started["connect_port"] == 3343
    server_module.session_manager.close("rackC")


def test_list_and_close_sessions():
    import mcp_server.server as server_module

    asyncio.run(handle_call_tool("self_check", {"session": "rackB"}))  # lazily creates rackB (will error on hw, but creates the session)
    sessions = _payload(asyncio.run(handle_call_tool("list_sessions", {})))
    ids = {row["session"] for row in sessions["data"]["sessions"]}
    assert "default" in ids and "rackB" in ids

    closed = _payload(asyncio.run(handle_call_tool("close_session", {"session_id": "rackB"})))
    assert closed["data"]["closed"] is True
    assert "rackB" not in server_module.session_manager.sessions


def test_server_exposes_run_and_wait_tools():
    tools = asyncio.run(handle_list_tools())
    tool_names = {tool.name for tool in tools}

    assert "run_and_wait" in tool_names
    assert "wait_for_stop" in tool_names


def test_server_exposes_tier3_depth_tools():
    tools = asyncio.run(handle_list_tools())
    tool_names = {tool.name for tool in tools}

    for expected in (
        "step", "run_to_line", "disassemble", "verify_flash", "sample_pc",
        # depth ops now live behind action-dispatched families:
        "inspect_symbol",   # sizeof / lookup_type / address_of / resolve_address / list_*
        "coredump",         # capture / load
        "read_registers",   # core / fault / cycle
    ):
        assert expected in tool_names
    # old per-kind step tools merged into `step`; depth singles merged into families
    assert "step_out" not in tool_names and "step_into" not in tool_names
    for gone in ("sizeof", "lookup_type", "address_of", "capture_coredump", "read_cycle_counter"):
        assert gone not in tool_names


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


def test_setup_swo_configures_target_and_returns_capture_recipe(monkeypatch):
    import mcp_server.server as server_module

    class FakeClient:
        def __init__(self):
            self.writes = []

        def write_typed_memory(self, address, value, width_bits=32):
            self.writes.append((int(address, 16), int(value, 16)))

    fake = FakeClient()
    monkeypatch.setattr(server_module, "gdb_client", fake)

    payload = _payload(asyncio.run(handle_call_tool(
        "setup_swo", {"hclk_hz": 80_000_000, "swo_hz": 2_000_000, "output": "swo_itm.log"})))

    assert payload["ok"] is True
    assert payload["data"]["prescaler"] == 39
    assert payload["data"]["swo_hz"] == 2_000_000
    # target was configured from the debugger (DEMCR TRCENA among the writes)
    assert (0xE000EDFC, 1 << 24) in fake.writes
    # the agent is handed the OpenOCD command and the printf retarget
    assert any("itm port 0 on" in c for c in payload["data"]["openocd_commands"])
    assert "ITM_SendChar" in payload["data"]["firmware_retarget"]


def test_logging_swo_with_file_uses_the_file_tailer(monkeypatch, tmp_path):
    import mcp_server.server as server_module

    path = tmp_path / "swo_itm.log"
    payload = _payload(asyncio.run(handle_call_tool(
        "logging", {"action": "start", "channel": "swo", "file": str(path)})))
    try:
        assert payload["ok"] is True
        assert payload["data"]["path"] == str(path)        # FileLogReader status, not a process
        assert server_module.swo_file_reader.is_running() is True
    finally:
        asyncio.run(handle_call_tool("logging", {"action": "stop", "channel": "swo"}))


def test_sample_pc_returns_symbolized_hotspots(monkeypatch):
    import mcp_server.server as server_module

    class FakeClient:
        def profile_pc(self, count, enable):
            assert enable is True
            return {"total_samples": count, "sampled": count, "unsampleable": 0,
                    "hotspots": [{"function": "busy_loop", "samples": count, "percent": 100.0}],
                    "hot_addresses": [{"address": "0x08000400", "samples": count, "function": "busy_loop"}]}

    monkeypatch.setattr(server_module, "gdb_client", FakeClient())
    payload = _payload(asyncio.run(handle_call_tool("sample_pc", {"count": 50})))

    assert payload["ok"] is True
    assert payload["data"]["hotspots"][0]["function"] == "busy_loop"
    assert "busy_loop" in payload["data"]["message"]


def test_sample_pc_flags_a_halted_or_sleeping_core(monkeypatch):
    import mcp_server.server as server_module

    class FakeClient:
        def profile_pc(self, count, enable):
            return {"total_samples": count, "sampled": 0, "unsampleable": count,
                    "hotspots": [], "hot_addresses": []}

    monkeypatch.setattr(server_module, "gdb_client", FakeClient())
    payload = _payload(asyncio.run(handle_call_tool("sample_pc", {})))

    assert payload["ok"] is True
    assert payload["data"]["sampled"] == 0
    assert "continue_execution" in payload["suggested_next_actions"]


def test_server_exposes_check_session_health_tool():
    tools = asyncio.run(handle_list_tools())
    tool_names = {tool.name for tool in tools}

    # check_session_health is now session_diagnostics(what="health")
    assert "session_diagnostics" in tool_names
    assert "check_session_health" not in tool_names


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

    # set_write_policy / get_write_audit_log merged into write_guard(action=policy|audit)
    assert "write_guard" in tool_names
    assert "set_write_policy" not in tool_names
    assert "get_write_audit_log" not in tool_names


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


def test_analyze_stack_detects_overflow_with_size(monkeypatch):
    import mcp_server.server as server_module

    class FakeClient:
        def read_register_value(self, expr):
            assert expr == "$sp"
            return 0x20007F00  # below the limit

        def read_word(self, addr):
            return 0x2000A000  # initial MSP / stack top

    monkeypatch.setattr(server_module, "gdb_client", FakeClient())
    payload = _payload(asyncio.run(handle_call_tool("analyze_stack", {"stack_size": "0x2000"})))

    assert payload["ok"] is True
    assert payload["data"]["overflow"] is True
    assert "OVERFLOW" in payload["data"]["summary"].upper()


def test_server_exposes_reconstruct_fault_context_tool():
    tools = asyncio.run(handle_list_tools())
    tool_names = {tool.name for tool in tools}

    assert "reconstruct_fault_context" in tool_names


def test_list_breakpoints_flags_never_reached(monkeypatch):
    import mcp_server.server as server_module

    class FakeClient:
        def list_breakpoints_decoded(self):
            return [
                {"number": "1", "func": "gated_fn", "hit_count": 0},
                {"number": "2", "func": "main", "hit_count": 4},
            ]

    monkeypatch.setattr(server_module, "gdb_client", FakeClient())
    payload = _payload(asyncio.run(handle_call_tool("list_breakpoints", {})))

    assert payload["ok"] is True
    assert "['1']" in payload["data"]["summary"]  # bp 1 never reached
    assert payload["data"]["breakpoints"][0]["hit_count"] == 0


def test_run_and_wait_timeout_suggests_investigation_not_retry(monkeypatch):
    import mcp_server.server as server_module

    class FakeClient:
        def run_and_wait(self, timeout_sec):
            return {"stopped": False, "reason": "timeout", "frame": None, "raw_response": []}

    monkeypatch.setattr(server_module, "gdb_client", FakeClient())
    payload = _payload(asyncio.run(handle_call_tool("run_and_wait", {"timeout_sec": 1.0})))

    actions = payload["suggested_next_actions"]
    assert "list_breakpoints" in actions and "capture_state" in actions
    assert "run_and_wait" not in actions  # do not nudge a blind retry


def test_breakpoint_family_exposes_actions_and_honors_options():
    tools = asyncio.run(handle_list_tools())
    bp_tool = next(tool for tool in tools if tool.name == "breakpoint")
    assert set(bp_tool.inputSchema["properties"]["action"]["enum"]) == {"set", "delete", "list", "watch"}
    assert "set_breakpoint" not in {t.name for t in tools}

    # condition/temporary still flow through the family to the underlying handler.
    payload = _payload(asyncio.run(handle_call_tool(
        "breakpoint", {"action": "list"})))  # list works without a live session shape
    assert "ok" in payload


def test_server_exposes_frame_navigation_tools():
    tools = asyncio.run(handle_list_tools())
    tool_names = {tool.name for tool in tools}

    # select_frame / list_source / read_frame_variables merged into frame(action=...)
    assert "frame" in tool_names
    assert "inspect_symbol" in tool_names  # resolve_address lives here now
    for gone in ("select_frame", "read_frame_variables", "list_source", "resolve_address"):
        assert gone not in tool_names


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


def test_flash_firmware_resets_and_runs_by_default(monkeypatch):
    import mcp_server.server as server_module

    calls = []

    class FakeClient:
        def load_firmware(self, path):
            calls.append(("flash", path))
            return [{"m": "ok"}]

        def reset_run(self, command):
            calls.append(("reset_run", command))
            return [{"m": "reset"}]

    class FakeManager:
        server_type = "openocd"

    class FakeProfile:
        def get(self):
            return {}

    monkeypatch.setattr(server_module, "gdb_client", FakeClient())
    monkeypatch.setattr(server_module, "gdb_manager", FakeManager())
    monkeypatch.setattr(server_module, "debug_profile", FakeProfile())

    payload = _payload(asyncio.run(handle_call_tool("flash_firmware", {"file_path": "fw.elf"})))

    assert payload["data"]["reset_run"] is True
    assert ("reset_run", "monitor reset run") in calls


def test_flash_firmware_flash_only_when_reset_run_false(monkeypatch):
    import mcp_server.server as server_module

    calls = []

    class FakeClient:
        def load_firmware(self, path):
            calls.append("flash")
            return [{"m": "ok"}]

        def reset_run(self, command):
            calls.append("reset_run")

    monkeypatch.setattr(server_module, "gdb_client", FakeClient())
    payload = _payload(asyncio.run(handle_call_tool("flash_firmware", {"file_path": "fw.elf", "reset_run": False})))

    assert payload["data"]["reset_run"] is False
    assert "reset_run" not in calls


def test_server_exposes_build_firmware_tool():
    tools = asyncio.run(handle_list_tools())
    assert "build_firmware" in {t.name for t in tools}


def test_suggest_server_args_returns_validated_openocd_args_with_fast_clock():
    payload = _payload(asyncio.run(handle_call_tool(
        "suggest_server_args", {"mcu": "STM32L431", "probe": "stlink"}
    )))
    assert payload["ok"] is True
    assert payload["data"]["server_args"] == [
        "-f", "interface/stlink.cfg", "-f", "target/stm32l4x.cfg", "-c", "adapter speed 4000",
    ]
    assert payload["data"]["speed_khz"] == 4000
    assert "start_debug_session" in payload["suggested_next_actions"]


def test_suggest_server_args_uses_profile_probe_when_omitted(monkeypatch):
    import mcp_server.server as server_module

    class FakeProfile:
        def get(self):
            return {"probe": "cmsis-dap"}

    monkeypatch.setattr(server_module, "debug_profile", FakeProfile())
    payload = _payload(asyncio.run(handle_call_tool(
        "suggest_server_args", {"mcu": "STM32G431"}
    )))

    assert payload["ok"] is True
    assert payload["data"]["interface"] == "cmsis-dap.cfg"
    assert payload["data"]["probe_source"] == "profile"


def test_suggest_server_args_missing_probe_errors_when_profile_empty(monkeypatch):
    import mcp_server.server as server_module

    class FakeProfile:
        def get(self):
            return {}

    monkeypatch.setattr(server_module, "debug_profile", FakeProfile())
    payload = _payload(asyncio.run(handle_call_tool(
        "suggest_server_args", {"mcu": "STM32L431"}
    )))

    assert payload["ok"] is False
    assert payload["error"]["code"] == "missing_argument"
    assert "set_debug_profile" in payload["suggested_next_actions"]


def test_call_invokes_any_tool_by_name():
    # the escape hatch: reach a tool even if a client truncated it from the list
    payload = _payload(asyncio.run(handle_call_tool(
        "call", {"tool": "suggest_server_args", "args": {"mcu": "STM32L431", "probe": "stlink"}})))
    assert payload["ok"] is True
    assert payload["data"]["server_args"][0] == "-f"


def test_call_rejects_recursion():
    payload = _payload(asyncio.run(handle_call_tool("call", {"tool": "call", "args": {}})))
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_call"


def test_compact_mode_exposes_small_core_with_call(monkeypatch):
    monkeypatch.setenv("STM32_GDB_MCP_COMPACT", "1")
    names = {t.name for t in asyncio.run(handle_list_tools())}
    assert "start_debug_session" in names and "call" in names and "batch" in names
    assert len(names) < 35                     # small enough to never be truncated
    assert "read_freertos" not in names        # reachable via call, not listed in compact
    # full mode exposes the consolidated surface (well under the old 87, lean like superpowers)
    monkeypatch.delenv("STM32_GDB_MCP_COMPACT")
    full = len(asyncio.run(handle_list_tools()))
    assert 50 <= full <= 85


def test_batch_runs_steps_in_one_call_returning_full_results():
    steps = [
        {"tool": "get_debug_profile", "args": {}},
        {"tool": "suggest_server_args", "args": {"mcu": "STM32L431", "probe": "stlink"}},
    ]
    payload = _payload(asyncio.run(handle_call_tool("batch", {"steps": steps})))

    assert payload["ok"] is True
    assert payload["data"]["count"] == 2
    results = payload["data"]["results"]
    assert results[0]["tool"] == "get_debug_profile" and results[0]["ok"] is True
    # full data is returned, not just a summary
    assert results[1]["data"]["server_args"][0] == "-f"


def test_batch_stop_on_error():
    steps = [
        {"tool": "does_not_exist", "args": {}},
        {"tool": "get_debug_profile", "args": {}},
    ]
    payload = _payload(asyncio.run(handle_call_tool("batch", {"steps": steps, "stop_on_error": True})))

    assert payload["data"]["count"] == 1  # stopped after the failing step
    assert payload["data"]["results"][0]["ok"] is False


def test_build_firmware_cmake_success(monkeypatch):
    import mcp_server.build as build_mod

    captured = {}

    def fake_run(argv, timeout=600, cwd=None, log_path=None):
        captured["argv"] = argv
        return {"returncode": 0, "output": "[100%] Built target app"}

    monkeypatch.setattr(build_mod, "run_build", fake_run)
    payload = _payload(asyncio.run(handle_call_tool(
        "build_firmware", {"kind": "cmake", "build_dir": "build/x", "target": "app"}
    )))

    assert payload["ok"] is True
    assert payload["data"]["success"] is True
    assert captured["argv"] == ["cmake", "--build", "build/x", "--target", "app"]
    assert "flash_firmware" in payload["suggested_next_actions"]


def test_build_firmware_failure_is_reported(monkeypatch):
    import mcp_server.build as build_mod

    monkeypatch.setattr(build_mod, "run_build", lambda *a, **k: {"returncode": 2, "output": "error: undefined reference"})
    payload = _payload(asyncio.run(handle_call_tool(
        "build_firmware", {"kind": "keil", "project": "fw.uvprojx"}
    )))

    assert payload["ok"] is False
    assert payload["error"]["code"] == "build_failed"
    assert payload["raw_response"]["returncode"] == 2


def test_start_openocd_without_server_args_gives_clear_error(monkeypatch):
    import mcp_server.server as server_module

    started = []

    class FakeManager:
        def start(self, server_type, args):
            started.append((server_type, args))
            return 3333

    monkeypatch.setattr(server_module, "gdb_manager", FakeManager())
    payload = _payload(asyncio.run(handle_call_tool("start_debug_session", {"server_type": "openocd"})))

    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_server_args"
    assert started == []  # openocd was never launched with an empty config
    assert "load_debug_config" in payload["suggested_next_actions"]


def test_start_openocd_without_server_args_uses_profile_defaults(monkeypatch):
    import mcp_server.server as server_module

    started = {}

    class FakeManager:
        def is_alive(self):
            return False

        def start(self, server_type, args):
            started["args"] = list(args)
            return 3333

    class FakeClient:
        def start_gdb(self):
            pass

        def connect(self, host, port):
            return [{"m": "ok"}]

        def load_symbols(self, path):
            pass

    class FakeProfile:
        def get(self):
            return {"mcu": "STM32L431", "probe": "stlink"}

    monkeypatch.setattr(server_module, "gdb_manager", FakeManager())
    monkeypatch.setattr(server_module, "gdb_client", FakeClient())
    monkeypatch.setattr(server_module, "debug_profile", FakeProfile())
    payload = _payload(asyncio.run(handle_call_tool("start_debug_session", {"server_type": "openocd"})))

    assert payload["ok"] is True
    flat = " ".join(started["args"])
    assert "interface/stlink.cfg" in flat
    assert "target/stm32l4x.cfg" in flat
    assert payload["data"]["server_args_source"] == "profile"


def test_load_symbols_falls_back_to_profile_elf(monkeypatch):
    import mcp_server.server as server_module

    loaded = []

    class FakeClient:
        def load_symbols(self, path):
            loaded.append(path)
            return [{"message": "ok"}]

    class FakeProfile:
        def get(self):
            return {"elf_path": "build/fw.elf"}

    monkeypatch.setattr(server_module, "gdb_client", FakeClient())
    monkeypatch.setattr(server_module, "debug_profile", FakeProfile())
    payload = _payload(asyncio.run(handle_call_tool("load_symbols", {})))

    assert payload["ok"] is True
    assert loaded == ["build/fw.elf"]


def test_start_debug_session_autoloads_symbols_from_profile(monkeypatch):
    import mcp_server.server as server_module

    loaded = []

    class FakeManager:
        def is_alive(self):
            return False

        def start(self, server_type, args):
            return 3333

    class FakeClient:
        def start_gdb(self):
            pass

        def connect(self, host, port):
            return [{"message": "connected"}]

        def load_symbols(self, path):
            loaded.append(path)

    class FakeProfile:
        def get(self):
            return {"elf_path": "build/fw.elf"}

    monkeypatch.setattr(server_module, "gdb_manager", FakeManager())
    monkeypatch.setattr(server_module, "gdb_client", FakeClient())
    monkeypatch.setattr(server_module, "debug_profile", FakeProfile())

    payload = _payload(asyncio.run(handle_call_tool(
        "start_debug_session", {"server_type": "openocd", "server_args": ["-f", "x.cfg"]}
    )))

    assert payload["ok"] is True
    assert payload["data"]["symbols_loaded"] is True
    assert loaded == ["build/fw.elf"]


def test_start_debug_session_retries_a_transient_probe_busy(monkeypatch):
    import mcp_server.server as server_module

    calls = {"n": 0}

    class FakeManager:
        def is_alive(self):
            return False

        def start(self, server_type, args):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("GDB server failed to start. Logs: ... Error: open failed")
            return 3333

    class FakeClient:
        def start_gdb(self):
            pass

        def connect(self, host, port):
            return [{"m": "ok"}]

        def load_symbols(self, path):
            pass

    class FakeProfile:
        def get(self):
            return {}

    monkeypatch.setattr(server_module, "gdb_manager", FakeManager())
    monkeypatch.setattr(server_module, "gdb_client", FakeClient())
    monkeypatch.setattr(server_module, "debug_profile", FakeProfile())

    payload = _payload(asyncio.run(handle_call_tool(
        "start_debug_session", {"server_type": "openocd", "server_args": ["-f", "x.cfg"]})))

    assert payload["ok"] is True
    assert calls["n"] == 2  # retried once past the transient "open failed"


def test_recover_session_without_prior_session_errors():
    import mcp_server.server as server_module

    server_module._last_session.update({"server_type": None, "server_args": []})
    payload = _payload(asyncio.run(handle_call_tool("recover_session", {})))

    assert payload["ok"] is False
    assert payload["error"]["code"] == "no_session"


def test_recover_session_restarts_server_and_reconnects(monkeypatch):
    import mcp_server.server as server_module

    events = []

    class FakeManager:
        def stop(self):
            events.append("server_stop")

        def start(self, server_type, args):
            events.append(("server_start", server_type, args))
            return 3333

    class FakeClient:
        def stop_gdb(self):
            events.append("client_stop")

        def start_gdb(self):
            events.append("client_start")

        def connect(self, host, port):
            events.append(("connect", host, port))
            return [{"message": "connected"}]

    monkeypatch.setattr(server_module, "gdb_manager", FakeManager())
    monkeypatch.setattr(server_module, "gdb_client", FakeClient())
    server_module._last_session.update({"server_type": "openocd", "server_args": ["-f", "x.cfg"]})

    payload = _payload(asyncio.run(handle_call_tool("recover_session", {})))

    assert payload["ok"] is True
    assert payload["data"]["port"] == 3333
    assert ("server_start", "openocd", ["-f", "x.cfg"]) in events
    assert events.index("client_stop") < events.index("server_stop") < events.index("client_start")


def test_self_check_reports_decoded_identity(monkeypatch):
    import mcp_server.server as server_module

    class FakeClient:
        def __init__(self):
            self.halted = False

        def halt_execution(self):
            self.halted = True

        def read_word(self, address):
            return {0xE000ED00: 0x410FC241, 0xE0042000: 0x10016435}[address]

    class FakeProfile:
        def get(self):
            return {"mcu": "STM32L431"}

    fake = FakeClient()
    monkeypatch.setattr(server_module, "gdb_client", fake)
    monkeypatch.setattr(server_module, "debug_profile", FakeProfile())

    payload = _payload(asyncio.run(handle_call_tool("self_check", {})))

    assert payload["data"]["ok"] is True
    assert payload["data"]["core"] == "Cortex-M4"
    assert fake.halted is True  # halts the core before reading identity
    assert payload["data"]["halted_for_check"] is True


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

    payload = _payload(asyncio.run(handle_call_tool("get_session", {"view": "journal"})))

    tools_recorded = [e["tool"] for e in payload["data"]["entries"]]
    assert "get_debug_profile" in tools_recorded
    # the journal-reading tool itself must not be journaled
    assert "get_session" not in tools_recorded
    assert payload["data"]["entries"][0]["duration_ms"] is not None


def test_get_and_set_timeouts_round_trip(monkeypatch):
    import mcp_server.server as server_module

    server_module.gdb_client.timeouts.set({"memory": 2.0})  # reset to default-ish baseline
    before = _payload(asyncio.run(handle_call_tool("get_timeouts", {})))
    assert "connect" in before["data"]["timeouts"]

    updated = _payload(asyncio.run(handle_call_tool("set_timeouts", {"overrides": {"memory": 4.0}})))
    assert updated["ok"] is True
    assert updated["data"]["timeouts"]["memory"] == 4.0
    assert server_module.gdb_client.timeouts.get("memory") == 4.0

    # restore default so other tests are unaffected
    server_module.gdb_client.timeouts.set({"memory": 2.0})


def test_report_issue_files_once_and_dedups(monkeypatch):
    import mcp_server.issue_reporter as ir
    import mcp_server.server as server_module

    calls = []

    def fake_file_issue(repo, title, body, runner=None):
        calls.append((repo, title))
        return {"ok": True, "url": "https://github.com/Zeraissh/stm32-gdb-mcp/issues/9"}

    monkeypatch.setattr(server_module, "file_issue", fake_file_issue)
    monkeypatch.setattr(ir, "file_issue", fake_file_issue)
    server_module._reported_issues.clear()

    p1 = _payload(asyncio.run(handle_call_tool(
        "report_issue", {"title": "[agent] X fails", "description": "did Y, got Z"})))
    assert p1["ok"] is True
    assert "issues/9" in p1["data"]["url"]
    assert len(calls) == 1

    # same title again -> deduped, not filed twice
    p2 = _payload(asyncio.run(handle_call_tool(
        "report_issue", {"title": "[agent] X fails", "description": "again"})))
    assert "deduplicated" in p2["data"]["message"]
    assert len(calls) == 1


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

    metrics = _payload(asyncio.run(handle_call_tool("get_session", {"view": "metrics"})))
    assert metrics["data"]["by_tool"]["get_debug_profile"]["calls"] == 2
    assert metrics["data"]["totals"]["calls"] == 2

    timeline = _payload(asyncio.run(handle_call_tool("get_session", {"view": "timeline"})))
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
    assert "run_for_duration" in tool_names


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


def test_run_for_duration_tool_halts_and_returns_captured_expressions(monkeypatch):
    import mcp_server.composites as composites_module
    import mcp_server.server as server_module

    class FakeClient:
        def continue_execution(self):
            return [{"message": "running"}]

        def halt_execution(self):
            return [{"message": "stopped"}]

        def read_call_stack_decoded(self):
            return [{"level": 0, "func": "poll_sensors", "file": "main.c", "line": 88, "addr": "0x08001000"}]

        def read_frame_variables_decoded(self, level=None):
            return {"state": "polling"}

        def read_variable(self, expression):
            assert expression == "rx_count"
            return [{"payload": {"value": "7"}}]

    monkeypatch.setattr(server_module, "gdb_client", FakeClient())
    monkeypatch.setattr(composites_module.time, "sleep", lambda _: None)
    payload = _payload(asyncio.run(handle_call_tool(
        "run_for_duration",
        {"duration_sec": 2.0, "capture": {"expressions": ["rx_count"]}},
    )))

    assert payload["ok"] is True
    assert payload["data"]["duration_sec"] == 2.0
    assert payload["data"]["halt"]["method"] == "halt_execution"
    assert payload["data"]["final_frame"]["func"] == "poll_sensors"
    assert payload["data"]["capture"]["expressions"]["values"][0]["value"] == 7


def test_run_for_duration_tool_accepts_low_rate_sampling(monkeypatch):
    import mcp_server.server as server_module

    captured = {}

    def fake_run_for_duration(gdb_client, **kwargs):
        captured["kwargs"] = kwargs
        return {
            "duration_sec": kwargs["duration_sec"],
            "halt": {"method": "halt_execution"},
            "resume_after": False,
            "sample": {
                "mode": "debugger_polling",
                "series": [{"index": 0, "t_sec": 0.0, "values": {"rx_count": 1}, "raw": {"rx_count": "1"}, "errors": {}}],
                "summary": {"rx_count": {"sample_count": 1, "error_count": 0, "first": 1, "last": 1}},
                "timing": {"sample_count": 1},
            },
        }

    monkeypatch.setattr(server_module, "run_for_duration", fake_run_for_duration)

    tool = next(tool for tool in asyncio.run(handle_list_tools()) if tool.name == "run_for_duration")
    assert "sample" in tool.inputSchema["properties"]

    payload = _payload(asyncio.run(handle_call_tool(
        "run_for_duration",
        {"duration_sec": 0.5, "sample": {"interval_ms": 250, "expressions": ["rx_count"]}},
    )))

    assert payload["ok"] is True
    assert captured["kwargs"]["sample"] == {"interval_ms": 250, "expressions": ["rx_count"]}
    assert payload["data"]["sample"]["mode"] == "debugger_polling"
    assert payload["data"]["sample"]["series"][0]["values"]["rx_count"] == 1


def test_expressions_capture_accepts_indexed_table_via_merged_family(monkeypatch):
    import mcp_server.server as server_module

    class FakeClient:
        def __init__(self):
            self.values = {
                "s_diag_cb_count[2]": "10",
                "s_diag_ack_count[2]": "8",
            }

        def read_variable(self, expression):
            return [{"payload": {"value": self.values[expression]}}]

    monkeypatch.setattr(server_module, "gdb_client", FakeClient())
    payload = _payload(asyncio.run(handle_call_tool(
        "expressions",
        {
            "action": "capture",
            "table": {"index_range": [2, 2], "columns": ["s_diag_cb_count", "s_diag_ack_count"]},
        },
    )))

    assert payload["ok"] is True
    assert payload["data"]["values"][0]["expression"] == "s_diag_cb_count[2]"
    assert payload["data"]["tables"][0]["rows"] == [
        {
            "index": 2,
            "values": {"s_diag_cb_count": 10, "s_diag_ack_count": 8},
            "raw": {"s_diag_cb_count": "10", "s_diag_ack_count": "8"},
            "errors": {},
        }
    ]


def test_same_session_dispatch_is_serialized(monkeypatch):
    """Two concurrent calls to the SAME session must not touch its GDB pipe at once."""
    import mcp_server.server as server_module

    overlap = {"active": 0, "max": 0}
    guard = threading.Lock()

    def fake_dispatch(name, arguments):
        with guard:
            overlap["active"] += 1
            overlap["max"] = max(overlap["max"], overlap["active"])
        time.sleep(0.05)
        with guard:
            overlap["active"] -= 1
        return [content_success({"name": name})]

    monkeypatch.setattr(server_module, "_dispatch_tool", fake_dispatch)

    async def run_two():
        return await asyncio.gather(
            handle_call_tool("read_core_registers", {}),
            handle_call_tool("read_core_registers", {}),
        )

    asyncio.run(run_two())

    # Both target the default session -> one shared per-session lock -> never overlap.
    assert overlap["max"] == 1


def test_different_sessions_dispatch_concurrently(monkeypatch):
    """Calls to DIFFERENT sessions (boards) must run in parallel, not block each other."""
    import mcp_server.server as server_module

    overlap = {"active": 0, "max": 0}
    guard = threading.Lock()

    def fake_dispatch(name, arguments):
        with guard:
            overlap["active"] += 1
            overlap["max"] = max(overlap["max"], overlap["active"])
        time.sleep(0.05)
        with guard:
            overlap["active"] -= 1
        return [content_success({"name": name})]

    class _StubSessions:
        def get(self, sid):
            return None

    monkeypatch.setattr(server_module, "_dispatch_tool", fake_dispatch)
    monkeypatch.setattr(server_module, "session_manager", _StubSessions())

    async def run_two():
        return await asyncio.gather(
            handle_call_tool("read_core_registers", {"session": "boardA"}),
            handle_call_tool("read_core_registers", {"session": "boardB"}),
        )

    asyncio.run(run_two())

    # Distinct sessions -> distinct locks -> both dispatches overlap in worker threads.
    assert overlap["max"] == 2


def test_blocking_dispatch_does_not_block_event_loop(monkeypatch):
    """A blocking GDB dispatch must run off the event loop so it stays responsive."""
    import mcp_server.server as server_module

    def slow_dispatch(name, arguments):
        time.sleep(0.2)
        return [content_success({"name": name})]

    monkeypatch.setattr(server_module, "_dispatch_tool", slow_dispatch)

    async def run():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        task = asyncio.create_task(ticker())
        await handle_call_tool("read_core_registers", {})
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return ticks

    ticks = asyncio.run(run())

    # If dispatch ran inline on the loop, the ticker could not advance during the 0.2s
    # blocking sleep. Run off-loop it ticks ~20 times; allow generous slack.
    assert ticks > 5


_KICAD_NETLIST = (
    '(export (version "E")'
    '  (components'
    '    (comp (ref "U1") (value "STM32L431CBT6") (footprint "LQFP-48"))'
    '    (comp (ref "J1") (value "USB_C") (footprint "Conn")))'
    '  (nets'
    '    (net (code "1") (name "/USART1_TX")'
    '      (node (ref "U1") (pin "42") (pinfunction "PA9"))'
    '      (node (ref "J1") (pin "3")))'
    '    (net (code "2") (name "/I2C1_SCL")'
    '      (node (ref "U1") (pin "45") (pinfunction "PB6")))'
    '    (net (code "3") (name "GND") (node (ref "U1") (pin "8")))))'
)


def test_server_exposes_netlist_pipeline_tools():
    tool_names = {tool.name for tool in asyncio.run(handle_list_tools())}

    assert "import_netlist" in tool_names
    assert "describe_board" in tool_names
    assert "import_spec" in tool_names
    assert "design_framework" in tool_names


def test_import_netlist_then_describe_board_roundtrip():
    imported = asyncio.run(handle_call_tool("import_netlist", {"text": _KICAD_NETLIST, "session": "board-import"}))
    payload = json.loads(imported[0].text)

    assert payload["ok"] is True
    assert payload["data"]["mcu"]["line"] == "STM32L431"
    assert payload["data"]["peripherals"] == ["I2C1", "USART1"]

    pins = asyncio.run(handle_call_tool("describe_board", {"what": "pins", "session": "board-import"}))
    pins_payload = json.loads(pins[0].text)

    assert pins_payload["ok"] is True
    tx = next(p for p in pins_payload["data"]["pins"] if p["net"] == "/USART1_TX")
    assert tx["port_pin"] == "PA9"
    assert tx["function"] == {"peripheral": "USART1", "signal": "TX"}


def test_import_netlist_requires_path_or_text():
    result = asyncio.run(handle_call_tool("import_netlist", {"session": "board-missing"}))
    payload = json.loads(result[0].text)

    assert payload["ok"] is False
    assert payload["error"]["code"] == "missing_argument"


def test_describe_board_without_import_errors():
    result = asyncio.run(handle_call_tool("describe_board", {"session": "board-empty"}))
    payload = json.loads(result[0].text)

    assert payload["ok"] is False
    assert payload["error"]["code"] == "no_board"


_KICAD_NETLIST_CONFLICT = (
    '(export (version "E")'
    '  (components'
    '    (comp (ref "U1") (value "STM32L431CBT6") (footprint "LQFP-48")))'
    '  (nets'
    '    (net (code "1") (name "/USART1_TX")'
    '      (node (ref "U1") (pin "42") (pinfunction "PA9")))'
    '    (net (code "2") (name "/MCU_USART1_TX")'
    '      (node (ref "U1") (pin "20") (pinfunction "PB6")))))'
)


def test_server_exposes_validate_board_tool():
    tool_names = {tool.name for tool in asyncio.run(handle_list_tools())}

    assert "validate_board" in tool_names


def test_validate_board_on_clean_import_is_ok():
    asyncio.run(handle_call_tool("import_netlist", {"text": _KICAD_NETLIST, "session": "board-validate-ok"}))
    result = asyncio.run(handle_call_tool("validate_board", {"session": "board-validate-ok"}))
    payload = json.loads(result[0].text)

    assert payload["ok"] is True  # envelope
    assert payload["data"]["ok"] is True  # no structural errors
    assert payload["data"]["af_checked"] is False
    # No power/SWD/NRST in the fixture -> warnings, but not blocking errors.
    assert payload["data"]["stats"]["error_count"] == 0


def test_validate_board_detects_duplicate_signal():
    asyncio.run(handle_call_tool("import_netlist", {"text": _KICAD_NETLIST_CONFLICT, "session": "board-validate-bad"}))
    result = asyncio.run(handle_call_tool("validate_board", {"session": "board-validate-bad"}))
    payload = json.loads(result[0].text)

    assert payload["ok"] is True  # tool ran successfully
    assert payload["data"]["ok"] is False  # board is invalid
    kinds = {c["type"] for c in payload["data"]["conflicts"]}
    assert "duplicate_peripheral_signal" in kinds


def test_validate_board_without_import_errors():
    result = asyncio.run(handle_call_tool("validate_board", {"session": "board-validate-empty"}))
    payload = json.loads(result[0].text)

    assert payload["ok"] is False
    assert payload["error"]["code"] == "no_board"


# --- Pillar B1: acceptance layer -------------------------------------------

_ACCEPTANCE_SPEC = {
    "name": "blinky-accept",
    "checks": [
        {"id": "usart1-on", "kind": "memory_u32", "address": "0x40013800", "expect": "0x1"},
        {"id": "sysclk", "kind": "variable", "name": "SystemCoreClock", "expect": 80000000},
        {"id": "sp-in-ram", "kind": "core_register", "register": "sp", "op": "ge", "expect": "0x20000000"},
        {"id": "no-fault", "kind": "no_fault"},
        {"id": "reached", "kind": "stopped_at", "symbol": "main_loop"},
    ],
}


class _ScriptedClient:
    """A gdb_client stand-in returning canned values for the acceptance reader."""

    def __init__(self, memory, variables, registers, symbols, fault_registers=None):
        self.memory = memory
        self.variables = variables
        self.registers = registers
        self.symbols = symbols
        self.fault_registers = fault_registers or {"CFSR": 0, "HFSR": 0, "BFAR": 0, "MMFAR": 0}

    def read_word(self, address):
        key = int(address, 0) if isinstance(address, str) else address
        return self.memory[key]

    def read_register_value(self, expr):
        return self.registers[expr.lstrip("$")]

    def read_fault_registers(self):
        return self.fault_registers

    def symbolize_pc(self, pc):
        return self.symbols.get(pc, "")

    def read_variable(self, name):
        return [{"type": "result", "payload": {"value": str(self.variables[name])}}]


def _healthy_client():
    return _ScriptedClient(
        memory={0x40013800: 0x1},
        variables={"SystemCoreClock": 80000000},
        registers={"sp": 0x20001000, "pc": 0x08000500},
        symbols={0x08000500: "main_loop"},
    )


def test_server_exposes_acceptance_tools():
    tool_names = {tool.name for tool in asyncio.run(handle_list_tools())}

    assert {"load_acceptance", "run_acceptance", "describe_acceptance"} <= tool_names


def test_load_and_describe_acceptance():
    loaded = asyncio.run(handle_call_tool("load_acceptance", {"spec": _ACCEPTANCE_SPEC, "session": "accept-desc"}))
    loaded_payload = json.loads(loaded[0].text)

    assert loaded_payload["ok"] is True
    assert loaded_payload["data"]["check_count"] == 5
    assert loaded_payload["data"]["kinds"]["memory_u32"] == 1

    checks = asyncio.run(handle_call_tool("describe_acceptance", {"what": "checks", "session": "accept-desc"}))
    checks_payload = json.loads(checks[0].text)

    assert checks_payload["ok"] is True
    assert [c["id"] for c in checks_payload["data"]["checks"]] == \
        ["usart1-on", "sysclk", "sp-in-ram", "no-fault", "reached"]


def test_load_acceptance_rejects_invalid_spec():
    result = asyncio.run(handle_call_tool(
        "load_acceptance", {"spec": {"checks": [{"kind": "bogus"}]}, "session": "accept-bad"}))
    payload = json.loads(result[0].text)

    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_spec"


def test_run_acceptance_passes_against_scripted_client(monkeypatch):
    import mcp_server.server as server_module

    sess = server_module.session_manager.get("accept-pass")
    monkeypatch.setattr(sess, "gdb_client", _healthy_client())

    asyncio.run(handle_call_tool("load_acceptance", {"spec": _ACCEPTANCE_SPEC, "session": "accept-pass"}))
    result = asyncio.run(handle_call_tool("run_acceptance", {"session": "accept-pass"}))
    payload = json.loads(result[0].text)

    assert payload["ok"] is True  # tool ran
    assert payload["data"]["ok"] is True  # all checks passed
    assert payload["data"]["stats"] == {"total": 5, "passed": 5, "failed": 0, "errored": 0}

    # The verdict is retrievable via describe_acceptance(what=last_result).
    last = asyncio.run(handle_call_tool("describe_acceptance", {"what": "last_result", "session": "accept-pass"}))
    assert json.loads(last[0].text)["data"]["ok"] is True


def test_run_acceptance_reports_failure(monkeypatch):
    import mcp_server.server as server_module

    sess = server_module.session_manager.get("accept-fail")
    client = _healthy_client()
    client.memory[0x40013800] = 0x0  # USART1 disabled -> usart1-on check fails
    monkeypatch.setattr(sess, "gdb_client", client)

    asyncio.run(handle_call_tool("load_acceptance", {"spec": _ACCEPTANCE_SPEC, "session": "accept-fail"}))
    result = asyncio.run(handle_call_tool("run_acceptance", {"session": "accept-fail"}))
    payload = json.loads(result[0].text)

    assert payload["ok"] is True
    assert payload["data"]["ok"] is False
    failed = [r for r in payload["data"]["results"] if r["status"] == "fail"]
    assert [r["id"] for r in failed] == ["usart1-on"]


def test_run_acceptance_without_spec_errors():
    result = asyncio.run(handle_call_tool("run_acceptance", {"session": "accept-empty"}))
    payload = json.loads(result[0].text)

    assert payload["ok"] is False
    assert payload["error"]["code"] == "no_spec"


# --- Pillar C: bounded acceptance-loop orchestrator ------------------------


def test_server_exposes_loop_tools():
    tool_names = {tool.name for tool in asyncio.run(handle_list_tools())}

    assert {"start_acceptance_loop", "run_acceptance_iteration", "acceptance_loop_status"} <= tool_names


def test_start_acceptance_loop_requires_spec():
    result = asyncio.run(handle_call_tool("start_acceptance_loop", {"session": "loop-nospec"}))
    payload = json.loads(result[0].text)

    assert payload["ok"] is False
    assert payload["error"]["code"] == "no_spec"


def test_loop_iterates_to_convergence(monkeypatch):
    import mcp_server.server as server_module

    sess = server_module.session_manager.get("loop-pass")
    monkeypatch.setattr(sess, "gdb_client", _healthy_client())

    asyncio.run(handle_call_tool("load_acceptance", {"spec": _ACCEPTANCE_SPEC, "session": "loop-pass"}))
    asyncio.run(handle_call_tool("start_acceptance_loop", {"max_iterations": 5, "session": "loop-pass"}))
    result = asyncio.run(handle_call_tool("run_acceptance_iteration", {"session": "loop-pass"}))
    payload = json.loads(result[0].text)

    assert payload["ok"] is True
    assert payload["data"]["decision"]["converged"] is True
    assert payload["data"]["iteration"]["ok"] is True

    status = asyncio.run(handle_call_tool("acceptance_loop_status", {"session": "loop-pass"}))
    status_payload = json.loads(status[0].text)
    assert status_payload["data"]["summary"]["status"] == "converged"
    assert status_payload["data"]["summary"]["iteration_count"] == 1


def test_loop_iteration_reports_failure_and_stays_active(monkeypatch):
    import mcp_server.server as server_module

    sess = server_module.session_manager.get("loop-fail")
    client = _healthy_client()
    client.memory[0x40013800] = 0x0  # USART1 disabled -> usart1-on fails
    monkeypatch.setattr(sess, "gdb_client", client)

    asyncio.run(handle_call_tool("load_acceptance", {"spec": _ACCEPTANCE_SPEC, "session": "loop-fail"}))
    asyncio.run(handle_call_tool("start_acceptance_loop", {"session": "loop-fail"}))
    result = asyncio.run(handle_call_tool("run_acceptance_iteration", {"session": "loop-fail"}))
    payload = json.loads(result[0].text)

    assert payload["data"]["decision"]["should_continue"] is True
    assert payload["data"]["iteration"]["unsatisfied_ids"] == ["usart1-on"]


def test_terminal_loop_refuses_without_force(monkeypatch):
    import mcp_server.server as server_module

    sess = server_module.session_manager.get("loop-terminal")
    monkeypatch.setattr(sess, "gdb_client", _healthy_client())

    asyncio.run(handle_call_tool("load_acceptance", {"spec": _ACCEPTANCE_SPEC, "session": "loop-terminal"}))
    asyncio.run(handle_call_tool("start_acceptance_loop", {"session": "loop-terminal"}))
    asyncio.run(handle_call_tool("run_acceptance_iteration", {"session": "loop-terminal"}))  # -> converged

    # A converged loop refuses another iteration unless forced.
    refused = json.loads(asyncio.run(handle_call_tool(
        "run_acceptance_iteration", {"session": "loop-terminal"}))[0].text)
    assert refused["data"]["iteration"] is None
    assert refused["data"]["decision"]["converged"] is True

    forced = json.loads(asyncio.run(handle_call_tool(
        "run_acceptance_iteration", {"force": True, "session": "loop-terminal"}))[0].text)
    assert forced["data"]["iteration"] is not None
    assert forced["data"]["summary"]["iteration_count"] == 2


def test_run_acceptance_iteration_without_loop_errors():
    result = asyncio.run(handle_call_tool("run_acceptance_iteration", {"session": "loop-none"}))
    payload = json.loads(result[0].text)

    assert payload["ok"] is False
    assert payload["error"]["code"] == "no_loop"


# --- Pillar D: design synthesis (framework/init-code solver) ----------------


def test_server_exposes_design_tools():
    tool_names = {tool.name for tool in asyncio.run(handle_list_tools())}

    assert {"design_framework", "describe_framework", "render_framework"} <= tool_names


def test_design_framework_requires_board():
    result = asyncio.run(handle_call_tool("design_framework", {"session": "design-noboard"}))
    payload = json.loads(result[0].text)

    assert payload["ok"] is False
    assert payload["error"]["code"] == "no_board"


def test_design_then_describe_and_render_roundtrip():
    sid = "design-roundtrip"
    asyncio.run(handle_call_tool("import_netlist", {"text": _KICAD_NETLIST, "session": sid}))

    design = {"USART1": {"baud": 115200, "word_length": "UART_WORDLENGTH_8B"}}
    af_map = {"STM32L431": {"PA9": {"USART1_TX": 7}}}
    designed = json.loads(asyncio.run(handle_call_tool(
        "design_framework", {"design": design, "af_map": af_map, "session": sid}))[0].text)

    assert designed["ok"] is True
    assert "__HAL_RCC_USART1_CLK_ENABLE" in designed["data"]["clocks"]
    assert {p["name"] for p in designed["data"]["peripherals"]} == {"I2C1", "USART1"}

    # describe: I2C1 still needs a variant-specific timing/speed decision -> unresolved.
    unresolved = json.loads(asyncio.run(handle_call_tool(
        "describe_framework", {"what": "unresolved", "session": sid}))[0].text)
    assert any(u["type"] == "param_unresolved" and u["peripheral"] == "I2C1"
               for u in unresolved["data"]["unresolved"])

    # render: concrete facts for USART1, honest TODO for I2C1's timing.
    rendered = json.loads(asyncio.run(handle_call_tool(
        "render_framework", {"session": sid}))[0].text)
    assert rendered["ok"] is True
    source = next(f["content"] for f in rendered["data"]["files"] if f["path"] == "bsp_init.c")
    assert "GPIO_InitStruct.Alternate = GPIO_AF7_USART1;" in source
    assert "huart1.Init.BaudRate = 115200;" in source
    # Mandatory UART fields are now filled from HAL defaults (complete, valid struct).
    assert "huart1.Init.WordLength = UART_WORDLENGTH_8B;" in source
    assert "hi2c1.Init.AddressingMode = I2C_ADDRESSINGMODE_7BIT;" in source
    assert "set hi2c1.Init.Timing/ClockSpeed" in source
    assert rendered["data"]["todo_count"] > 0


def test_describe_framework_without_design_errors():
    result = asyncio.run(handle_call_tool("describe_framework", {"session": "design-empty"}))
    payload = json.loads(result[0].text)

    assert payload["ok"] is False
    assert payload["error"]["code"] == "no_design"


def test_render_framework_without_design_errors():
    result = asyncio.run(handle_call_tool("render_framework", {"session": "design-empty2"}))
    payload = json.loads(result[0].text)

    assert payload["ok"] is False
    assert payload["error"]["code"] == "no_design"


def _seed_design(sid):
    asyncio.run(handle_call_tool("import_netlist", {"text": _KICAD_NETLIST, "session": sid}))
    design = {"USART1": {"baud": 115200}}
    af_map = {"STM32L431": {"PA9": {"USART1_TX": 7}}}
    asyncio.run(handle_call_tool(
        "design_framework", {"design": design, "af_map": af_map, "session": sid}))


def test_synthesize_acceptance_requires_design():
    result = asyncio.run(handle_call_tool("synthesize_acceptance", {"session": "synth-nodesign"}))
    payload = json.loads(result[0].text)

    assert payload["ok"] is False
    assert payload["error"]["code"] == "no_design"


def test_synthesize_acceptance_no_placement_source_is_no_fault_only():
    sid = "synth-nofault"
    _seed_design(sid)
    result = json.loads(asyncio.run(handle_call_tool(
        "synthesize_acceptance", {"session": sid}))[0].text)

    assert result["ok"] is True
    data = result["data"]
    assert data["placement_source"] == "none"
    assert data["stats"]["clock_checks"] == 0
    assert [c["kind"] for c in data["checks"]] == ["no_fault"]
    # Clocks the plan enables are surfaced as unresolved, never invented.
    assert {"GPIOA", "GPIOB", "USART1", "I2C1"} <= {u["clock"] for u in data["unresolved"]}


def test_synthesize_acceptance_with_register_map_emits_clock_check():
    sid = "synth-regmap"
    _seed_design(sid)
    register_map = {"STM32L431": {"USART1": {"address": "0x40021060", "bit": 14}}}
    result = json.loads(asyncio.run(handle_call_tool(
        "synthesize_acceptance", {"register_map": register_map, "session": sid}))[0].text)

    assert result["ok"] is True
    data = result["data"]
    assert data["placement_source"] == "register_map"
    usart = next(c for c in data["checks"] if c["id"] == "clk_USART1_enabled")
    assert usart["kind"] == "memory_u32"
    assert usart["op"] == "bits_set"
    assert usart["expect"] == "0x00004000"
    # Clocks without a placement stay unresolved.
    assert any(u["clock"] == "I2C1" for u in data["unresolved"])


def test_synthesize_acceptance_rejects_bad_register_map():
    sid = "synth-badmap"
    _seed_design(sid)
    result = json.loads(asyncio.run(handle_call_tool(
        "synthesize_acceptance", {"register_map": "nope", "session": sid}))[0].text)

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_argument"


def test_synthesize_acceptance_auto_loads_into_session():
    sid = "synth-autoload"
    _seed_design(sid)
    synth = json.loads(asyncio.run(handle_call_tool(
        "synthesize_acceptance", {"stopped_at": "main", "session": sid}))[0].text)
    assert synth["data"]["loaded"] is True

    # The derived spec is now the session's acceptance judge.
    checks = json.loads(asyncio.run(handle_call_tool(
        "describe_acceptance", {"what": "checks", "session": sid}))[0].text)
    assert checks["ok"] is True
    ids = [c["id"] for c in checks["data"]["checks"]]
    assert "no_fault_after_init" in ids
    kinds = {c["kind"] for c in checks["data"]["checks"]}
    assert "stopped_at" in kinds


def test_synthesize_acceptance_no_load_leaves_session_untouched():
    sid = "synth-noload"
    _seed_design(sid)
    synth = json.loads(asyncio.run(handle_call_tool(
        "synthesize_acceptance", {"load": False, "session": sid}))[0].text)
    assert synth["data"]["loaded"] is False

    # Nothing was loaded, so describe_acceptance has no spec to show.
    describe = json.loads(asyncio.run(handle_call_tool(
        "describe_acceptance", {"what": "checks", "session": sid}))[0].text)
    assert describe["ok"] is False


def _seed_design_nvic(sid):
    asyncio.run(handle_call_tool("import_netlist", {"text": _KICAD_NETLIST, "session": sid}))
    design = {"USART1": {"baud": 115200, "nvic": True}}
    af_map = {"STM32L431": {"PA9": {"USART1_TX": 7}}}
    asyncio.run(handle_call_tool(
        "design_framework", {"design": design, "af_map": af_map, "session": sid}))


def test_synthesize_acceptance_irq_map_emits_nvic_iser_check():
    sid = "synth-irqmap"
    _seed_design_nvic(sid)
    irq_map = {"STM32L431": {"USART1_IRQn": 37}}
    result = json.loads(asyncio.run(handle_call_tool(
        "synthesize_acceptance", {"irq_map": irq_map, "session": sid}))[0].text)

    assert result["ok"] is True
    data = result["data"]
    assert data["resolver_sources"]["nvic"] == "irq_map"
    nvic = next(c for c in data["checks"] if c["id"] == "nvic_USART1_IRQn_enabled")
    assert nvic["kind"] == "memory_u32"
    assert nvic["address"] == "0xe000e104"   # ISER[1], IRQ 37
    assert nvic["expect"] == "0x00000020"    # bit 5
    assert data["stats"]["nvic_checks"] == 1


def test_synthesize_acceptance_gpio_map_emits_moder_check():
    sid = "synth-gpiomap"
    _seed_design(sid)
    gpio_map = {"STM32L431": {"A": "0x48000000", "B": "0x48000400"}}
    result = json.loads(asyncio.run(handle_call_tool(
        "synthesize_acceptance", {"gpio_map": gpio_map, "session": sid}))[0].text)

    assert result["ok"] is True
    data = result["data"]
    assert data["resolver_sources"]["gpio"] == "gpio_map"
    pa9 = next(c for c in data["checks"] if c["id"] == "gpio_PA9_mode")
    assert pa9["op"] == "eq"
    assert pa9["mask"] == "0x000c0000"     # pin 9 -> shift 18
    assert pa9["expect"] == "0x00080000"   # AF = 0b10
    assert data["stats"]["gpio_checks"] >= 1


def test_synthesize_acceptance_reports_all_sources_none_without_maps_or_svd():
    sid = "synth-nosrc"
    _seed_design(sid)
    result = json.loads(asyncio.run(handle_call_tool(
        "synthesize_acceptance", {"session": sid}))[0].text)

    assert result["ok"] is True
    assert result["data"]["resolver_sources"] == {"clock": "none", "nvic": "none", "gpio": "none"}


def test_synthesize_acceptance_annotates_source_provenance():
    sid = "synth-prov"
    _seed_design_nvic(sid)
    result = json.loads(asyncio.run(handle_call_tool(
        "synthesize_acceptance",
        {"irq_map": {"STM32L431": {"USART1_IRQn": 37}},
         "gpio_map": {"STM32L431": {"A": "0x48000000", "B": "0x48000400"}},
         "session": sid}))[0].text)

    assert result["ok"] is True
    assert result["data"]["provenance"]["located"] >= 2  # nvic + gpio at least
    # The stored spec carries each check's resolved source location.
    checks = json.loads(asyncio.run(handle_call_tool(
        "describe_acceptance", {"what": "checks", "session": sid}))[0].text)["data"]["checks"]
    nvic = next(c for c in checks if c["id"] == "nvic_USART1_IRQn_enabled")["provenance"]["source"]
    assert nvic["located"] is True
    assert nvic["init_fn"] == "MX_USART1_UART_Init"
    assert nvic["file"] == "bsp_init.c"
    gpio = next(c for c in checks if c["id"] == "gpio_PA9_mode")["provenance"]["source"]
    assert gpio["init_fn"] == "MX_GPIO_Init"


def test_run_acceptance_failure_carries_source_provenance(monkeypatch):
    import mcp_server.server as server_module

    sid = "accept-prov"
    _seed_design_nvic(sid)
    asyncio.run(handle_call_tool(
        "synthesize_acceptance",
        {"gpio_map": {"STM32L431": {"A": "0x48000000", "B": "0x48000400"}}, "session": sid}))

    sess = server_module.session_manager.get(sid)
    client = _ScriptedClient(  # MODER still at reset (input) -> every GPIO mode check fails
        memory={0x48000000: 0x0, 0x48000400: 0x0}, variables={}, registers={}, symbols={})
    monkeypatch.setattr(sess, "gdb_client", client)

    payload = json.loads(asyncio.run(handle_call_tool("run_acceptance", {"session": sid}))[0].text)
    assert payload["ok"] is True
    assert payload["data"]["ok"] is False
    gpio_fail = next(r for r in payload["data"]["results"] if r["id"] == "gpio_PA9_mode")
    assert gpio_fail["status"] == "fail"
    assert gpio_fail["provenance"]["source"]["located"] is True
    assert gpio_fail["provenance"]["source"]["init_fn"] == "MX_GPIO_Init"
    # a passing check carries no provenance noise
    no_fault = next(r for r in payload["data"]["results"] if r["id"] == "no_fault_after_init")
    assert no_fault["status"] == "pass"
    assert "provenance" not in no_fault


# --- solve_clock_tree (Pillar D Tier 3: SystemClock_Config synthesis) --------

_H7_NETLIST = (
    '(export (version "E")'
    '  (components'
    '    (comp (ref "U1") (value "STM32H750VBT6") (footprint "LQFP-100")))'
    '  (nets'
    '    (net (code "1") (name "/USART1_TX")'
    '      (node (ref "U1") (pin "42") (pinfunction "PA9")))))'
)


def test_solve_clock_tree_requires_design():
    result = asyncio.run(handle_call_tool("solve_clock_tree", {"sysclk_hz": 80_000_000, "session": "clk-nodesign"}))
    payload = json.loads(result[0].text)

    assert payload["ok"] is False
    assert payload["error"]["code"] == "no_design"


def test_solve_clock_tree_requires_target():
    sid = "clk-notarget"
    _seed_design(sid)
    result = json.loads(asyncio.run(handle_call_tool("solve_clock_tree", {"session": sid}))[0].text)

    assert result["ok"] is False
    assert result["error"]["code"] == "missing_argument"


def test_solve_clock_tree_unmodelled_device_is_surfaced_not_guessed():
    sid = "clk-h7"
    asyncio.run(handle_call_tool("import_netlist", {"text": _H7_NETLIST, "session": sid}))
    asyncio.run(handle_call_tool("design_framework", {"session": sid}))
    result = json.loads(asyncio.run(handle_call_tool(
        "solve_clock_tree", {"sysclk_hz": 400_000_000, "session": sid}))[0].text)

    assert result["ok"] is True
    assert result["data"]["feasible"] is False
    assert result["data"]["unresolved"][0]["type"] == "device_unmodelled"


def test_solve_clock_tree_happy_path_stores_config_and_renders_real_code():
    sid = "clk-happy"
    _seed_design(sid)
    solved = json.loads(asyncio.run(handle_call_tool(
        "solve_clock_tree", {"source": "HSI", "sysclk_hz": 80_000_000, "session": sid}))[0].text)

    assert solved["ok"] is True
    assert solved["data"]["feasible"] is True
    assert solved["data"]["loaded"] is True
    assert solved["data"]["clock"]["sysclk_mhz"] == 80.0

    # render_framework now emits a real SystemClock_Config instead of the TODO stub.
    rendered = json.loads(asyncio.run(handle_call_tool("render_framework", {"session": sid}))[0].text)
    source = next(f["content"] for f in rendered["data"]["files"] if f["path"] == "bsp_init.c")
    assert "TODO: configure the clock tree" not in source
    assert "RCC_OscInitStruct.PLL.PLLR = RCC_PLLR_DIV2;" in source
    assert "FLASH_LATENCY_4" in source


def test_solve_clock_tree_infeasible_target_is_honest():
    sid = "clk-infeasible"
    _seed_design(sid)
    result = json.loads(asyncio.run(handle_call_tool(
        "solve_clock_tree", {"source": "HSI", "sysclk_hz": 200_000_000, "session": sid}))[0].text)

    assert result["ok"] is True
    assert result["data"]["feasible"] is False
    assert result["data"]["unresolved"][0]["type"] == "target_exceeds_max_sysclk"


def test_solve_clock_tree_no_load_leaves_stub():
    sid = "clk-noload"
    _seed_design(sid)
    solved = json.loads(asyncio.run(handle_call_tool(
        "solve_clock_tree", {"source": "HSI", "sysclk_hz": 80_000_000, "load": False, "session": sid}))[0].text)
    assert solved["data"]["loaded"] is False

    rendered = json.loads(asyncio.run(handle_call_tool("render_framework", {"session": sid}))[0].text)
    source = next(f["content"] for f in rendered["data"]["files"] if f["path"] == "bsp_init.c")
    assert "TODO: configure the clock tree" in source

# --- solve_timer (Pillar D Tier 3: timer base-frequency synthesis) -----------

_TIMER_NETLIST = (
    '(export (version "E")'
    '  (components'
    '    (comp (ref "U1") (value "STM32L431CBT6") (footprint "LQFP-48")))'
    '  (nets'
    '    (net (code "1") (name "/TIM3_CH1")'
    '      (node (ref "U1") (pin "10") (pinfunction "PA6")))'
    '    (net (code "2") (name "GND") (node (ref "U1") (pin "8")))))'
)


def _seed_timer_design(sid, target=1000):
    asyncio.run(handle_call_tool("import_netlist", {"text": _TIMER_NETLIST, "session": sid}))
    asyncio.run(handle_call_tool(
        "design_framework", {"design": {"TIM3": {"update_hz": target}}, "session": sid}))


def test_solve_timer_requires_design():
    result = json.loads(asyncio.run(handle_call_tool(
        "solve_timer", {"timer": "TIM3", "session": "tim-nodesign"}))[0].text)
    assert result["ok"] is False
    assert result["error"]["code"] == "no_design"


def test_solve_timer_needs_clock_first_is_honest():
    sid = "tim-noclock"
    _seed_timer_design(sid)
    result = json.loads(asyncio.run(handle_call_tool("solve_timer", {"session": sid}))[0].text)
    assert result["ok"] is True
    assert result["data"]["solved_count"] == 0
    assert result["data"]["results"][0]["unresolved"][0]["type"] == "no_clock_solution"

    # The rendered timer init still carries the honest Prescaler/Period TODO.
    rendered = json.loads(asyncio.run(handle_call_tool("render_framework", {"session": sid}))[0].text)
    source = next(f["content"] for f in rendered["data"]["files"] if f["path"] == "bsp_init.c")
    assert "TODO: set htim3.Init.Prescaler" in source


def test_solve_timer_fills_prescaler_period_and_renders():
    sid = "tim-happy"
    _seed_timer_design(sid, target=1000)
    asyncio.run(handle_call_tool(
        "solve_clock_tree", {"source": "HSI", "sysclk_hz": 80_000_000, "session": sid}))

    solved = json.loads(asyncio.run(handle_call_tool("solve_timer", {"timer": "TIM3", "session": sid}))[0].text)
    assert solved["ok"] is True
    assert solved["data"]["solved_count"] == 1
    res = solved["data"]["results"][0]
    assert res["feasible"] and res["exact"] and res["bus"] == "apb1"
    assert (res["psc"] + 1) * (res["arr"] + 1) == 80_000  # 80 MHz TIMxCLK / 1 kHz

    # render_framework now emits concrete PSC/ARR instead of a TODO.
    rendered = json.loads(asyncio.run(handle_call_tool("render_framework", {"session": sid}))[0].text)
    source = next(f["content"] for f in rendered["data"]["files"] if f["path"] == "bsp_init.c")
    assert f"htim3.Init.Prescaler = {res['psc']};" in source
    assert f"htim3.Init.Period = {res['arr']};" in source
    assert "TODO: set htim3.Init.Prescaler" not in source

    # And the plan's unresolved list no longer flags TIM3 Prescaler/Period.
    unresolved = json.loads(asyncio.run(handle_call_tool(
        "describe_framework", {"what": "unresolved", "session": sid}))[0].text)
    tim_params = [u for u in unresolved["data"]["unresolved"]
                  if u.get("peripheral") == "TIM3" and u.get("type") == "param_unresolved"]
    assert tim_params == []


def test_solve_timer_explicit_clock_whatif_without_loading():
    sid = "tim-whatif"
    _seed_timer_design(sid, target=1000)
    # No clock solved, but an explicit TIMxCLK enables a pure what-if; load=False keeps the plan clean.
    solved = json.loads(asyncio.run(handle_call_tool(
        "solve_timer", {"timer": "TIM3", "timer_clock_hz": 84_000_000, "load": False, "session": sid}))[0].text)
    assert solved["data"]["solved_count"] == 1
    assert solved["data"]["loaded"] is False
    res = solved["data"]["results"][0]
    assert (res["psc"] + 1) * (res["arr"] + 1) == 84_000

    # Because load=False, the persisted plan still shows the TODO.
    rendered = json.loads(asyncio.run(handle_call_tool("render_framework", {"session": sid}))[0].text)
    source = next(f["content"] for f in rendered["data"]["files"] if f["path"] == "bsp_init.c")
    assert "TODO: set htim3.Init.Prescaler" in source


def test_solve_timer_no_recorded_target_is_reported():
    sid = "tim-notarget"
    asyncio.run(handle_call_tool("import_netlist", {"text": _TIMER_NETLIST, "session": sid}))
    asyncio.run(handle_call_tool("design_framework", {"session": sid}))  # no timer target recorded
    result = json.loads(asyncio.run(handle_call_tool("solve_timer", {"timer": "TIM3", "session": sid}))[0].text)
    assert result["ok"] is True
    assert result["data"]["solved_count"] == 0
    assert "no recorded target" in result["data"]["detail"]

# --- NVIC interrupt backbone (Pillar D Tier 3) ------------------------------


def test_design_framework_nvic_renders_calls_and_isr():
    sid = "nvic-happy"
    asyncio.run(handle_call_tool("import_netlist", {"text": _KICAD_NETLIST, "session": sid}))
    design = {"USART1": {"baud": 115200, "nvic_priority": 5}, "I2C1": {"nvic": True}}
    designed = json.loads(asyncio.run(handle_call_tool(
        "design_framework", {"design": design, "session": sid}))[0].text)
    assert designed["ok"] is True
    usart = next(p for p in designed["data"]["peripherals"] if p["name"] == "USART1")
    assert usart["nvic"]["irqns"] == ["USART1_IRQn"]

    rendered = json.loads(asyncio.run(handle_call_tool("render_framework", {"session": sid}))[0].text)
    source = next(f["content"] for f in rendered["data"]["files"] if f["path"] == "bsp_init.c")
    assert "HAL_NVIC_SetPriority(USART1_IRQn, 5, 0);" in source
    assert "HAL_NVIC_EnableIRQ(USART1_IRQn);" in source
    assert "void USART1_IRQHandler(void)" in source
    assert "HAL_UART_IRQHandler(&huart1);" in source
    # I2C default-enabled -> both event/error vectors + a review note.
    assert "HAL_NVIC_EnableIRQ(I2C1_EV_IRQn);" in source
    assert "HAL_NVIC_EnableIRQ(I2C1_ER_IRQn);" in source
    assert "default priority" in source


def test_design_framework_nvic_unknown_vector_is_surfaced_not_guessed():
    sid = "nvic-unresolved"
    # Advanced-timer TIM1 has an irregular vector deliberately left out of the
    # built-in table, so a bare nvic request must surface honestly (never guessed).
    tim1_net = _TIMER_NETLIST.replace("/TIM3_CH1", "/TIM1_CH1")
    asyncio.run(handle_call_tool("import_netlist", {"text": tim1_net, "session": sid}))
    designed = json.loads(asyncio.run(handle_call_tool(
        "design_framework", {"design": {"TIM1": {"nvic": True}}, "session": sid}))[0].text)
    assert designed["ok"] is True
    tim1 = next(p for p in designed["data"]["peripherals"] if p["name"] == "TIM1")
    assert tim1["nvic"]["requested"] is True
    assert tim1["nvic"]["resolved"] is False
    assert tim1["nvic"]["irqns"] == []

    unresolved = json.loads(asyncio.run(handle_call_tool(
        "describe_framework", {"what": "unresolved", "session": sid}))[0].text)
    assert any(u["type"] == "nvic_unresolved" and u["peripheral"] == "TIM1"
               for u in unresolved["data"]["unresolved"])

    rendered = json.loads(asyncio.run(handle_call_tool("render_framework", {"session": sid}))[0].text)
    source = next(f["content"] for f in rendered["data"]["files"] if f["path"] == "bsp_init.c")
    assert "TODO: enable TIM1 interrupt" in source
    assert "HAL_NVIC_EnableIRQ" not in source

# --- DMA association (Pillar D Tier 3) --------------------------------------


def test_design_framework_dma_renders_streams_link_and_isr():
    sid = "dma-happy"
    asyncio.run(handle_call_tool("import_netlist", {"text": _KICAD_NETLIST, "session": sid}))
    design = {"USART1": {"baud": 115200, "dma": True}, "I2C1": {"dma": True}}
    designed = json.loads(asyncio.run(handle_call_tool(
        "design_framework", {"design": design, "session": sid}))[0].text)
    assert designed["ok"] is True
    usart = next(p for p in designed["data"]["peripherals"] if p["name"] == "USART1")
    assert {s["instance"] for s in usart["dma"]["streams"]} == {"DMA1_Channel5", "DMA1_Channel4"}

    rendered = json.loads(asyncio.run(handle_call_tool("render_framework", {"session": sid}))[0].text)
    source = next(f["content"] for f in rendered["data"]["files"] if f["path"] == "bsp_init.c")
    assert "__HAL_RCC_DMA1_CLK_ENABLE();" in source
    assert "hdma_usart1_rx.Init.Request = DMA_REQUEST_2;" in source
    assert "__HAL_LINKDMA(&huart1, hdmarx, hdma_usart1_rx);" in source
    assert "void DMA1_Channel5_IRQHandler(void)" in source
    assert "HAL_DMA_IRQHandler(&hdma_usart1_rx);" in source
    # I2C1 gets its own EV/ER-independent DMA channels too.
    assert "__HAL_LINKDMA(&hi2c1, hdmarx, hdma_i2c1_rx);" in source


def test_design_framework_dma_unmapped_is_surfaced_not_guessed():
    sid = "dma-unresolved"
    # USART2 is a real L4 peripheral deliberately left out of the built-in DMA table.
    net = _KICAD_NETLIST.replace('(name "/USART1_TX")', '(name "/USART2_TX")')
    asyncio.run(handle_call_tool("import_netlist", {"text": net, "session": sid}))
    asyncio.run(handle_call_tool("design_framework", {"design": {"USART2": {"dma": True}}, "session": sid}))
    unresolved = json.loads(asyncio.run(handle_call_tool(
        "describe_framework", {"what": "unresolved", "session": sid}))[0].text)
    assert any(u["type"] == "dma_unresolved" and u["peripheral"] == "USART2"
               for u in unresolved["data"]["unresolved"])
    rendered = json.loads(asyncio.run(handle_call_tool("render_framework", {"session": sid}))[0].text)
    source = next(f["content"] for f in rendered["data"]["files"] if f["path"] == "bsp_init.c")
    assert "USART2 rx DMA requested but stream unknown" in source
    assert "HAL_DMA_Init" not in source

# --- Product-spec entry (controlled vocabulary -> design params) -------------


def test_import_spec_then_design_from_spec_renders_translated_hal_macros():
    sid = "spec-happy"
    asyncio.run(handle_call_tool("import_netlist", {"text": _KICAD_NETLIST, "session": sid}))
    spec = {"USART1": {"baud": 115200, "framing": "8E1", "direction": "txrx"},
            "I2C1": {"addressing": "10bit"}}
    imported = json.loads(asyncio.run(handle_call_tool("import_spec", {"spec": spec, "session": sid}))[0].text)
    assert imported["ok"] is True
    data = imported["data"]
    assert data["cross_checked"] is True
    assert data["conflicts"] == []
    # 8E1 -> HAL folds the parity bit into WordLength.
    assert data["design"]["USART1"]["word_length"] == "UART_WORDLENGTH_9B"
    assert data["design"]["USART1"]["parity"] == "UART_PARITY_EVEN"

    designed = json.loads(asyncio.run(handle_call_tool(
        "design_framework", {"from_spec": True, "session": sid}))[0].text)
    assert designed["ok"] is True
    rendered = json.loads(asyncio.run(handle_call_tool("render_framework", {"session": sid}))[0].text)
    source = next(f["content"] for f in rendered["data"]["files"] if f["path"] == "bsp_init.c")
    assert "huart1.Init.BaudRate = 115200;" in source
    assert "huart1.Init.WordLength = UART_WORDLENGTH_9B;" in source
    assert "huart1.Init.Parity = UART_PARITY_EVEN;" in source
    assert "huart1.Init.Mode = UART_MODE_TX_RX;" in source
    assert "hi2c1.Init.AddressingMode = I2C_ADDRESSINGMODE_10BIT;" in source


def test_import_spec_flags_peripheral_absent_from_netlist():
    sid = "spec-conflict"
    asyncio.run(handle_call_tool("import_netlist", {"text": _KICAD_NETLIST, "session": sid}))
    imported = json.loads(asyncio.run(handle_call_tool(
        "import_spec", {"spec": {"SPI2": {"role": "master"}}, "session": sid}))[0].text)
    data = imported["data"]
    assert any(c["peripheral"] == "SPI2" for c in data["conflicts"])
    assert "SPI2" not in data["design"]  # no code for hardware that is not wired


def test_design_framework_from_spec_without_import_errors():
    sid = "spec-missing"
    asyncio.run(handle_call_tool("import_netlist", {"text": _KICAD_NETLIST, "session": sid}))
    res = json.loads(asyncio.run(handle_call_tool(
        "design_framework", {"from_spec": True, "session": sid}))[0].text)
    assert res["ok"] is False
    assert res["error"]["code"] == "no_spec"


def test_design_framework_from_spec_explicit_design_overrides():
    sid = "spec-merge"
    asyncio.run(handle_call_tool("import_netlist", {"text": _KICAD_NETLIST, "session": sid}))
    asyncio.run(handle_call_tool("import_spec", {"spec": {"USART1": {"baud": 9600}}, "session": sid}))
    asyncio.run(handle_call_tool(
        "design_framework", {"from_spec": True, "design": {"USART1": {"baud": 115200}}, "session": sid}))
    rendered = json.loads(asyncio.run(handle_call_tool("render_framework", {"session": sid}))[0].text)
    source = next(f["content"] for f in rendered["data"]["files"] if f["path"] == "bsp_init.c")
    assert "huart1.Init.BaudRate = 115200;" in source
    assert "9600" not in source

# --- DB-derived GPIO alternate-function resolution (Pillar D Tier 3) ---------


def test_design_framework_derives_af_from_db(tmp_path):
    sid = "design-af-db"
    asyncio.run(handle_call_tool("import_netlist", {"text": _KICAD_NETLIST, "session": sid}))
    db = tmp_path / "pins.json"
    db.write_text(json.dumps({
        "STM32L431": {
            "PA9": [{"peripheral": "USART1", "signal": "TX", "af": 7}],
            "PB6": [{"peripheral": "I2C1", "signal": "SCL", "af": 4}],
        }
    }), encoding="utf-8")

    designed = json.loads(asyncio.run(handle_call_tool(
        "design_framework",
        {"design": {"USART1": {"baud": 115200}}, "db_path": str(db), "session": sid}))[0].text)
    assert designed["ok"] is True

    rendered = json.loads(asyncio.run(handle_call_tool("render_framework", {"session": sid}))[0].text)
    blob = "\n".join(f["content"] for f in rendered["data"]["files"])
    # AF numbers are transcribed from the DB, and the datasheet TODO disappears.
    assert "GPIO_AF7_USART1" in blob
    assert "GPIO_AF4_I2C1" in blob
    assert "TODO: GPIO_InitStruct.Alternate" not in blob


def test_design_framework_explicit_af_map_overrides_db(tmp_path):
    sid = "design-af-override"
    asyncio.run(handle_call_tool("import_netlist", {"text": _KICAD_NETLIST, "session": sid}))
    db = tmp_path / "pins.json"
    db.write_text(json.dumps({
        "STM32L431": {"PA9": [{"peripheral": "USART1", "signal": "TX", "af": 7}]}
    }), encoding="utf-8")

    asyncio.run(handle_call_tool(
        "design_framework",
        {"design": {"USART1": {"baud": 115200}},
         "af_map": {"STM32L431": {"PA9": {"USART1_TX": 3}}},
         "db_path": str(db), "session": sid}))

    rendered = json.loads(asyncio.run(handle_call_tool("render_framework", {"session": sid}))[0].text)
    blob = "\n".join(f["content"] for f in rendered["data"]["files"])
    assert "GPIO_AF3_USART1" in blob
    assert "GPIO_AF7_USART1" not in blob


def test_design_framework_missing_db_entry_stays_honest(tmp_path):
    sid = "design-af-partial"
    asyncio.run(handle_call_tool("import_netlist", {"text": _KICAD_NETLIST, "session": sid}))
    db = tmp_path / "pins.json"
    # DB knows PA9 but not PB6 -> PB6 must stay an unresolved TODO, never guessed.
    db.write_text(json.dumps({
        "STM32L431": {"PA9": [{"peripheral": "USART1", "signal": "TX", "af": 7}]}
    }), encoding="utf-8")

    asyncio.run(handle_call_tool(
        "design_framework",
        {"design": {"USART1": {"baud": 115200}}, "db_path": str(db), "session": sid}))

    rendered = json.loads(asyncio.run(handle_call_tool("render_framework", {"session": sid}))[0].text)
    blob = "\n".join(f["content"] for f in rendered["data"]["files"])
    assert "GPIO_AF7_USART1" in blob
    assert "TODO: GPIO_InitStruct.Alternate for I2C1_SCL" in blob


def test_design_framework_bad_db_path_is_honest(tmp_path):
    sid = "design-af-baddb"
    asyncio.run(handle_call_tool("import_netlist", {"text": _KICAD_NETLIST, "session": sid}))
    result = json.loads(asyncio.run(handle_call_tool(
        "design_framework",
        {"db_path": str(tmp_path / "nope.json"), "session": sid}))[0].text)
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_db"


# --- load_device_pack (Pillar F: data-driven device facts) -------------------

def _pack_for(family="STM32ZZ"):
    return {
        "schema": device_packs.SCHEMA,
        "family": family,
        "clock": {"profiles": [
            {"match_lines": [family], "profile": {"family": family, "max_sysclk_hz": 80_000_000}}]},
        "dma": {
            "arch": {"unit": "Stream", "select_field": "Channel", "select_prefix": "DMA_CHANNEL_"},
            "map": {"SPI1": {"rx": [2, 0, 3], "tx": [2, 3, 3]}}},
        "nvic": {"i2c_dual": True, "irq": {"TIM2": ["TIM2_IRQn"]}},
        "timer": {"apb2": ["TIM1"], "bits32": ["TIM2"]},
    }


def test_load_device_pack_reports_coverage():
    payload = _payload(asyncio.run(handle_call_tool("load_device_pack", {})))
    assert payload["ok"] is True
    assert payload["data"]["action"] == "coverage"
    assert "STM32F4" in payload["data"]["coverage"]["builtin"]
    assert "STM32L4" in payload["data"]["coverage"]["builtin"]


def test_load_device_pack_registers_family_and_drives_synthesis():
    try:
        payload = _payload(asyncio.run(handle_call_tool("load_device_pack", {"pack": _pack_for()})))
        assert payload["ok"] is True
        assert payload["data"]["action"] == "registered"
        assert payload["data"]["family"] == "STM32ZZ"
        assert payload["data"]["sections"] == ["clock", "dma", "nvic", "timer"]
        # The freshly-registered family is now a first-class deterministic fact source.
        assert "STM32ZZ" in device_packs.dma_families()
        assert device_packs.nvic_table("STM32ZZ")["TIM2"] == ["TIM2_IRQn"]
    finally:
        device_packs.reset_external()


def test_load_device_pack_rejects_invalid_pack():
    try:
        bad = {"schema": "wrong", "family": "NRF52"}
        payload = _payload(asyncio.run(handle_call_tool("load_device_pack", {"pack": bad})))
        assert payload["ok"] is False
        assert payload["error"]["code"] == "invalid_pack"
        problems = payload["raw_response"]["problems"]
        assert any("schema" in p for p in problems)
        assert any("family" in p for p in problems)
        # Nothing half-loaded.
        assert "NRF52" not in device_packs.coverage()["families"]
    finally:
        device_packs.reset_external()


def test_load_device_pack_refuses_builtin_shadow():
    try:
        payload = _payload(asyncio.run(handle_call_tool("load_device_pack", {"pack": _pack_for("STM32F4")})))
        assert payload["ok"] is False
        assert payload["error"]["code"] == "invalid_pack"
        assert any("built-in" in p for p in payload["raw_response"]["problems"])
        # Override lets it through.
        ok = _payload(asyncio.run(handle_call_tool(
            "load_device_pack", {"pack": _pack_for("STM32F4"), "allow_override": True})))
        assert ok["ok"] is True and ok["data"]["action"] == "registered"
    finally:
        device_packs.reset_external()


def test_load_device_pack_loads_from_path(tmp_path):
    try:
        path = tmp_path / "pack.json"
        path.write_text(json.dumps(_pack_for("STM32YY")), encoding="utf-8")
        payload = _payload(asyncio.run(handle_call_tool("load_device_pack", {"path": str(path)})))
        assert payload["ok"] is True
        assert payload["data"]["family"] == "STM32YY"
    finally:
        device_packs.reset_external()


def test_load_device_pack_bad_path_is_honest():
    payload = _payload(asyncio.run(handle_call_tool("load_device_pack", {"path": "no-such-file.json"})))
    assert payload["ok"] is False
    assert payload["error"]["code"] == "pack_unreadable"


def test_run_pipeline_registered_as_capstone_tool():
    tool = next(t for t in asyncio.run(handle_list_tools()) if t.name == "run_pipeline")
    props = tool.inputSchema["properties"]
    # It advertises the whole design-half surface in one tool.
    for key in ("netlist", "spec", "sysclk_hz", "af_map", "irq_map", "gpio_map", "synthesize"):
        assert key in props


def test_run_pipeline_end_to_end_from_netlist_and_spec():
    # One call: netlist + product spec -> plan -> render -> acceptance, with every
    # stage's honest gaps aggregated into one list.
    result = _payload(asyncio.run(handle_call_tool("run_pipeline", {
        "netlist": {"text": _KICAD_NETLIST},
        "spec": {"USART1": {"baud": 115200}},
        "session": "pipe-e2e",
    })))
    assert result["ok"] is True
    data = result["data"]

    # The design DAG ran in order; the two input-gated stages were honestly skipped.
    assert data["ran"] == ["import_netlist", "import_spec", "design_framework",
                           "render_framework", "synthesize_acceptance"]
    assert {s["stage"] for s in data["skipped"]} == {"solve_clock_tree", "solve_timer"}

    # No af_map supplied -> PA9's alternate function is an honest, aggregated gap,
    # tagged with the stage that produced it. Nothing is invented.
    assert data["pipeline_status"] == "complete_with_unresolved"
    assert data["unresolved_count"] == len(data["unresolved"])
    assert data["unresolved_count"] >= 1
    assert any(gap["stage"] == "design_framework" for gap in data["unresolved"])

    # The engineer gets the actual products in hand: MCU, rendered files, acceptance spec.
    assert data["mcu"]["line"] == "STM32L431"
    assert data["files"] and all("path" in f and "content" in f for f in data["files"])
    assert any(f["path"].endswith(".c") for f in data["files"])
    assert data["acceptance"]["check_count"] >= 1


def test_run_pipeline_blocked_is_honest_without_board():
    # No netlist and no board in the session -> the required design stage fails; the
    # pipeline reports that honestly as blocked rather than guessing a board.
    result = _payload(asyncio.run(handle_call_tool("run_pipeline", {"session": "pipe-blocked"})))
    assert result["ok"] is True
    data = result["data"]
    assert data["pipeline_status"] == "blocked"
    assert data["blocked"]["stage"] == "design_framework"
    assert data["blocked"]["code"] == "no_board"
    assert data["ran"] == ["design_framework"]
    assert "files" not in data and "acceptance" not in data


def test_run_pipeline_synthesize_false_stops_after_render():
    # synthesize=false hands off a flashable skeleton without a machine judge.
    result = _payload(asyncio.run(handle_call_tool("run_pipeline", {
        "netlist": {"text": _KICAD_NETLIST},
        "spec": {"USART1": {"baud": 115200}},
        "af_map": {"STM32L431": {"PA9": {"USART1_TX": 7}}},
        "synthesize": False,
        "session": "pipe-nosynth",
    })))
    assert result["ok"] is True
    data = result["data"]
    assert "synthesize_acceptance" not in data["ran"]
    assert any(s["stage"] == "synthesize_acceptance" for s in data["skipped"])
    assert "acceptance" not in data
    assert data["files"]  # render still produced the skeleton
