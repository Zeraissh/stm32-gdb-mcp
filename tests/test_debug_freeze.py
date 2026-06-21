import pytest

from mcp_server.debug_freeze import resolve_freeze_targets, supported_families


def test_f4_iwdg_and_wwdg_resolve_to_apb1_freeze_bits():
    targets = resolve_freeze_targets("stm32f4", ["iwdg", "wwdg"])

    by_name = {t["peripheral"]: t for t in targets}
    assert by_name["iwdg"]["address"] == 0xE0042008
    assert by_name["iwdg"]["bit"] == 12
    assert by_name["iwdg"]["mask"] == (1 << 12)
    assert by_name["wwdg"]["bit"] == 11


def test_l4_uses_its_own_register_layout():
    targets = resolve_freeze_targets("stm32l4", ["iwdg"])

    assert targets[0]["address"] == 0xE0042008  # APB1FZR1
    assert targets[0]["bit"] == 12


def test_unknown_family_raises():
    with pytest.raises(ValueError, match="Unsupported"):
        resolve_freeze_targets("stm32zz", ["iwdg"])


def test_unknown_peripheral_raises():
    with pytest.raises(ValueError, match="Unknown peripheral"):
        resolve_freeze_targets("stm32f4", ["not_a_peripheral"])


def test_supported_families_lists_known_families():
    families = supported_families()
    assert "stm32f4" in families
    assert "stm32l4" in families
