# Timer base-frequency solver (Pillar D Tier 3)

## Why / 目标

The framework synthesizer fills every mandatory HAL `.Init` field except the two
that are genuine design decisions on a timer: `Prescaler` (PSC) and `Period`
(ARR). A senior engineer thinks in terms of "I want TIM3 to update at 1 kHz", not
"PSC=799, ARR=99". This solver turns that intent into concrete PSC/ARR values by
pure integer datasheet math, using the timer's input clock (TIMxCLK) derived from
the already-solved clock tree — eliminating the last per-peripheral TODO for
timers.

## Core principle / 核心原则

Deterministic and honest, like the rest of the machine layer. The timer input
clock and the PSC/ARR math are exact. A target that cannot be hit exactly yields
the closest achievable PSC/ARR **plus the achieved frequency and ppm error** — it
never silently pretends to be exact. A target that cannot be represented at all
(faster than TIMxCLK, or slower than the 16-/32-bit dividers allow), or a timer
whose APB bus is unknown for the device, is surfaced as `unresolved`, never
guessed.

## STM32 timer clocking rule

`TIMxCLK = PCLKx` when the APBx prescaler is 1, else `PCLKx * 2`. TIM1/8/9/10/11
(F4) and TIM1/8/15/16/17 (L4) live on APB2; the rest on APB1. TIM2 and TIM5 are
32-bit (ARR up to 2^32-1); the others are 16-bit.

Counter update frequency: `f = TIMxCLK / ((PSC+1) * (ARR+1))`.

## Tier 1 — pure solver (`timer_solver.py`)

- `solve_timer_dividers(timer_clock_hz, target_hz, psc_max=65535, arr_max=65535)`
  -> `{feasible, psc, arr, prescaler, period, timer_clock_hz, target_hz,
  achieved_hz, error_hz, error_ppm, exact, arr_bits, unresolved, notes}`.
  - **Exact pass**: when `timer_clock_hz % target_hz == 0`, factor
    `N = timer_clock_hz / target_hz` into `(PSC+1)*(ARR+1)` with both in range,
    choosing the **smallest PSC** (largest ARR) for maximum counter resolution.
  - **Closest pass**: otherwise pick the minimal PSC that fits ARR in range, then
    the nearest ARR; report `exact=False` + `error_ppm`.
  - **Infeasible**: `target > timer_clock` (too fast) or
    `N > (psc_max+1)*(arr_max+1)` (too slow for the divider width) -> `feasible=False`
    with a typed `unresolved` (suggest a 32-bit timer / `arr_bits=32` for the slow case).
- `timer_input_clock(clock_solution, bus)` -> TIMxCLK via the x1/x2 rule.
- `resolve_timer_bus(line, family, timer)` -> `"apb1"|"apb2"|None` (built-in F4/L4
  APB2 sets; `None` for unknown families — honest, never guessed).
- `timer_arr_bits(line, family, timer)` -> `32` for TIM2/TIM5 on F4/L4, else `16`.
- `solve_timers_in_plan(plan, ...)` -> resolve every TIM block carrying a recorded
  target against `plan["clock_config"]`, inject PSC/ARR as `derived` config fields,
  clear their `param_unresolved`, and return a per-timer report. Honest reasons
  (`no_clock_solution`, `bus_unknown`, `infeasible`) when a timer cannot be solved.

## Tier 2 — intent capture + `solve_timer` tool

- `design_framework(design={"TIM3": {"update_hz": 1000}})` records the target on
  the TIM block (`timer_target_hz`); `Prescaler`/`Period` stay honest TODOs whose
  hint notes the recorded target until solved. Explicit `prescaler`/`period` still
  win.
- `solve_timer(timer?, target_hz?, timer_clock_hz?, bus?, arr_bits?, load?, session?)`
  resolves TIMxCLK (explicit `timer_clock_hz` > `plan.clock_config` + bus), solves
  PSC/ARR, injects them, and (by default) persists into the session plan so the next
  `render_framework` emits concrete values with an achieved-frequency comment.

## Status / 状态

- [x] Tier 1 — `timer_solver.py` + unit tests.
- [x] Tier 2 — intent capture in `build_framework_plan` + `solve_timer` tool + tests.
- [x] Docs + CHANGELOG.
