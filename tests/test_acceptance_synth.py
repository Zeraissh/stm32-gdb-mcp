from mcp_server.acceptance_eval import evaluate_acceptance
from mcp_server.acceptance_model import validate_acceptance_spec
from mcp_server.acceptance_synth import (
    derive_acceptance_spec,
    dict_clock_resolver,
    dict_gpio_resolver,
    dict_irq_resolver,
    svd_clock_resolver,
    svd_gpio_resolver,
    svd_irq_resolver,
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


# --- NVIC ISER checks: arch-standard placement from a resolved IRQ number -----


class _FakeIRQSVD:
    """Minimal stand-in exposing interrupt_numbers() like svd_parser.SVDParser."""

    def __init__(self, numbers):
        self._numbers = numbers

    def interrupt_numbers(self):
        return dict(self._numbers)


def _nvic_plan(design):
    board = {"mcu": {"part_normalized": "STM32L431CBT6", "family": "STM32L4", "line": "STM32L431", "pins": [
        _pin("PA9", "/USART1_TX", _fn("USART1", "TX")),
        _pin("PA10", "/USART1_RX", _fn("USART1", "RX")),
    ]}}
    return build_framework_plan(board, design=design)


def test_nvic_iser_check_from_dict_irq_resolver():
    plan = _nvic_plan({"USART1": {"nvic": True}})
    resolver = dict_irq_resolver({"STM32L431": {"USART1_IRQn": 37}}, "STM32L431", "STM32L4")
    result = derive_acceptance_spec(plan, irq_resolver=resolver)

    nvic = next(c for c in result["spec"]["checks"] if c["id"] == "nvic_USART1_IRQn_enabled")
    assert nvic["kind"] == "memory_u32"
    assert nvic["address"] == "0xe000e104"   # ISER[1] = 0xE000E100 + 4*(37 // 32)
    assert nvic["expect"] == "0x00000020"    # bit 37 % 32 = 5
    assert nvic["op"] == "bits_set"
    assert result["stats"]["nvic_checks"] == 1


def test_nvic_low_irq_lands_in_iser0_and_strips_suffix():
    plan = _nvic_plan({"USART1": {"nvic": True}})
    # key without _IRQn suffix, resolved via family fallback (line absent)
    resolver = dict_irq_resolver({"STM32L4": {"USART1": 6}}, "STM32L431", "STM32L4")
    result = derive_acceptance_spec(plan, irq_resolver=resolver)

    nvic = next(c for c in result["spec"]["checks"] if c["kind"] == "memory_u32")
    assert nvic["address"] == "0xe000e100"   # ISER[0]
    assert nvic["expect"] == "0x00000040"    # 1 << 6


def test_nvic_unresolved_number_is_surfaced_not_faked():
    plan = _nvic_plan({"USART1": {"nvic": True}})
    result = derive_acceptance_spec(plan, irq_resolver=lambda name: None)

    assert all(c["kind"] != "memory_u32" for c in result["spec"]["checks"])
    assert any(u["type"] == "irq_number_unknown" and u["irq"] == "USART1_IRQn"
               for u in result["unresolved"])


def test_no_irq_resolver_notes_but_does_not_spam_unresolved():
    plan = _nvic_plan({"USART1": {"nvic": True}})
    result = derive_acceptance_spec(plan, irq_resolver=None)

    assert result["stats"]["nvic_checks"] == 0
    assert not any(u["type"] == "irq_number_unknown" for u in result["unresolved"])
    assert any("irq resolver" in n.lower() for n in result["notes"])


def test_nvic_dma_stream_interrupt_is_verified():
    plan = _nvic_plan({"USART1": {"dma": "rx"}})
    block = next(b for b in plan["peripherals"] if b["name"] == "USART1")
    stream_irq = block["dma"]["streams"][0]["nvic"]["irqn"]
    resolver = dict_irq_resolver({"STM32L431": {stream_irq: 11}}, "STM32L431", "STM32L4")
    result = derive_acceptance_spec(plan, irq_resolver=resolver)

    nvic = next(c for c in result["spec"]["checks"] if c["id"] == f"nvic_{stream_irq}_enabled")
    assert nvic["address"] == "0xe000e100"   # IRQ 11 -> ISER[0]
    assert nvic["expect"] == "0x00000800"    # 1 << 11


def test_nvic_svd_irq_resolver_reads_interrupt_numbers():
    plan = _nvic_plan({"USART1": {"nvic": True}})
    resolver = svd_irq_resolver(_FakeIRQSVD({"USART1": 37}))
    result = derive_acceptance_spec(plan, irq_resolver=resolver)

    nvic = next(c for c in result["spec"]["checks"] if c["id"] == "nvic_USART1_IRQn_enabled")
    assert nvic["address"] == "0xe000e104"


def test_nvic_can_be_disabled():
    plan = _nvic_plan({"USART1": {"nvic": True}})
    resolver = dict_irq_resolver({"STM32L431": {"USART1_IRQn": 37}}, "STM32L431", "STM32L4")
    result = derive_acceptance_spec(plan, irq_resolver=resolver, options={"include_nvic": False})

    assert all(not c["id"].startswith("nvic_") for c in result["spec"]["checks"])


# --- GPIO MODER checks: masked equality, F1 excluded, base resolved -----------


def test_gpio_moder_check_masked_eq_for_af_pin():
    plan = _plan()   # PA9/PA10 af_pp, PB6 af_od -> all AF (0b10)
    resolver = dict_gpio_resolver({"STM32L431": {"A": 0x48000000, "B": 0x48000400}},
                                  "STM32L431", "STM32L4")
    result = derive_acceptance_spec(plan, gpio_resolver=resolver)

    pa9 = next(c for c in result["spec"]["checks"] if c["id"] == "gpio_PA9_mode")
    assert pa9["kind"] == "memory_u32"
    assert pa9["address"] == "0x48000000"
    assert pa9["mask"] == "0x000c0000"     # pin 9 -> shift 18, 0b11 << 18
    assert pa9["expect"] == "0x00080000"   # AF = 0b10 << 18
    assert pa9["op"] == "eq"
    assert result["stats"]["gpio_checks"] == 3


def test_gpio_moder_analog_role_is_0b11():
    board = {"mcu": {"part_normalized": "STM32L431CBT6", "family": "STM32L4", "line": "STM32L431", "pins": [
        _pin("PA0", "/ADC1_IN5", _fn("ADC1", "IN5")),
    ]}}
    plan = build_framework_plan(board)
    resolver = dict_gpio_resolver({"STM32L4": {"A": 0x48000000}}, "STM32L431", "STM32L4")
    result = derive_acceptance_spec(plan, gpio_resolver=resolver)

    pa0 = next(c for c in result["spec"]["checks"] if c["id"] == "gpio_PA0_mode")
    assert pa0["mask"] == "0x00000003"     # pin 0 -> shift 0
    assert pa0["expect"] == "0x00000003"   # analog = 0b11


def test_gpio_moder_skipped_on_f1_family():
    board = {"mcu": {"part_normalized": "STM32F103C8T6", "family": "STM32F1", "line": "STM32F103", "pins": [
        _pin("PA9", "/USART1_TX", _fn("USART1", "TX")),
    ]}}
    plan = build_framework_plan(board)
    resolver = dict_gpio_resolver({"STM32F103": {"A": 0x40010800}}, "STM32F103", "STM32F1")
    result = derive_acceptance_spec(plan, gpio_resolver=resolver)

    assert all(not c["id"].startswith("gpio_") for c in result["spec"]["checks"])
    assert any(u["type"] == "gpio_moder_unsupported" for u in result["unresolved"])
    assert result["stats"]["gpio_checks"] == 0


def test_gpio_base_unknown_is_surfaced_not_faked():
    plan = _plan()
    result = derive_acceptance_spec(plan, gpio_resolver=lambda port: None)

    assert all(not c["id"].startswith("gpio_") for c in result["spec"]["checks"])
    assert any(u["type"] == "gpio_base_unknown" for u in result["unresolved"])


def test_no_gpio_resolver_notes_but_no_unresolved_spam():
    plan = _plan()
    result = derive_acceptance_spec(plan, gpio_resolver=None)

    assert result["stats"]["gpio_checks"] == 0
    assert not any(u["type"] == "gpio_base_unknown" for u in result["unresolved"])
    assert any("gpio resolver" in n.lower() for n in result["notes"])


def test_gpio_svd_resolver_uses_moder_register_address():
    plan = _plan()
    svd = _FakeSVD({("GPIOA", "MODER"): {"address_int": 0x48000000, "fields": []},
                    ("GPIOB", "MODER"): {"address_int": 0x48000400, "fields": []}})
    resolver = svd_gpio_resolver(svd)
    result = derive_acceptance_spec(plan, gpio_resolver=resolver)

    pa9 = next(c for c in result["spec"]["checks"] if c["id"] == "gpio_PA9_mode")
    assert pa9["address"] == "0x48000000"


def test_gpio_can_be_disabled():
    plan = _plan()
    resolver = dict_gpio_resolver({"STM32L431": {"A": 0x48000000, "B": 0x48000400}},
                                  "STM32L431", "STM32L4")
    result = derive_acceptance_spec(plan, gpio_resolver=resolver, options={"include_gpio": False})

    assert all(not c["id"].startswith("gpio_") for c in result["spec"]["checks"])


def test_derived_gpio_check_passes_when_moder_matches():
    plan = _plan()
    resolver = dict_gpio_resolver({"STM32L431": {"A": 0x48000000, "B": 0x48000400}},
                                  "STM32L431", "STM32L4")
    spec = validate_acceptance_spec(derive_acceptance_spec(plan, gpio_resolver=resolver)["spec"])
    reader = _FakeReader(memory={0x48000000: (0b10 << 18), 0x48000400: (0b10 << 12)})

    report = evaluate_acceptance(spec, reader)
    pa9 = next(r for r in report["results"] if r["id"] == "gpio_PA9_mode")
    assert pa9["status"] == "pass"


def test_derived_gpio_check_fails_when_pin_left_as_input():
    plan = _plan()
    resolver = dict_gpio_resolver({"STM32L431": {"A": 0x48000000, "B": 0x48000400}},
                                  "STM32L431", "STM32L4")
    spec = validate_acceptance_spec(derive_acceptance_spec(plan, gpio_resolver=resolver)["spec"])
    reader = _FakeReader(memory={0x48000000: 0x0, 0x48000400: 0x0})  # MODER still reset (input)

    report = evaluate_acceptance(spec, reader)
    pa9 = next(r for r in report["results"] if r["id"] == "gpio_PA9_mode")
    assert pa9["status"] == "fail"
