from mcp_server.debug_snapshot import collect_debug_snapshot


class FakeGdbClient:
    def read_core_registers(self):
        return [{"payload": "r0 0x00000000\npc 0x08001234"}]

    def read_fault_registers(self):
        return {"CFSR": 0x02000000, "HFSR": 0, "BFAR": 0, "MMFAR": 0}

    def read_call_stack(self):
        return [{"payload": {"stack": [{"func": "main"}]}}]

    def disassemble_around_pc(self):
        return [{"payload": "0x08001234 <main+4>: bl HAL_Init"}]

    def get_responses(self, timeout_sec=0.1):
        return [{"type": "notify", "message": "stopped"}]


class FakeGdbServerManager:
    server_type = "openocd"
    port = 3333

    def get_logs(self):
        return "Info : device id = 0x10016413"


def test_collect_debug_snapshot_groups_target_evidence():
    snapshot = collect_debug_snapshot(FakeGdbClient(), FakeGdbServerManager())

    assert snapshot["session"]["server_type"] == "openocd"
    assert snapshot["session"]["port"] == 3333
    assert snapshot["core_registers"][0]["payload"].startswith("r0")
    assert snapshot["fault_registers"]["CFSR"] == "0x02000000"
    assert snapshot["fault_diagnosis"]["active_flags"] == ["DIVBYZERO"]
    assert snapshot["gdb_events"][0]["message"] == "stopped"
    assert "device id" in snapshot["server_logs"]


def test_collect_debug_snapshot_can_include_project_and_rtos_context():
    project = {"mcu": "STM32F407VGTx"}
    rtos = {"detection": {"detected": True, "rtos": "FreeRTOS"}}

    snapshot = collect_debug_snapshot(
        FakeGdbClient(),
        FakeGdbServerManager(),
        project_context=project,
        rtos_context=rtos,
    )

    assert snapshot["project"] == project
    assert snapshot["rtos"] == rtos


def test_collect_debug_snapshot_can_include_log_context():
    logs = {"running": True, "entries": [{"source": "rtt", "line": "boot"}]}

    snapshot = collect_debug_snapshot(
        FakeGdbClient(),
        FakeGdbServerManager(),
        log_context=logs,
    )

    assert snapshot["logs"] == logs
