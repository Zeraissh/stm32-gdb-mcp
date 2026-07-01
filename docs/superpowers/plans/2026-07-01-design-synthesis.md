# Design synthesis — framework/init-code solver (Pillar D)

## Why / 目标

Close the one remaining hand-written link in the spec-to-silicon pipeline. Pillars
A→B1→C already turn a netlist into a machine-readable board model, turn a product
spec into a machine-checked judge, and run a bounded build→flash→verify loop. But
the **framework design + code writing** step (网表图 + 产品规格 → 框架设计 + 代码编写)
still relies on the agent free-handing peripheral init code.

Pillar D makes that step **deterministic**: given the BoardDescription (Pillar A)
and an optional per-peripheral design config, derive a `FrameworkPlan` — exactly
which clocks to enable, how each pin must be muxed, and which peripheral init
blocks to emit, in dependency order — then render it to a HAL C init skeleton the
agent flashes and the acceptance loop (Pillar C) verifies.

## Core principle / 核心原则

Same as every other pillar: the machine never hallucinates. Everything derivable
from the board model alone is 100% deterministic. Values that need target-specific
data (a GPIO alternate-function number) or a human design decision (a baud rate)
are **never invented** — they are surfaced in `unresolved` and become clearly
marked `/* TODO */` holes in the rendered code. A senior engineer gets correct
scaffolding, not a plausible-looking guess.

## FrameworkPlan schema

```
{
  "mcu": {"part_normalized", "family", "line"},
  "clocks": [
     {"kind": "gpio_port", "port": "A", "hal_macro": "__HAL_RCC_GPIOA_CLK_ENABLE"},
     {"kind": "peripheral", "peripheral": "USART1", "hal_macro": "__HAL_RCC_USART1_CLK_ENABLE"}
  ],
  "gpio": [
     {"port_pin", "port", "pin", "peripheral", "signal", "role",
      "hal_mode", "pull", "speed", "af", "hal_alternate", "net"}
  ],
  "peripherals": [
     {"name", "kind", "instance", "handle", "hal_type", "init_fn", "hal_init_call",
      "clock_macro", "pins": [...], "config": {...}, "config_fields": [...], "has_config"}
  ],
  "init_order": ["SystemClock_Config", "MX_GPIO_Init", "MX_USART1_UART_Init", ...],
  "unresolved": [{"type", ...}],   # af_unknown | no_config | port_pin_unknown | unknown_role
  "warnings": [...],
  "stats": {...}
}
```

## Tier 1 — pure solver + renderer (unit-tested, no hardware)

- `framework_solver.py`: `build_framework_plan(board, design=None, af_map=None)` +
  helpers (`classify_peripheral`, `parse_port_pin`, GPIO role map, HAL clock-macro
  derivation, per-kind handle/init metadata, design-param field maps) +
  `summarize_framework` / `framework_view`.
- `framework_render.py`: `render_framework(plan, style="hal")` → `bsp_init.c` +
  `bsp_init.h`, honest TODOs for every `unresolved` item.
- Tests: `tests/test_framework_solver.py`, `tests/test_framework_render.py`.

## Tier 2 — MCP tools + per-session state

- `design_framework(design?, af_map?, session?)` — build + persist the plan from the
  session's imported board; returns `summarize_framework`.
- `describe_framework(what=summary|clocks|gpio|peripherals|unresolved, session?)`.
- `render_framework(style=hal, session?)` — render the persisted plan to code.
- Per-session `_design = {"current": None, "last_render": None}` mirroring board /
  acceptance / loop persistence.
- Tests appended to `tests/test_server_tools.py`.

## Tier 3 — auto-derive the AcceptanceSpec from the plan (welds D -> B1 -> C)

The machine now generates the pass/fail judge itself, so the whole
`netlist + spec -> design -> code -> acceptance -> loop` chain needs no
hand-written checks. Deterministic and honest: every check is a resolved fact;
anything unresolvable is surfaced, never guessed.

- `acceptance_synth.py` (pure Tier 1 core):
  - `derive_acceptance_spec(plan, clock_resolver=None, options=None)` ->
    `{spec, unresolved, notes, stats}`. Always emits a `no_fault` check
    (`no_fault_after_init`; target-independent ARM SCB fact). For each clock the
    plan enables, calls `clock_resolver(name)`; if resolved -> a `memory_u32`
    `bits_set` check `expect = hex(1 << bit)` (no mask needed); else -> an
    `unresolved` entry. Optional `stopped_at` symbol check.
  - `dict_clock_resolver(register_map, line, family)` — resolve RCC enable-bit
    placements from an explicit `{line_or_family: {clock: {address, bit}}}` map
    (line first, then family).
  - `svd_clock_resolver(svd_parser, rcc_name="RCC")` — resolve placements from a
    loaded SVD by scanning RCC enable registers for the `<NAME>EN` field (GPIO
    ports also try `IOP<L>EN` for F1/L0). Never raises.
- Deliberately bounded: only `no_fault` + RCC clock-enable checks. Peripheral-
  enable (e.g. `USART1 CR1.UE`) and GPIO `MODER` checks are deferred because GPIO
  register layout differs radically across families (F1 CRL/CRH vs F4/L4 MODER) —
  correctness risk not worth taking in the deterministic layer.
- Tier 2 tool `synthesize_acceptance(register_map?, stopped_at?, include_no_fault?,
  load?, name?, session?)`: derives the spec from the session's FrameworkPlan,
  resolves clock placements from `register_map` (preferred) or the session's loaded
  SVD, validates it, and (by default) loads it as the session acceptance judge.
  Returns `placement_source` (register_map | svd | none), `unresolved`, `notes`,
  `stats`, `loaded`.
- Tests: `tests/test_acceptance_synth.py` (13) + 6 server tests.

## Not in scope (future) / 暂不包含

- Clock-tree solver (`SystemClock_Config` stays an honest TODO stub).
- Register-level (non-HAL) codegen back-ends.
- Peripheral-enable / GPIO-MODER acceptance checks (family-specific register
  layouts; deferred from Tier 3 to keep the deterministic judge correct).

## Status / 状态
- [x] **Tier 1** — `framework_solver.py` + `framework_render.py` + tests. (16 + 12 tests)
- [x] **Tier 2** — `design/describe/render_framework` tools + per-session state + tests. (5 server tests; suite 340 passed / 1 skipped)
- [x] **Tier 3** — `acceptance_synth.py` + `synthesize_acceptance` tool welding D -> B1 -> C. (13 + 6 tests; suite 359 passed / 1 skipped)
- [x] **Tier 3 (cont.)** — deterministic clock-tree solver (`clock_solver.py` + `solve_clock_tree`); render emits a real `SystemClock_Config`. (see `2026-07-01-clock-tree-solver.md`; suite 382 passed / 1 skipped)
- [x] **Tier 3 (cont.)** — DB-derived GPIO alternate-function resolution (see below; 2 + 2 + 4 tests; suite 390 passed / 1 skipped)

## Tier 3 (cont.) — DB-derived GPIO alternate-function numbers

The renderer previously left `GPIO_InitStruct.Alternate` as a datasheet TODO
whenever the caller did not hand-write an `af_map`. The pin-capability DB
(`PinCapabilityDB`, CubeMX-derived) already lists which `{peripheral, signal}`
each port pin can carry; entries now accept an optional integer `"af"` so the
same DB doubles as the authoritative source for the alternate-function *number*.

- `PinCapabilityDB.af_map()` projects entries that carry an `"af"` into the
  solver's existing `af_map` shape `{line_or_family: {port_pin: {"PERIPH_SIG": af}}}`.
  Entries without an `af` are omitted — an unknown pin stays unresolved, never
  guessed.
- `framework_solver.merge_af_maps(base, override)` layers an explicit `af_map`
  on top of the DB-derived one (explicit wins per pin), so a caller can correct
  or extend the DB without restating every pin.
- `design_framework(db_path?)` (or the `STM32_GDB_MCP_PIN_DB` env, mirroring
  `validate_board`) loads the DB, derives an `af_map`, merges any explicit
  `af_map`, and feeds `build_framework_plan`. With the DB present the rendered
  code carries concrete `GPIO_AF<n>_<PERIPH>` values and the `af_unknown` /
  `TODO: Alternate` markers disappear for every pin the DB knows.
- Honest by construction: a pin missing from the DB still renders the datasheet
  TODO and reports `af_unknown`; a bad `db_path` yields an `invalid_db` error.
