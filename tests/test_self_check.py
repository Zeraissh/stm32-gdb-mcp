from mcp_server.self_check import evaluate_self_check

# STM32L431 (Cortex-M4 r0p1): CPUID 0x410FC241, DBGMCU IDCODE 0x10016435.
L431_CPUID = 0x410FC241
L431_IDCODE = 0x10016435


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
