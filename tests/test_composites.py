import pytest

from mcp_server.composites import capture_state, debug_until, flash_and_run, run_for_duration
from mcp_server.sampling import sample_expressions


class FakeClient:
    """Records calls and returns canned, already-decoded values."""

    def __init__(self, stop_reason="breakpoint-hit"):
        self.calls = []
        self._stop_reason = stop_reason
        self.expressions = {"rx_count": "42"}

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

    def continue_execution(self):
        self.calls.append(("continue_execution",))
        return [{"message": "running"}]

    def halt_execution(self):
        self.calls.append(("halt_execution",))
        return [{"message": "stopped"}]

    def read_variable(self, expression):
        self.calls.append(("read_variable", expression))
        return [{"payload": {"value": self.expressions[expression]}}]


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


def test_run_for_duration_continues_sleeps_halts_and_captures_expressions():
    client = FakeClient()
    sleeps = []
    clock = iter([10.0, 55.0])

    result = run_for_duration(
        client,
        duration_sec=45.0,
        capture={"expressions": ["rx_count"]},
        sleep=sleeps.append,
        monotonic=lambda: next(clock),
    )

    assert client.calls[:2] == [("continue_execution",), ("halt_execution",)]
    assert sleeps == [45.0]
    assert result["elapsed_sec"] == 45.0
    assert result["halt"]["method"] == "halt_execution"
    assert result["final_frame"]["func"] == "trigger_divzero"
    assert result["capture"]["expressions"]["values"][0]["value"] == 42


def test_run_for_duration_can_recover_after_halt_failure_before_capture():
    class FlakyHaltClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.halt_attempts = 0

        def halt_execution(self):
            self.halt_attempts += 1
            self.calls.append(("halt_execution", self.halt_attempts))
            if self.halt_attempts == 1:
                raise RuntimeError("target_unresponsive")
            return [{"message": "stopped"}]

    client = FlakyHaltClient()
    recoveries = []

    result = run_for_duration(
        client,
        duration_sec=1.0,
        capture={"expressions": ["rx_count"]},
        recover=lambda: recoveries.append("recover_session"),
        sleep=lambda _: None,
        monotonic=lambda: 0.0,
    )

    assert recoveries == ["recover_session"]
    assert client.halt_attempts == 2
    assert result["halt"]["method"] == "recover_session+halt_execution"
    assert result["capture"]["expressions"]["values"][0]["expression"] == "rx_count"


def test_sample_expressions_collects_time_series_and_summary_without_halting():
    client = FakeClient()
    client.expressions["rx_count"] = "10"
    sleeps = []
    times = iter([100.0, 100.5, 101.0])

    def sleep(seconds):
        sleeps.append(seconds)
        client.expressions["rx_count"] = str(int(client.expressions["rx_count"]) + 2)

    result = sample_expressions(
        client,
        duration_sec=1.0,
        interval_sec=0.5,
        expressions=["rx_count"],
        sleep=sleep,
        monotonic=lambda: next(times),
    )

    assert sleeps == [0.5, 0.5]
    assert [sample["t_sec"] for sample in result["series"]] == [0.0, 0.5, 1.0]
    assert [sample["values"]["rx_count"] for sample in result["series"]] == [10, 12, 14]
    assert result["summary"]["rx_count"] == {
        "sample_count": 3,
        "error_count": 0,
        "first": 10,
        "last": 14,
        "min": 10,
        "max": 14,
        "delta": 4,
    }
    assert result["timing"]["requested_interval_sec"] == 0.5
    assert result["timing"]["sample_count"] == 3
    assert not any(call[0] == "halt_execution" for call in client.calls)


def test_sample_expressions_records_read_errors_per_expression():
    class RunningReadBlockedClient(FakeClient):
        def read_variable(self, expression):
            self.calls.append(("read_variable", expression))
            raise RuntimeError("target_unresponsive")

    client = RunningReadBlockedClient()

    result = sample_expressions(
        client,
        duration_sec=0.0,
        interval_sec=0.25,
        expressions=["rx_count"],
        sleep=lambda _: None,
        monotonic=lambda: 10.0,
    )

    assert result["series"] == [
        {
            "index": 0,
            "t_sec": 0.0,
            "values": {},
            "raw": {},
            "errors": {"rx_count": "target_unresponsive"},
        }
    ]
    assert result["summary"]["rx_count"]["sample_count"] == 1
    assert result["summary"]["rx_count"]["error_count"] == 1


def test_sample_expressions_budget_counts_final_partial_interval_sample():
    client = FakeClient()
    times = iter([10.0, 10.3, 10.6, 10.9, 11.0])

    with pytest.raises(ValueError, match="5 samples requested"):
        sample_expressions(
            client,
            duration_sec=1.0,
            interval_sec=0.3,
            expressions=["rx_count"],
            max_samples=4,
            sleep=lambda _: None,
            monotonic=lambda: next(times),
        )


def test_sample_expressions_requires_expressions_or_table():
    client = FakeClient()

    with pytest.raises(ValueError, match="expressions or table"):
        sample_expressions(
            client,
            duration_sec=0.0,
            interval_sec=0.5,
            sleep=lambda _: None,
            monotonic=lambda: 10.0,
        )


def test_run_for_duration_samples_before_final_halt_and_capture():
    client = FakeClient()
    sleeps = []
    times = iter([10.0, 10.0, 10.25, 10.5, 10.5])

    def sleep(seconds):
        sleeps.append(seconds)
        client.expressions["rx_count"] = str(int(client.expressions["rx_count"]) + 1)

    result = run_for_duration(
        client,
        duration_sec=0.5,
        sample={"interval_sec": 0.25, "expressions": ["rx_count"]},
        capture={"expressions": ["rx_count"]},
        sleep=sleep,
        monotonic=lambda: next(times),
    )

    first_halt = client.calls.index(("halt_execution",))
    sample_reads = [index for index, call in enumerate(client.calls) if call == ("read_variable", "rx_count")]
    assert client.calls[0] == ("continue_execution",)
    assert all(index < first_halt for index in sample_reads[:3])
    assert sample_reads[-1] > first_halt
    assert result["sample"]["series"][0]["values"]["rx_count"] == 42
    assert result["sample"]["summary"]["rx_count"]["sample_count"] == 3
    assert result["capture"]["expressions"]["values"][0]["value"] == 44
