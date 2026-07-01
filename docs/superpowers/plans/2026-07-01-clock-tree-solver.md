# Clock-tree solver (Pillar D Tier 3 — SystemClock_Config) / 时钟树求解器

## Why / 为什么

`render_framework` today emits `SystemClock_Config()` as an honest **TODO stub** —
the single most conspicuous hand-written gap in the generated init code. Every real
board boots with a clock config, and computing PLL dividers + bus prescalers + flash
latency by hand is tedious and error-prone. This is pure, bounded math over
datasheet constraints — exactly the deterministic, never-hallucinate work the machine
layer should own.

## Contract / 契约

Given a **device profile** (datasheet constraints) + a **request** (source + target
SYSCLK), compute the exact PLL/bus configuration, or honestly report infeasible.
Nothing is guessed: an unknown device (no built-in profile, no explicit `profile`) or
an HSE with no crystal frequency is surfaced as `unresolved`, never fabricated.

```
vco_in  = f_in / M                 # within [vco_in_min, vco_in_max]
vco_out = vco_in * N               # within [vco_out_min, vco_out_max]
sysclk  = vco_out / D              # D = PLLP (F4) or PLLR (L4), in {2,4,6,8}
clk48   = vco_out / Q              # optional, exact 48 MHz for USB
hclk    = sysclk / AHB_presc       # <= max_hclk
pclk1   = hclk / APB1_presc        # <= max_pclk1
pclk2   = hclk / APB2_presc        # <= max_pclk2
flash_ws = latency(hclk, voltage)  # from the profile's table
```

Deterministic tie-break: prefer `vco_in` closest to the profile's `ideal_vco_in_hz`
(2 MHz for F4, 16 MHz for L4), then smaller M/N; bus prescalers minimised (maximise
peripheral clocks) like CubeMX. Result is electrically equivalent to vendor output.

## Tier 1 — pure solver + renderer (no hardware)

- `clock_solver.py`:
  - `solve_clock_tree(profile, request)` -> `{feasible, solution, unresolved, notes, stats}`.
  - `resolve_profile(line, family)` -> built-in profile | None (F407/F405, F401,
    F411, mainstream L4 <= 80 MHz). Honest `None` for unknown devices.
  - `render_system_clock_config(solution)` -> pure-ASCII HAL `SystemClock_Config()`
    (HSE/HSI + PLL + bus dividers + `FLASH_LATENCY_n`), family-correct macros.
  - `summarize_clock_solution(result)` for tool responses.
- Tests: `tests/test_clock_solver.py` — golden anchors (F407 HSE 8 MHz -> 168 MHz with
  exact 48 MHz USB; L431 HSI16 -> 80 MHz = M1/N10/R2), invariants, infeasible cases.

## Tier 2 — wire into render + MCP tool

- `framework_render._render_system_clock(plan)` emits the solved config when
  `plan["clock_config"]` is present, else keeps the honest TODO stub.
- `solve_clock_tree` MCP tool: require the session FrameworkPlan (`no_design` else),
  resolve the profile (explicit `profile` arg > built-in by the plan's line/family,
  else `unresolved`), solve, and store the solution as `plan["clock_config"]` so the
  next `render_framework` emits real clock code.
- Server tests appended to `tests/test_server_tools.py`.

## Not in scope / 暂不包含

- Overdrive / VOS-boost lines (F42x/F43x 180 MHz, L4+ 120 MHz) — different topology.
- Multiple independent PLLs (PLLSAI/PLLI2S) and peripheral kernel-clock muxing.
- MSI as a PLL source (L4) — HSI/HSE only for now.

## Status / 状态
- [x] Tier 1 — `clock_solver.py` + tests. (14 tests: golden anchors F407->168/L431->80, invariants, infeasible/honest-failure cases, F4/L4 rendering)
- [x] Tier 2 — render wiring + `solve_clock_tree` tool + server tests. (3 render + 6 server tests; suite 382 passed / 1 skipped)
