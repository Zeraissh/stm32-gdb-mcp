"""Unit tests for the deterministic timer base-frequency solver (Pillar D Tier 3)."""

from mcp_server.timer_solver import (
    resolve_timer_bus,
    solve_timer_dividers,
    solve_timers_in_plan,
    timer_arr_bits,
    timer_input_clock,
)

# --- Pure PSC/ARR math ------------------------------------------------------


def test_exact_solution_prefers_max_resolution():
    # 80 MHz / 1 kHz = 80000 = 2 * 40000 -> smallest PSC, largest ARR.
    sol = solve_timer_dividers(80_000_000, 1000)
    assert sol["feasible"] and sol["exact"]
    assert sol["psc"] == 1 and sol["arr"] == 39999
    assert sol["achieved_hz"] == 1000
    assert sol["error_ppm"] == 0.0
    assert (sol["psc"] + 1) * (sol["arr"] + 1) == 80_000


def test_exact_solution_168mhz():
    sol = solve_timer_dividers(168_000_000, 1000)
    assert sol["feasible"] and sol["exact"]
    assert (sol["psc"] + 1) * (sol["arr"] + 1) == 168_000
    assert sol["achieved_hz"] == 1000


def test_closest_solution_reports_ppm_error():
    # 80 MHz / 7000 Hz is not integral -> closest pair + non-zero ppm.
    sol = solve_timer_dividers(80_000_000, 7000)
    assert sol["feasible"] and not sol["exact"]
    assert sol["psc"] is not None and sol["arr"] is not None
    assert abs(sol["error_ppm"]) > 0
    assert abs(sol["achieved_hz"] - 7000) / 7000 < 0.001  # within 0.1%


def test_target_faster_than_clock_is_infeasible():
    sol = solve_timer_dividers(1_000_000, 2_000_000)
    assert not sol["feasible"]
    assert sol["unresolved"][0]["type"] == "target_too_fast"


def test_target_too_slow_for_16bit_suggests_32bit():
    sol = solve_timer_dividers(80_000_000, 0.01)  # needs 8e9 > 65536*65536
    assert not sol["feasible"]
    assert sol["unresolved"][0]["type"] == "target_too_slow"
    assert "32-bit" in sol["unresolved"][0]["suggestion"]


def test_32bit_arr_makes_slow_target_feasible():
    sol = solve_timer_dividers(80_000_000, 0.01, arr_max=(1 << 32) - 1)
    assert sol["feasible"]
    assert sol["arr_bits"] == 32


def test_rejects_bad_inputs():
    assert not solve_timer_dividers(0, 1000)["feasible"]
    assert not solve_timer_dividers(80_000_000, 0)["feasible"]
    assert not solve_timer_dividers(80_000_000, True)["feasible"]


# --- Device data ------------------------------------------------------------


def test_resolve_timer_bus_f4():
    assert resolve_timer_bus("STM32F407", "STM32F4", "TIM1") == "apb2"
    assert resolve_timer_bus("STM32F407", "STM32F4", "TIM3") == "apb1"


def test_resolve_timer_bus_unknown_family_is_none():
    assert resolve_timer_bus("STM32ZZ9", "STM32ZZ", "TIM3") is None


def test_timer_arr_bits():
    assert timer_arr_bits("STM32F407", "STM32F4", "TIM2") == 32
    assert timer_arr_bits("STM32F407", "STM32F4", "TIM5") == 32
    assert timer_arr_bits("STM32F407", "STM32F4", "TIM3") == 16
    assert timer_arr_bits("STM32ZZ9", "STM32ZZ", "TIM2") == 16  # unknown -> conservative


def test_timer_input_clock_x2_rule():
    sol = {"pclk1_hz": 42_000_000, "apb1_presc": 4, "pclk2_hz": 84_000_000, "apb2_presc": 2}
    assert timer_input_clock(sol, "apb1") == 84_000_000  # x2 when presc != 1
    assert timer_input_clock(sol, "apb2") == 168_000_000
    assert timer_input_clock({"pclk1_hz": 80_000_000, "apb1_presc": 1}, "apb1") == 80_000_000
    assert timer_input_clock(None, "apb1") is None


# --- Plan integration -------------------------------------------------------


def _timer_plan(target=1000, clock=None, family="STM32L4", line="STM32L431"):
    block = {
        "name": "TIM3", "kind": "timer", "pins": [],
        "config_fields": [
            {"field": "CounterMode", "value": "TIM_COUNTERMODE_UP", "rendered": "TIM_COUNTERMODE_UP",
             "source": "default", "source_key": None, "mapped": True, "note": None},
        ],
        "param_todos": [
            {"field": "Prescaler", "hint": "target 1000 Hz recorded; run solve_clock_tree then solve_timer"},
            {"field": "Period", "hint": "target 1000 Hz recorded; run solve_clock_tree then solve_timer"},
        ],
        "config_sources": {"explicit": 0, "derived": 0, "default": 1},
        "has_config": True,
        "timer_target_hz": target,
    }
    plan = {
        "mcu": {"line": line, "family": family},
        "peripherals": [block],
        "unresolved": [
            {"type": "param_unresolved", "peripheral": "TIM3", "field": "Prescaler"},
            {"type": "param_unresolved", "peripheral": "TIM3", "field": "Period"},
        ],
        "stats": {"unresolved_count": 2},
    }
    if clock is not None:
        plan["clock_config"] = clock
    return plan, block


def test_solve_timers_in_plan_injects_derived_fields():
    clock = {"family": "STM32L4", "pclk1_hz": 80_000_000, "apb1_presc": 1,
             "pclk2_hz": 80_000_000, "apb2_presc": 1}
    plan, block = _timer_plan(clock=clock)
    report = solve_timers_in_plan(plan)

    assert report["solved_count"] == 1
    fields = {f["field"]: f for f in block["config_fields"]}
    assert fields["Prescaler"]["source"] == "derived"
    assert fields["Prescaler"]["value"] == 1
    assert fields["Period"]["value"] == 39999
    # Prescaler must render before Period in canonical HAL order.
    names = [f["field"] for f in block["config_fields"]]
    assert names.index("Prescaler") < names.index("Period")
    assert block["param_todos"] == []
    assert plan["unresolved"] == []
    assert plan["stats"]["unresolved_count"] == 0


def test_solve_timers_in_plan_without_clock_is_honest():
    plan, block = _timer_plan(clock=None)
    report = solve_timers_in_plan(plan)
    assert report["solved_count"] == 0
    assert report["results"][0]["unresolved"][0]["type"] == "no_clock_solution"
    # Block left untouched -- Prescaler/Period still TODO.
    assert {t["field"] for t in block["param_todos"]} == {"Prescaler", "Period"}


def test_solve_timers_in_plan_unknown_bus_is_honest():
    clock = {"pclk1_hz": 80_000_000, "apb1_presc": 1}
    plan, _ = _timer_plan(clock=clock, family="STM32ZZ", line="STM32ZZ9")
    report = solve_timers_in_plan(plan)
    assert report["results"][0]["unresolved"][0]["type"] == "bus_unknown"
    # But an explicit bus override rescues it.
    plan2, _ = _timer_plan(clock=clock, family="STM32ZZ", line="STM32ZZ9")
    report2 = solve_timers_in_plan(plan2, bus_override="apb1")
    assert report2["solved_count"] == 1


def test_solve_timers_in_plan_explicit_clock_bypasses_tree():
    plan, block = _timer_plan(clock=None)
    report = solve_timers_in_plan(plan, timer_clock_hz=84_000_000)
    assert report["solved_count"] == 1
    fields = {f["field"]: f for f in block["config_fields"]}
    assert (fields["Prescaler"]["value"] + 1) * (fields["Period"]["value"] + 1) == 84_000
