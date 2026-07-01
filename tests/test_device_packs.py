"""Tests for the device-pack registry (Pillar F): data-driven per-family facts."""

import json

import pytest

from mcp_server import (
    clock_solver,
    device_packs,
    dma_solver,
    interrupt_solver,
    timer_solver,
)


@pytest.fixture(autouse=True)
def _reset_external():
    """Keep every test hermetic: no external pack leaks across tests."""
    device_packs.reset_external()
    yield
    device_packs.reset_external()


def _synthetic_pack(family="STM32ZZ"):
    """A minimal but valid pack covering all four sections.

    DMA triples are JSON lists (not tuples) on purpose, to exercise _normalize.
    """
    return {
        "schema": device_packs.SCHEMA,
        "family": family,
        "clock": {"profiles": [
            {"match_lines": [family], "profile": {"family": family, "max_sysclk_hz": 80_000_000}},
        ]},
        "dma": {
            "arch": {"unit": "Stream", "select_field": "Channel", "select_prefix": "DMA_CHANNEL_"},
            "map": {"SPI1": {"rx": [2, 0, 3], "tx": [2, 3, 3]}},
        },
        "nvic": {"i2c_dual": True, "irq": {"TIM2": ["TIM2_IRQn"]}},
        "timer": {"apb2": ["TIM1"], "bits32": ["TIM2"]},
    }


# --- built-ins ---------------------------------------------------------------


def test_builtin_packs_validate_clean():
    assert device_packs.validate_pack(device_packs.get_pack("STM32F4")) == []
    assert device_packs.validate_pack(device_packs.get_pack("STM32L4")) == []


def test_builtin_dma_accessors():
    assert device_packs.dma_arch("STM32F4")["unit"] == "Stream"
    assert device_packs.dma_arch("STM32L4")["unit"] == "Channel"
    assert device_packs.dma_map("STM32F4")["SPI1"]["rx"] == (2, 0, 3)
    assert {"STM32F4", "STM32L4"}.issubset(set(device_packs.dma_families()))
    # An unmodelled family carries no DMA facts.
    assert device_packs.dma_arch("STM32G0") is None
    assert device_packs.dma_map("STM32G0") == {}


def test_builtin_nvic_accessors():
    assert device_packs.nvic_table("STM32F4")["TIM2"] == ["TIM2_IRQn"]
    # Explicit pack flag.
    assert device_packs.i2c_dual("STM32F4") is True
    # Seeded naming rule (no full pack, still known dual EV/ER).
    assert device_packs.i2c_dual("STM32F1") is True
    # Neither pack nor seed -> honest False.
    assert device_packs.i2c_dual("STM32G0") is False


def test_builtin_timer_accessors():
    assert "TIM9" in device_packs.timer_apb2("STM32F4")
    assert device_packs.timer_bits32("STM32F4") == {"TIM2", "TIM5"}
    # Unmodelled family: None (honest), not an empty set.
    assert device_packs.timer_apb2("STM32G0") is None


def test_builtin_clock_resolution_via_solver():
    assert clock_solver.resolve_profile("STM32F407xx", "STM32F4")["family"] == "STM32F4"
    # Mainstream L4 falls back to the L4 profile.
    assert clock_solver.resolve_profile("STM32L431xx", "STM32L4")["family"] == "STM32L4"
    # L4+ (R/S/P/Q) is a known-unmodelled exclusion -> honest None.
    assert clock_solver.resolve_profile("STM32L4R5xx", "STM32L4") is None


# --- validation --------------------------------------------------------------


@pytest.mark.parametrize("mutate, needle", [
    (lambda p: "not-a-dict", "JSON object"),
    (lambda p: {**p, "schema": "wrong"}, "schema"),
    (lambda p: {**p, "family": "NRF52"}, "family"),
    (lambda p: {"schema": device_packs.SCHEMA, "family": "STM32ZZ"}, "no clock/dma/nvic/timer"),
    (lambda p: {**p, "dma": {**p["dma"], "map": {"SPI1": {"rx": [2, 0]}}}}, "controller, unit, selector"),
    (lambda p: {**p, "nvic": {"irq": {"TIM2": []}}}, "non-empty list"),
    (lambda p: {**p, "timer": {"apb2": [1, 2]}}, "list of timer-name strings"),
    (lambda p: {**p, "clock": {"profiles": []}}, "non-empty list"),
    (lambda p: {**p, "clock": {"profiles": [{"profile": {}}]}}, "selector"),
])
def test_validate_rejects_bad_packs(mutate, needle):
    problems = device_packs.validate_pack(mutate(_synthetic_pack()))
    assert any(needle in p for p in problems), problems


# --- registry lifecycle ------------------------------------------------------


def test_register_and_reset():
    assert "STM32ZZ" not in device_packs.coverage()["families"]
    assert device_packs.register_pack(_synthetic_pack()) == []
    cov = device_packs.coverage()
    assert "STM32ZZ" in cov["families"]
    assert "STM32ZZ" in cov["external"]
    assert "STM32ZZ" in device_packs.dma_families()
    device_packs.reset_external()
    assert "STM32ZZ" not in device_packs.coverage()["families"]


def test_register_refuses_builtin_shadow_without_override():
    problems = device_packs.register_pack(_synthetic_pack("STM32F4"))
    assert any("built-in" in p for p in problems)
    # Built-in untouched.
    assert device_packs.dma_map("STM32F4")["SPI1"]["rx"] == (2, 0, 3)


def test_register_allows_builtin_shadow_with_override():
    assert device_packs.register_pack(_synthetic_pack("STM32F4"), allow_override=True) == []
    # External now shadows the built-in (synthetic pack maps only SPI1).
    assert set(device_packs.dma_map("STM32F4")) == {"SPI1"}


def test_normalize_coerces_json_lists_to_tuples():
    device_packs.register_pack(_synthetic_pack())
    triple = device_packs.get_pack("STM32ZZ")["dma"]["map"]["SPI1"]["rx"]
    assert triple == (2, 0, 3)
    assert isinstance(triple, tuple)


# --- integration: a registered pack drives the deterministic solvers ---------


def test_registered_pack_drives_all_solvers():
    device_packs.register_pack(_synthetic_pack())

    dma = dma_solver.build_dma("SPI1", "spi", "STM32ZZ", dma=True)
    assert dma["resolved"] is True
    assert dma["streams"][0]["controller"] == "DMA2"

    vectors = interrupt_solver.resolve_vectors("TIM2", "timer", "STM32ZZ")
    assert [v["irqn"] for v in vectors] == ["TIM2_IRQn"]

    assert timer_solver.resolve_timer_bus(None, "STM32ZZ", "TIM1") == "apb2"
    assert timer_solver.resolve_timer_bus(None, "STM32ZZ", "TIM3") == "apb1"
    assert timer_solver.timer_arr_bits(None, "STM32ZZ", "TIM2") == 32

    assert clock_solver.resolve_profile("STM32ZZ9", "STM32ZZ")["family"] == "STM32ZZ"


def test_unregistered_family_is_honestly_unresolved():
    dma = dma_solver.build_dma("SPI1", "spi", "STM32ZZ", dma=True)
    assert dma["resolved"] is False
    assert "load a device pack" in dma["unresolved_reason"]


# --- JSON round-trip via load_pack -------------------------------------------


def test_load_pack_round_trip(tmp_path):
    path = tmp_path / "f4.json"
    path.write_text(json.dumps(_synthetic_pack("STM32YY")), encoding="utf-8")
    pack, problems = device_packs.load_pack(str(path))
    assert problems == []
    assert pack["family"] == "STM32YY"
    assert device_packs.register_pack(pack) == []
    assert "STM32YY" in device_packs.dma_families()


def test_load_pack_missing_file_reports_problem():
    pack, problems = device_packs.load_pack("does-not-exist.json")
    assert pack is None
    assert any("not found" in p for p in problems)
