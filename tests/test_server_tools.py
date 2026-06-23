import asyncio
import json

from mcp_server.server import handle_call_tool, handle_list_tools


def test_server_provides_workflow_instructions():
    from mcp_server.server import server

    instructions = server.instructions
    assert instructions and "self_check" in instructions
    assert "HALTED" in instructions
    assert "recover_session" in instructions


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
    assert "read_freertos" in tool_names
    assert "capture_rtos_snapshot" in tool_names
    assert "start_logging" in tool_names
    assert "stop_logging" in tool_names
    assert "get_logs" in tool_names
    assert "clear_logs" in tool_names
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


def test_unified_logging_tools_handle_all_channels():
    tools = asyncio.run(handle_list_tools())
    start = next(t for t in tools if t.name == "start_logging")
    assert set(start.inputSchema["properties"]["channel"]["enum"]) == {"rtt", "swo", "uart"}

    # the 12 old per-channel logging tools are gone (consolidated)
    names = {t.name for t in tools}
    for old in ("start_rtt_logging", "start_swo_logging", "start_uart_logging",
                "get_uart_logs", "clear_rtt_logs"):
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
        "step", "run_to_line", "disassemble",
        "list_functions", "list_variables", "lookup_type", "sizeof", "address_of",
        "capture_coredump", "load_coredump", "verify_flash",
        "read_cycle_counter", "sample_pc",
    ):
        assert expected in tool_names
    # old per-kind step tools merged into `step`
    assert "step_out" not in tool_names and "step_into" not in tool_names


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
    # full mode still exposes everything
    monkeypatch.delenv("STM32_GDB_MCP_COMPACT")
    assert len(asyncio.run(handle_list_tools())) > 80


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
