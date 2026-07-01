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
- Peripheral-enable acceptance checks (USART `UE` / SPI `SPE` / I2C `PE` / ...):
  not architecture-standard and HAL's post-`Init` state is non-uniform, so a
  derived check could falsely fail -- kept out to keep the judge honest. (GPIO
  MODER checks, previously deferred here, are now derived via a resolver + F1
  exclusion; see the acceptance-deepening Status entry below.)

## Status / 状态
- [x] **Tier 1** — `framework_solver.py` + `framework_render.py` + tests. (16 + 12 tests)
- [x] **Tier 2** — `design/describe/render_framework` tools + per-session state + tests. (5 server tests; suite 340 passed / 1 skipped)
- [x] **Tier 3** — `acceptance_synth.py` + `synthesize_acceptance` tool welding D -> B1 -> C. (13 + 6 tests; suite 359 passed / 1 skipped)
- [x] **Tier 3 (cont.)** — deterministic clock-tree solver (`clock_solver.py` + `solve_clock_tree`); render emits a real `SystemClock_Config`. (see `2026-07-01-clock-tree-solver.md`; suite 382 passed / 1 skipped)
- [x] **Tier 3 (cont.)** — DB-derived GPIO alternate-function resolution (see below; 2 + 2 + 4 tests; suite 390 passed / 1 skipped)
- [x] **Tier 3 (cont.)** — complete peripheral `.Init` structs: HAL-standard defaults + netlist-derived values (UART flow control, SPI NSS) + honest required-decision TODOs (see below; suite 395 passed / 1 skipped)
- [x] **Tier 3 (cont.)** — deterministic timer base-frequency solver (`timer_solver.py` + `solve_timer`): a recorded update frequency becomes concrete PSC/ARR via TIMxCLK from the solved clock tree (see `2026-07-01-timer-solver.md`; 15 + 5 tests; suite 415 passed / 1 skipped)
- [x] **Tier 3 (cont.)** — deterministic NVIC interrupt backbone (`interrupt_solver.py`; interrupts captured through `design_framework`): resolved IRQn + priority + `HAL_NVIC_EnableIRQ` + one dispatching ISR per vector, with honest `nvic_unresolved` / `TODO` for irregular or unknown vectors instead of a guessed IRQn (see `2026-07-01-nvic-backbone.md`; 12 + 6 + 6 + 2 tests; suite 439 passed / 1 skipped)
- [x] **Tier 3 (cont.)** — deterministic DMA association (`dma_solver.py`; DMA captured through `design_framework`): a peripheral opts in (`dma: true`/`"rx"`/`"tx"`) and the render emits the `DMA_HandleTypeDef` wiring + `HAL_DMA_Init` + `__HAL_LINKDMA` + the DMA stream/channel `HAL_NVIC_*` and an ISR into `HAL_DMA_IRQHandler` (reusing the NVIC backbone). Verified F4 (stream+channel) / L4 (channel+CSELR request) table for USART1/SPI1/I2C1/ADC1; unmapped peripherals or a stream collision surface as `dma_unresolved` / `dma_conflict`, never a guessed stream (see `2026-07-01-dma-association.md`; 15 + 6 + 4 + 2 tests; suite 465 passed / 1 skipped)
- [x] **Upstream product-spec guard** — deterministic controlled-vocabulary spec reducer (`spec_model.py`; `import_spec` tool + `design_framework(from_spec=true)`): human/product intent (UART framing `8N1`, SPI `spi_mode` 0..3, I2C `speed`/`addressing`, ADC `resolution`/`conversion`, timer `update_hz`, plus dma/interrupt/priority opt-ins) is expanded to HAL design params deterministically (8E1 → UART_WORDLENGTH_9B + UART_PARITY_EVEN by HAL's parity-bit rule) and cross-checked against the netlist — a peripheral absent from the board is a `conflict`, an unmodelled key/value is `unresolved`, and the I2C bus-timing register is recorded but never guessed. This closes the pipeline's last hand-written link that had no machine guard, and the most upstream one (see `2026-07-01-spec-model.md`; 26 + 4 tests; suite 495 passed / 1 skipped)
- [x] **Tier 3 (cont.)** — deepened auto-derived acceptance: on top of no_fault + RCC clock enables, `synthesize_acceptance` now also derives **NVIC ISER** checks for every interrupt the plan enables (peripheral + DMA-stream vectors; arch-standard `0xE000E100 + 4*(irq//32)`, bit `irq%32`, with the IRQ *number* resolved from the SVD or an `irq_map`) and **GPIO MODER** checks for every configured pin (masked-eq AF=`0b10`/analog=`0b11`, MODER base from the SVD or a `gpio_map`, F1's CRL/CRH skipped honestly). Peripheral-enable bits stay out of scope (not arch-standard; non-uniform HAL post-Init state). Adds `svd_parser.interrupt_numbers()` (see `2026-07-01-acceptance-deepening.md`; 16 + 3 tests; suite 514 passed / 1 skipped)
- [x] **Pillar E — failure→source provenance** — every derived check now carries a `provenance` join key (clock macro / IRQ name / port-pin / stopped-at symbol), `render_framework` returns a `source_map` scanned from the rendered init text (`provenance.build_source_map`: per-file functions + tagged constructs with 1-based lines), and `synthesize_acceptance` welds the two at synth time (`annotate_spec_sources`) so each stored check gains `provenance.source = {located, file, init_fn, line, text}`. `evaluate_acceptance` passes provenance through onto every **non-pass** result, so a failing `run_acceptance` verdict **and** every Pillar-C loop verdict point straight at `bsp_init.c:line in MX_*_Init` — zero new tools (stays 77), zero loop changes. Honest `located:false` when a construct was not emitted or the plan drifted; passing results stay lean (see `2026-07-01-failure-provenance.md`; 13 + 7 + 2 tests; suite 536 passed / 1 skipped)

## Tier 3 (cont.) — complete peripheral .Init structs

Previously a peripheral init emitted only the fields the engineer explicitly
passed, so `design={"USART1": {"baud": 115200}}` rendered a struct with
`BaudRate` set but `WordLength` / `StopBits` / `Parity` / `Mode` / `OverSampling`
**uninitialized** — a latent bug (garbage HAL config from stack junk). The init
struct is now always complete and valid:

- **HAL-standard defaults.** `framework_solver._KIND_PARAMS` gives each kind
  (UART / SPI / I2C / TIM) its canonical `.Init` field order plus the default
  value CubeMX itself emits for every mandatory member (UART 8N1 / 16x oversample
  / TX+RX; SPI master / 8-bit / mode 0 / MSB; I2C 7-bit addressing / no stretch;
  TIM up-counting / div1). Filling them is strictly more correct than leaving
  members uninitialized — not a target-specific guess.
- **Netlist-derived values.** UART `HwFlowCtl` is derived from the presence of
  RTS/CTS pins on the board (`UART_HWCONTROL_RTS_CTS` / `_RTS` / `_CTS` / `_NONE`),
  and SPI `NSS` from a hardware NSS/CS pin (`SPI_NSS_HARD_OUTPUT` vs `_SOFT`).
  100% deterministic from the board model.
- **Transparent provenance.** Every field carries a `source` of
  `explicit` (engineer) / `derived` (board) / `default` (HAL standard), rendered
  as a trailing `/* default */` or `/* derived: ... */` comment. Precedence is
  explicit > derived > default, so an explicit value always wins.
- **Honest required decisions.** Values with no safe universal default — UART
  baud, TIM `Prescaler`/`Period`, I2C `Timing`/`ClockSpeed` (variant- and
  clock-dependent) — are never invented. When unset they surface as
  `param_unresolved` and render as a `TODO: set <handle>.Init.<field> -- <hint>`.
  Kinds with no default table (ADC/generic) keep the plain `no_config` hole.

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
