import json
from types import SimpleNamespace

import pytest

from mcp_server.self_check import (
    dbgmcu_idcode_candidates,
    evaluate_self_check,
    read_dbgmcu_idcode,
)
from mcp_server.tools.session_tools import self_check

# STM32L431 (Cortex-M4 r0p1): CPUID 0x410FC241, DBGMCU IDCODE 0x10016435.
L431_CPUID = 0x410FC241
L431_IDCODE = 0x10016435

# STM32H750VB as read on hardware (issue #78): Cortex-M7, legacy base returns 0,
# real IDCODE at 0x5C001000 is DEV_ID 0x450.
H750_CPUID = 0x411FC271
H750_IDCODE = 0x20036450
H750_MEMORY = {
    0xE000ED00: H750_CPUID,
    0xE0042000: 0x00000000,
    0x5C001000: H750_IDCODE,
}

# Cortex-M0 / M0+ (architecture nibble 0xC, not 0xF) and Cortex-M33.
M0_CPUID = 0x410CC200
M0P_CPUID = 0x410CC601
M33_CPUID = 0x410FD214


def test_valid_l431_passes_all_checks():
    result = evaluate_self_check(L431_CPUID, L431_IDCODE, expected_family="STM32L431")

    assert result["ok"] is True
    assert result["core"] == "Cortex-M4"
    assert "L43" in result["device"]
    assert all(c["ok"] for c in result["checks"])


def test_byte_reversed_cpuid_is_flagged_by_byte_order_check():
    # 0x410FC241 with bytes reversed -> 0x41C20F41 (the exact bug we hit on HW).
    reversed_cpuid = int.from_bytes(L431_CPUID.to_bytes(4, "big"), "little")
    assert reversed_cpuid == 0x41C20F41

    result = evaluate_self_check(reversed_cpuid, L431_IDCODE)

    assert result["ok"] is False
    byte_order = next(c for c in result["checks"] if c["name"] == "byte_order")
    assert byte_order["ok"] is False


def test_stm32l151_dev_id_0x427_is_recognized():
    # issue #5: STM32L151CCUx (Cortex-M3) has dev_id 0x427; must not be flagged unknown.
    result = evaluate_self_check(0x410FC231, 0x10010427, expected_family="STM32L151CCUx")
    assert result["ok"] is True
    assert result["core"] == "Cortex-M3"
    assert "L151" in result["device"]
    dev = next(c for c in result["checks"] if c["name"] == "dbgmcu_dev_id")
    assert dev["ok"] is True


def test_stm32u535_dev_id_0x455_is_recognized():
    result = evaluate_self_check(0x410FD214, 0x10000455, expected_family="STM32U535")

    assert result["ok"] is True
    assert result["core"] == "Cortex-M33"
    assert "U535" in result["device"]
    dev = next(c for c in result["checks"] if c["name"] == "dbgmcu_dev_id")
    assert dev["ok"] is True


def test_stm32u535_zero_dbgmcu_idcode_is_advisory_when_cpuid_matches():
    result = evaluate_self_check(0x410FD214, 0x00000000, expected_family="STM32U535")

    assert result["ok"] is True
    assert result["core"] == "Cortex-M33"
    dev = next(c for c in result["checks"] if c["name"] == "dbgmcu_dev_id")
    assert dev["ok"] is True
    assert "unavailable" in dev["detail"]


def test_zero_dbgmcu_idcode_still_fails_for_non_u5_expected_family():
    result = evaluate_self_check(L431_CPUID, 0x00000000, expected_family="STM32L431")

    assert result["ok"] is False
    dev = next(c for c in result["checks"] if c["name"] == "dbgmcu_dev_id")
    assert dev["ok"] is False


def test_expected_family_mismatch_is_flagged():
    result = evaluate_self_check(L431_CPUID, L431_IDCODE, expected_family="STM32F407")

    dev = next(c for c in result["checks"] if c["name"] == "dbgmcu_dev_id")
    assert dev["ok"] is False
    assert result["ok"] is False


def test_unknown_core_partno_flagged():
    bogus = 0x410F0001  # implementer ok, constant nibble ok, but partno unknown
    result = evaluate_self_check(bogus, L431_IDCODE)

    core_check = next(c for c in result["checks"] if c["name"] == "cortex_m_core")
    assert core_check["ok"] is False


@pytest.mark.parametrize(
    ("family", "address"),
    [
        ("STM32H750", 0x5C001000),
        ("STM32H7", 0x5C001000),
        ("STM32H750VB", 0x5C001000),
        ("STM32H7A3", 0x5C001000),
        ("STM32F030", 0x40015800),
        ("STM32F0", 0x40015800),
        ("STM32G071", 0x40015800),
        ("STM32G0", 0x40015800),
        ("STM32L071", 0x40015800),
        ("STM32L0", 0x40015800),
        ("STM32C031", 0x40015800),
        ("STM32U073", 0x40015800),
        ("STM32L552", 0xE0044000),
        ("STM32L5", 0xE0044000),
        ("STM32U535", 0xE0044000),
        ("STM32U5", 0xE0044000),
        ("STM32U375", 0xE0044000),
        ("STM32WBA52", 0xE0044000),
        ("STM32H563", 0x44024000),
        ("STM32F407", 0xE0042000),
        ("STM32F4", 0xE0042000),
        ("STM32F103", 0xE0042000),
        ("STM32F7", 0xE0042000),
        ("STM32L431", 0xE0042000),
        ("STM32L151CCUx", 0xE0042000),
        ("STM32G431", 0xE0042000),
        ("STM32WB55", 0xE0042000),
        ("STM32WL55", 0xE0042000),
    ],
)
def test_family_selects_one_idcode_address(family, address):
    assert dbgmcu_idcode_candidates(family) == [address]


@pytest.mark.parametrize(
    ("cpuid", "address"),
    [
        (M0_CPUID, 0x40015800),
        (M0P_CPUID, 0x40015800),
        (M33_CPUID, 0xE0044000),
        (L431_CPUID, 0xE0042000),  # Cortex-M4
        (0x410FC231, 0xE0042000),  # Cortex-M3
    ],
)
def test_cpuid_selects_idcode_address_when_family_is_absent(cpuid, address):
    assert dbgmcu_idcode_candidates(None, cpuid) == [address]


def test_named_family_wins_over_an_ambiguous_cortex_m7_cpuid():
    # F7 and H7 are both Cortex-M7. The family, not the part number, decides.
    assert dbgmcu_idcode_candidates("STM32F746", H750_CPUID) == [0xE0042000]
    assert dbgmcu_idcode_candidates("STM32H750", H750_CPUID) == [0x5C001000]
    # H5 is Cortex-M33, same part number as U5, but its DBGMCU is not at 0xE0044000.
    assert dbgmcu_idcode_candidates("STM32H563", M33_CPUID) == [0x44024000]
    assert dbgmcu_idcode_candidates(None, M33_CPUID) == [0xE0044000]


def test_cortex_m7_without_a_family_tries_legacy_then_h7():
    assert dbgmcu_idcode_candidates(None, H750_CPUID) == [0xE0042000, 0x5C001000]


def test_unknown_family_falls_back_to_cpuid_instead_of_the_legacy_base():
    # "STM32" alone matches no series. An M0+ still has to use the low APB base.
    assert dbgmcu_idcode_candidates("STM32", M0P_CPUID) == [0x40015800]


def test_h750_legacy_read_fails_and_h7_address_passes():
    """Same memory image as the STM32H750VB repro in issue #78.

    The old self_check always evaluated the word at 0xE0042000. That word is 0,
    so expected_family STM32H750 fails. Reading 0x5C001000 instead yields
    DEV_ID 0x450 and the check passes. No target was attached for this test.
    """
    old = evaluate_self_check(H750_CPUID, H750_MEMORY[0xE0042000], expected_family="STM32H750")
    assert old["ok"] is False
    assert old["device"] is None
    assert old["core"] == "Cortex-M7"
    dev = next(c for c in old["checks"] if c["name"] == "dbgmcu_dev_id")
    assert dev["ok"] is False
    assert "0x000" in dev["detail"]

    seen = []

    def read_word(address):
        seen.append(address)
        if address not in H750_MEMORY:
            raise AssertionError(f"unexpected read at {address:#x}")
        return H750_MEMORY[address]

    value, address = read_dbgmcu_idcode(read_word, expected_family="STM32H750", cpuid=H750_CPUID)
    assert seen == [0x5C001000]
    assert address == 0x5C001000
    assert value == H750_IDCODE

    new = evaluate_self_check(H750_CPUID, value, expected_family="STM32H750", idcode_address=address)
    assert new["ok"] is True
    assert new["device"] == "STM32H74x/75x"
    assert new["core"] == "Cortex-M7"
    assert new["dbgmcu_idcode"] == "0x20036450"
    assert new["dbgmcu_idcode_address"] == "0x5c001000"

    value, address = read_dbgmcu_idcode(read_word, expected_family="STM32H7", cpuid=H750_CPUID)
    assert address == 0x5C001000
    assert evaluate_self_check(H750_CPUID, value, expected_family="STM32H7")["ok"] is True


def test_cortex_m7_probe_keeps_f7_on_the_legacy_address():
    memory = {0xE0042000: 0x10000449, 0x5C001000: 0xDEADDEAD}
    seen = []

    def read_word(address):
        seen.append(address)
        return memory[address]

    value, address = read_dbgmcu_idcode(read_word, cpuid=H750_CPUID)
    assert seen == [0xE0042000]
    assert address == 0xE0042000
    assert value == 0x10000449
    assert evaluate_self_check(H750_CPUID, value, expected_family="STM32F7")["device"] == "STM32F74x/75x"


def test_cortex_m7_probe_keeps_the_legacy_read_when_the_h7_address_faults():
    def read_word(address):
        if address == 0x5C001000:
            raise RuntimeError("bus fault")
        return 0

    value, address = read_dbgmcu_idcode(read_word, cpuid=H750_CPUID)
    assert (value, address) == (0, 0xE0042000)


def test_cortex_m7_probe_uses_h7_when_the_legacy_read_faults():
    def read_word(address):
        if address == 0xE0042000:
            raise RuntimeError("bus fault")
        return H750_IDCODE

    value, address = read_dbgmcu_idcode(read_word, cpuid=H750_CPUID)
    assert (value, address) == (H750_IDCODE, 0x5C001000)


def test_cortex_m7_probe_uses_h7_when_the_legacy_base_is_zero():
    seen = []

    def read_word(address):
        seen.append(address)
        return H750_MEMORY[address]

    value, address = read_dbgmcu_idcode(read_word, cpuid=H750_CPUID)
    assert seen == [0xE0042000, 0x5C001000]
    assert (value, address) == (H750_IDCODE, 0x5C001000)


def test_cortex_m0_architecture_nibble_passes_byte_order():
    # F0/G0/L0 are ARMv6-M: CPUID bits[19:16] are 0xC, not the 0xF of M3/M4/M7.
    result = evaluate_self_check(M0_CPUID, 0x00000444, expected_family="STM32F030")
    byte_order = next(c for c in result["checks"] if c["name"] == "byte_order")
    assert byte_order["ok"] is True
    assert result["core"] == "Cortex-M0"
    assert result["ok"] is True
    assert result["device"] == "STM32F03x"


def test_byte_reversed_m0_cpuid_still_fails_byte_order():
    reversed_cpuid = int.from_bytes(M0_CPUID.to_bytes(4, "big"), "little")
    result = evaluate_self_check(reversed_cpuid, 0x00000444, expected_family="STM32F030")
    byte_order = next(c for c in result["checks"] if c["name"] == "byte_order")
    assert byte_order["ok"] is False
    assert result["ok"] is False


def _run_self_check(memory, *, profile_mcu=None, arguments=None):
    seen = []

    class Client:
        def halt_execution(self):
            return None

        def read_word(self, address):
            seen.append(address)
            if address not in memory:
                raise KeyError(hex(address))
            return memory[address]

    ctx = SimpleNamespace(
        gdb_client=Client(),
        debug_profile=SimpleNamespace(get=lambda: {"mcu": profile_mcu} if profile_mcu else {}),
    )
    payload = json.loads(self_check(ctx, arguments or {})[0].text)
    return payload, seen


def test_self_check_tool_reads_h7_idcode_from_profile_mcu():
    payload, seen = _run_self_check(H750_MEMORY, profile_mcu="STM32H750")

    assert seen == [0xE000ED00, 0x5C001000]
    assert payload["ok"] is True
    data = payload["data"]
    assert data["ok"] is True
    assert data["device"] == "STM32H74x/75x"
    assert data["core"] == "Cortex-M7"
    assert data["dbgmcu_idcode"] == "0x20036450"
    assert data["dbgmcu_idcode_address"] == "0x5c001000"


def test_self_check_expected_family_overrides_profile_mcu():
    # Profile says F4 (legacy base) but the call names the H7 on the wire.
    payload, seen = _run_self_check(
        H750_MEMORY,
        profile_mcu="STM32F407",
        arguments={"expected_family": "STM32H750", "halt": False},
    )
    assert seen == [0xE000ED00, 0x5C001000]
    assert payload["data"]["device"] == "STM32H74x/75x"
    assert payload["data"]["ok"] is True


@pytest.mark.parametrize(
    ("family", "cpuid", "idcode_address", "idcode", "device"),
    [
        ("STM32F030", M0_CPUID, 0x40015800, 0x00000444, "STM32F03x"),
        ("STM32G071", M0P_CPUID, 0x40015800, 0x10006460, "STM32G07x/08x"),
        ("STM32L071", M0P_CPUID, 0x40015800, 0x10006447, "STM32L07x/08x"),
        ("STM32L552", M33_CPUID, 0xE0044000, 0x10000472, "STM32L552/562"),
        ("STM32U535", M33_CPUID, 0xE0044000, 0x10000455, "STM32U535/U545"),
        ("STM32F407", L431_CPUID, 0xE0042000, 0x10076413, "STM32F40x/41x"),
        ("STM32WB55", L431_CPUID, 0xE0042000, 0x20016495, "STM32WB55"),
    ],
)
def test_self_check_tool_reads_the_family_base(family, cpuid, idcode_address, idcode, device):
    memory = {0xE000ED00: cpuid, idcode_address: idcode}
    payload, seen = _run_self_check(memory, arguments={"expected_family": family, "halt": False})

    assert seen == [0xE000ED00, idcode_address]
    assert payload["data"]["ok"] is True
    assert payload["data"]["device"] == device
    assert payload["data"]["dbgmcu_idcode_address"] == f"0x{idcode_address:08x}"
