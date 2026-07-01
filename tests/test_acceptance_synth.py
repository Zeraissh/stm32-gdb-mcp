from mcp_server.acceptance_eval import evaluate_acceptance
from mcp_server.acceptance_model import validate_acceptance_spec
from mcp_server.acceptance_synth import (
    derive_acceptance_spec,
    dict_clock_resolver,
    svd_clock_resolver,
)
from mcp_server.framework_solver import build_framework_plan


def _fn(peripheral, signal):
    return {"peripheral": peripheral, "signal": signal}


def _pin(port_pin, net, function):
    return {"package_pin": "0", "port_pin": port_pin, "net": net, "function": function}


def _plan():
    board = {"mcu": {"part_normalized": "STM32L431CBT6", "family": "STM32L4", "line": "STM32L431", "pins": [
        _pin("PA9", "/USART1_TX", _fn("USART1", "TX")),
        _pin("PA10", "/USART1_RX", _fn("USART1", "RX")),
        _pin("PB6", "/I2C1_SCL", _fn("I2C1", "SCL")),
    ]}}
    return build_framework_plan(board)


# --- no_fault is always present and needs no target data --------------------


def test_no_fault_check_always_emitted():
    result = derive_acceptance_spec(_plan(), clock_resolver=None)
    kinds = [c["kind"] for c in result["spec"]["checks"]]

    assert "no_fault" in kinds
    assert result["stats"]["clock_checks"] == 0
    assert any("no clock resolver" in n.lower() for n in result["notes"])


def test_derived_spec_is_valid_per_acceptance_model():
    result = derive_acceptance_spec(_plan(), clock_resolver=None)
    normalized = validate_acceptance_spec(result["spec"])  # must not raise

    assert normalized["checks"][0]["kind"] == "no_fault"


# --- clock-enable checks are resolved, never guessed ------------------------


def test_unresolved_clocks_are_surfaced_not_faked():
    result = derive_acceptance_spec(_plan(), clock_resolver=lambda name: None)
    clock_ids = [c["id"] for c in result["spec"]["checks"] if c["kind"] == "memory_u32"]

    assert clock_ids == []  # nothing fabricated
    resolved_names = {u["clock"] for u in result["unresolved"]}
    assert {"GPIOA", "GPIOB", "USART1", "I2C1"} <= resolved_names


def test_dict_resolver_emits_bits_set_checks():
    register_map = {"STM32L431": {
        "USART1": {"address": "0x40021060", "bit": 14},
        "GPIOA": {"address": "0x4002104C", "bit": 0},
    }}
    resolver = dict_clock_resolver(register_map, "STM32L431", "STM32L4")
    result = derive_acceptance_spec(_plan(), clock_resolver=resolver)

    by_id = {c["id"]: c for c in result["spec"]["checks"]}
    usart = by_id["clk_USART1_enabled"]
    assert usart["kind"] == "memory_u32"
    assert usart["address"] == "0x40021060"
    assert usart["op"] == "bits_set"
    assert usart["expect"] == "0x00004000"  # 1 << 14
    # I2C1 not in the map -> unresolved, not invented.
    assert "clk_I2C1_enabled" not in by_id
    assert any(u["clock"] == "I2C1" for u in result["unresolved"])


def test_dict_resolver_prefers_line_then_family():
    register_map = {"STM32L4": {"USART1": {"address": "0x50000000", "bit": 3}}}
    resolver = dict_clock_resolver(register_map, "STM32L431", "STM32L4")  # line absent -> family
    result = derive_acceptance_spec(_plan(), clock_resolver=resolver)

    usart = next(c for c in result["spec"]["checks"] if c["id"] == "clk_USART1_enabled")
    assert usart["address"] == "0x50000000"
    assert usart["expect"] == "0x00000008"  # 1 << 3


def test_integer_address_is_normalized_to_hex():
    resolver = dict_clock_resolver({"STM32L431": {"USART1": {"address": 0x40021060, "bit": 14}}},
                                   "STM32L431", "STM32L4")
    result = derive_acceptance_spec(_plan(), clock_resolver=resolver)
    usart = next(c for c in result["spec"]["checks"] if c["id"] == "clk_USART1_enabled")
    assert usart["address"] == "0x40021060"


# --- SVD-backed resolver (fake parser) --------------------------------------


class _FakeSVD:
    """Minimal stand-in exposing get_register like svd_parser.SVDParser."""

    def __init__(self, registers):
        self._registers = registers  # {(periph, reg): {"address_int", "fields":[...]}}

    def get_register(self, peripheral, register):
        key = (peripheral, register)
        if key not in self._registers:
            raise ValueError(f"no {peripheral}.{register}")
        return self._registers[key]


def test_svd_resolver_finds_enable_bit_across_registers():
    svd = _FakeSVD({
        ("RCC", "APB1ENR1"): {"address_int": 0x40021058, "fields": [
            {"name": "USART1EN", "bit_offset": 14}, {"name": "I2C1EN", "bit_offset": 21}]},
        ("RCC", "AHB2ENR"): {"address_int": 0x4002104C, "fields": [
            {"name": "GPIOAEN", "bit_offset": 0}, {"name": "GPIOBEN", "bit_offset": 1}]},
    })
    resolver = svd_clock_resolver(svd)
    result = derive_acceptance_spec(_plan(), clock_resolver=resolver)

    by_id = {c["id"]: c for c in result["spec"]["checks"]}
    assert by_id["clk_I2C1_enabled"]["address"] == "0x40021058"
    assert by_id["clk_I2C1_enabled"]["expect"] == "0x00200000"  # 1 << 21
    assert by_id["clk_GPIOA_enabled"]["address"] == "0x4002104c"
    assert result["stats"]["unresolved_count"] == 0


def test_svd_resolver_handles_f1_iop_gpio_naming():
    svd = _FakeSVD({("RCC", "APB2ENR"): {"address_int": 0x40021018, "fields": [
        {"name": "IOPAEN", "bit_offset": 2}, {"name": "USART1EN", "bit_offset": 14}]}})
    resolver = svd_clock_resolver(svd)
    result = derive_acceptance_spec(_plan(), clock_resolver=resolver)

    gpioa = next(c for c in result["spec"]["checks"] if c["id"] == "clk_GPIOA_enabled")
    assert gpioa["expect"] == "0x00000004"  # IOPAEN bit 2


def test_svd_resolver_never_raises_on_missing_rcc():
    resolver = svd_clock_resolver(_FakeSVD({}))  # get_register always raises
    result = derive_acceptance_spec(_plan(), clock_resolver=resolver)
    assert result["stats"]["clock_checks"] == 0
    assert all(c["kind"] == "no_fault" for c in result["spec"]["checks"])


# --- options ----------------------------------------------------------------


def test_stopped_at_option_adds_check():
    result = derive_acceptance_spec(_plan(), options={"stopped_at": "main"})
    stopped = next(c for c in result["spec"]["checks"] if c["kind"] == "stopped_at")
    assert stopped["symbol"] == "main"


def test_no_fault_can_be_disabled():
    result = derive_acceptance_spec(_plan(), options={"include_no_fault": False})
    assert all(c["kind"] != "no_fault" for c in result["spec"]["checks"])


# --- end-to-end: derived spec evaluates against a fake reader ----------------


class _FakeReader:
    def __init__(self, memory, faults=None):
        self._memory = memory
        self._faults = faults or {}

    def read_u32(self, address):
        return self._memory[int(str(address), 0)]

    def read_fault_registers(self):
        return self._faults


def test_derived_spec_passes_when_bits_are_set():
    resolver = dict_clock_resolver({"STM32L431": {
        "USART1": {"address": "0x40021060", "bit": 14}}}, "STM32L431", "STM32L4")
    spec = validate_acceptance_spec(derive_acceptance_spec(_plan(), clock_resolver=resolver)["spec"])
    reader = _FakeReader(memory={0x40021060: (1 << 14)})

    report = evaluate_acceptance(spec, reader)
    usart = next(r for r in report["results"] if r["id"] == "clk_USART1_enabled")
    assert usart["status"] == "pass"


def test_derived_spec_fails_when_clock_bit_clear():
    resolver = dict_clock_resolver({"STM32L431": {
        "USART1": {"address": "0x40021060", "bit": 14}}}, "STM32L431", "STM32L4")
    spec = validate_acceptance_spec(derive_acceptance_spec(_plan(), clock_resolver=resolver)["spec"])
    reader = _FakeReader(memory={0x40021060: 0x0})

    report = evaluate_acceptance(spec, reader)
    usart = next(r for r in report["results"] if r["id"] == "clk_USART1_enabled")
    assert usart["status"] == "fail"
