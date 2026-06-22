from mcp_server.composites import capture_state, debug_until, flash_and_run


class FakeClient:
    """Records calls and returns canned, already-decoded values."""

    def __init__(self, stop_reason="breakpoint-hit"):
        self.calls = []
        self._stop_reason = stop_reason

    def set_breakpoint(self, location, condition=None, temporary=False, ignore_count=None):
        self.calls.append(("set_breakpoint", location, condition, temporary, ignore_count))
        return [{"message": "bp"}]

    def run_and_wait(self, timeout_sec):
        self.calls.append(("run_and_wait", timeout_sec))
        return {
            "stopped": self._stop_reason != "timeout",
            "reason": self._stop_reason,
            "frame": {"func": "trigger_divzero", "file": "main.c", "line": 21, "addr": "0x08000046"},
            "raw_response": [],
        }

    def read_call_stack_decoded(self):
        self.calls.append(("read_call_stack_decoded",))
        return [{"level": 0, "func": "trigger_divzero", "file": "main.c", "line": 21, "addr": "0x08000046"}]

    def read_frame_variables_decoded(self, level=None):
        self.calls.append(("read_frame_variables_decoded", level))
        return {"g_divisor": "0"}

    def read_core_registers_decoded(self):
        self.calls.append(("read_core_registers_decoded",))
        return {"pc": "0x08000046", "lr": "0xfffffff9", "sp": "0x200040b0"}

    def load_firmware(self, path):
        self.calls.append(("load_firmware", path))
        return [{"message": "flashed"}]

    def reset_halt(self, command="monitor reset halt"):
        self.calls.append(("reset_halt", command))
        return [{"message": "reset"}]


def test_debug_until_sets_temp_conditional_breakpoint_runs_and_gathers_context():
    client = FakeClient()

    result = debug_until(client, location="trigger_divzero", condition="g_divisor == 0", timeout_sec=5.0)

    # one temporary, conditional breakpoint then a single run
    assert ("set_breakpoint", "trigger_divzero", "g_divisor == 0", True, None) in client.calls
    assert ("run_and_wait", 5.0) in client.calls
    assert result["stopped"] is True
    assert result["stop"]["reason"] == "breakpoint-hit"
    assert result["backtrace"][0]["func"] == "trigger_divzero"
    assert result["locals"] == {"g_divisor": "0"}


def test_debug_until_on_timeout_skips_context_gathering():
    client = FakeClient(stop_reason="timeout")

    result = debug_until(client, location="main", timeout_sec=1.0)

    assert result["stopped"] is False
    assert "backtrace" not in result
    # must not try to read frames while the core is running
    assert not any(c[0] == "read_call_stack_decoded" for c in client.calls)


def test_capture_state_bundles_registers_backtrace_and_locals_in_one_call():
    client = FakeClient()

    state = capture_state(client)

    assert state["registers"]["pc"] == "0x08000046"
    assert "pc=0x08000046" in state["summary"]
    assert state["backtrace"][0]["func"] == "trigger_divzero"
    assert state["locals"] == {"g_divisor": "0"}


def test_flash_and_run_flashes_resets_breaks_at_entry_and_runs():
    client = FakeClient()

    result = flash_and_run(client, file_path="fw.elf", run_to="main", timeout_sec=8.0)

    names = [c[0] for c in client.calls]
    assert names.index("load_firmware") < names.index("reset_halt") < names.index("run_and_wait")
    assert ("set_breakpoint", "main", None, True, None) in client.calls
    assert result["flashed"] == "fw.elf"
    assert result["stop"]["frame"]["func"] == "trigger_divzero"
