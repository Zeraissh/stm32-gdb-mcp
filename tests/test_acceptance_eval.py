from mcp_server.acceptance_eval import evaluate_acceptance
from mcp_server.acceptance_model import validate_acceptance_spec


class FakeReader:
    """A dict-backed reader implementing the acceptance reader protocol."""

    def __init__(self, memory=None, variables=None, registers=None, fault_registers=None, symbols=None):
        self.memory = memory or {}
        self.variables = variables or {}
        self.registers = registers or {}
        self.fault_registers = fault_registers or {"CFSR": 0, "HFSR": 0, "BFAR": 0, "MMFAR": 0}
        self.symbols = symbols or {}

    def _key(self, address):
        return address if isinstance(address, int) else int(address, 0)

    def read_u32(self, address):
        key = self._key(address)
        if key not in self.memory:
            raise ValueError(f"cannot read memory at {address}")
        return self.memory[key]

    def read_variable(self, name):
        if name not in self.variables:
            raise ValueError(f"no such variable {name}")
        return self.variables[name]

    def read_register(self, name):
        return self.registers[name]

    def read_fault_registers(self):
        return self.fault_registers

    def symbolize(self, address):
        return self.symbols.get(address, "")


def _run(checks, reader):
    spec = validate_acceptance_spec({"checks": checks})
    return evaluate_acceptance(spec, reader)


def _status_of(report, check_id):
    return next(r["status"] for r in report["results"] if r["id"] == check_id)


def test_memory_u32_eq_pass_and_fail():
    reader = FakeReader(memory={0x40013800: 0x0000200D})
    report = _run([
        {"id": "ok", "kind": "memory_u32", "address": "0x40013800", "expect": "0x200D"},
        {"id": "bad", "kind": "memory_u32", "address": "0x40013800", "expect": "0x1"},
    ], reader)

    assert _status_of(report, "ok") == "pass"
    assert _status_of(report, "bad") == "fail"
    assert report["ok"] is False
    assert report["stats"] == {"total": 2, "passed": 1, "failed": 1, "errored": 0}


def test_memory_u32_mask_and_bits_set():
    reader = FakeReader(memory={0x40013800: 0b1101})
    report = _run([
        {"id": "ue", "kind": "memory_u32", "address": "0x40013800", "mask": "0x1", "op": "bits_set", "expect": "0x1"},
        {"id": "bit1", "kind": "memory_u32", "address": "0x40013800", "op": "bits_clear", "expect": "0x2"},
    ], reader)

    assert _status_of(report, "ue") == "pass"       # bit0 set
    assert _status_of(report, "bit1") == "pass"      # bit1 clear
    assert report["ok"] is True


def test_variable_comparisons():
    reader = FakeReader(variables={"SystemCoreClock": 80000000, "count": 5})
    report = _run([
        {"id": "clk", "kind": "variable", "name": "SystemCoreClock", "expect": 80000000},
        {"id": "count-ge", "kind": "variable", "name": "count", "op": "ge", "expect": 3},
        {"id": "count-ne", "kind": "variable", "name": "count", "op": "ne", "expect": 5},
    ], reader)

    assert _status_of(report, "clk") == "pass"
    assert _status_of(report, "count-ge") == "pass"
    assert _status_of(report, "count-ne") == "fail"


def test_core_register_ge():
    reader = FakeReader(registers={"sp": 0x20001000})
    report = _run([
        {"id": "sp", "kind": "core_register", "register": "sp", "op": "ge", "expect": "0x20000000"},
    ], reader)

    assert _status_of(report, "sp") == "pass"


def test_no_fault_pass_and_fail():
    clean = FakeReader()
    report_ok = _run([{"id": "nf", "kind": "no_fault"}], clean)
    assert _status_of(report_ok, "nf") == "pass"

    # CFSR bit for a usage fault set -> diagnose reports a fault class -> check fails.
    faulted = FakeReader(fault_registers={"CFSR": 1 << 16, "HFSR": 0, "BFAR": 0, "MMFAR": 0})
    report_bad = _run([{"id": "nf", "kind": "no_fault"}], faulted)
    assert _status_of(report_bad, "nf") == "fail"
    assert report_bad["ok"] is False


def test_stopped_at_symbol():
    reader = FakeReader(registers={"pc": 0x08000500}, symbols={0x08000500: "main_loop"})
    report = _run([
        {"id": "reached", "kind": "stopped_at", "symbol": "main_loop"},
        {"id": "wrong", "kind": "stopped_at", "symbol": "Error_Handler"},
    ], reader)

    assert _status_of(report, "reached") == "pass"
    assert _status_of(report, "wrong") == "fail"


def test_unreadable_target_is_error_not_fail():
    reader = FakeReader(memory={})  # address not present -> reader raises
    report = _run([
        {"id": "missing", "kind": "memory_u32", "address": "0x40013800", "expect": "0x1"},
    ], reader)

    result = report["results"][0]
    assert result["status"] == "error"
    assert result["actual"] is None
    assert "cannot read memory" in result["detail"]
    assert report["ok"] is False  # an errored check blocks acceptance
    assert report["stats"]["errored"] == 1


def test_all_pass_is_ok():
    reader = FakeReader(
        memory={0x40013800: 0x1},
        variables={"SystemCoreClock": 80000000},
        registers={"pc": 0x100},
        symbols={0x100: "main"},
    )
    report = _run([
        {"id": "m", "kind": "memory_u32", "address": "0x40013800", "expect": "0x1"},
        {"id": "v", "kind": "variable", "name": "SystemCoreClock", "expect": 80000000},
        {"id": "f", "kind": "no_fault"},
        {"id": "s", "kind": "stopped_at", "symbol": "main"},
    ], reader)

    assert report["ok"] is True
    assert report["stats"]["passed"] == 4


# --- provenance passthrough: non-pass results carry the check's provenance ------

_PROV = {"origin": "clock_enable", "macro": "__HAL_RCC_USART1_CLK_ENABLE",
         "source": {"located": True, "file": "bsp_init.c",
                    "init_fn": "MX_USART1_UART_Init", "line": 80}}


def test_provenance_flows_to_failing_result():
    reader = FakeReader(memory={0x40021060: 0x0})  # bit not set -> fail
    report = _run([{"id": "clk", "kind": "memory_u32", "address": "0x40021060",
                    "expect": "0x4000", "op": "bits_set", "provenance": _PROV}], reader)

    fail = next(r for r in report["results"] if r["id"] == "clk")
    assert fail["status"] == "fail"
    assert fail["provenance"]["source"]["init_fn"] == "MX_USART1_UART_Init"
    assert fail["provenance"]["source"]["line"] == 80


def test_error_result_also_carries_provenance():
    reader = FakeReader(memory={})  # unreadable target -> error, never a silent pass
    report = _run([{"id": "clk", "kind": "memory_u32", "address": "0x40021060",
                    "expect": "0x4000", "op": "bits_set", "provenance": _PROV}], reader)

    err = next(r for r in report["results"] if r["id"] == "clk")
    assert err["status"] == "error"
    assert err["provenance"]["origin"] == "clock_enable"


def test_passing_result_does_not_carry_provenance():
    reader = FakeReader(memory={0x40021060: 0x4000})  # bit set -> pass
    report = _run([{"id": "clk", "kind": "memory_u32", "address": "0x40021060",
                    "expect": "0x4000", "op": "bits_set", "provenance": _PROV}], reader)

    ok = next(r for r in report["results"] if r["id"] == "clk")
    assert ok["status"] == "pass"
    assert "provenance" not in ok


def test_check_without_provenance_yields_no_provenance_key():
    reader = FakeReader(memory={0x40021060: 0x0})
    report = _run([{"id": "clk", "kind": "memory_u32", "address": "0x40021060",
                    "expect": "0x4000", "op": "bits_set"}], reader)

    fail = next(r for r in report["results"] if r["id"] == "clk")
    assert fail["status"] == "fail"
    assert "provenance" not in fail
