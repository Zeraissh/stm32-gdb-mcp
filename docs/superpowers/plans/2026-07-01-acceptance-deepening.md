# Acceptance deepening -- derive NVIC ISER + GPIO MODER checks (Pillar D Tier 3, cont.)

## Why / 目标

The auto-derived AcceptanceSpec (`acceptance_synth.py`) welds the design solver
to the acceptance judge: the machine writes the pass/fail contract from what the
FrameworkPlan said the init code would do. But it only verified **two** of the
things the plan actually does -- `no_fault` and RCC **clock enables**. The plan
also resolves **interrupts** (NVIC backbone) and **DMA stream vectors**, and
configures every **GPIO pin's mode**; none of that was checked, so a plan that
enabled `HAL_NVIC_EnableIRQ` or set a pin to alternate-function had no machine
witness that the silicon reached that state.

This deepens the derived judge to also emit, honestly:

- **NVIC ISER checks** -- for each interrupt the plan enables (peripheral vectors
  + DMA stream vectors), assert its NVIC set-enable bit is set.
- **GPIO MODER checks** -- for each configured pin, assert its two mode bits match
  the planned role (AF / analog).

## The determinism boundary (why this stays honest)

Two of the three new placements are **architecture-standard** and therefore need
no per-device data at all:

- **NVIC ISER** is Cortex-M standard: `ISER[n] = 0xE000E100 + 4*n`, and IRQ number
  `k` is bit `k % 32` of `ISER[k // 32]`. The only device-specific fact is the IRQ
  **number** for a vector *name* (`USART1_IRQn` -> 37 on this line). That number
  comes from an `irq_resolver` (the SVD's `<interrupt>` table, or an explicit
  `irq_map`) -- exactly the same resolver pattern the clock checks already use.
  A vector whose number the resolver can't place is `unresolved`, never guessed.
- **GPIO MODER** offset-0, two-bits-per-pin layout (AF = `0b10`, analog = `0b11`)
  is arch-standard on every STM32 GPIO port **except F1** (which uses CRL/CRH and
  is skipped honestly). The only device-specific fact is the port **base address**,
  from a `gpio_resolver` (the SVD's `GPIO<port>.MODER` register address, or an
  explicit `gpio_map`). MODER's exact-value semantics are expressed with the
  evaluator's existing `mask` field: `mask = 0b11 << 2p`, `expect = mode << 2p`,
  `op = eq`.

**Deliberately still out of scope:** peripheral-enable bits (USART `UE`, SPI `SPE`,
I2C `PE`, ...). Unlike ISER/MODER these are **not** arch-standard *and* HAL's
post-`Init` state is not uniform (e.g. `HAL_SPI_Init` does not set `SPE`), so a
derived check would risk a false failure. An honest judge omits it rather than
emit a check that can wrongly fail.

## What the machine does / 机器职责

- `svd_parser.interrupt_numbers()` -- expose the SVD's `{interrupt_name: number}`
  table (device IRQ numbers already present in the SVD).
- `dict_irq_resolver` / `svd_irq_resolver` -- IRQ name -> number (name with or
  without the `_IRQn` suffix).
- `dict_gpio_resolver` / `svd_gpio_resolver` -- GPIO port letter -> MODER base.
- `derive_acceptance_spec(plan, clock_resolver, options, irq_resolver, gpio_resolver)`
  -- adds, per resolved target, a `memory_u32` check; dedupes NVIC vectors by IRQ
  name; skips conflicting DMA streams (their init is not emitted); surfaces every
  unresolved number/base/family in `unresolved`; counts `nvic_checks` / `gpio_checks`.
  `options.include_nvic` / `include_gpio` gate each family (default on).

## Tier 2 / 工具集成

`synthesize_acceptance` gains optional `irq_map` / `gpio_map` (symmetric to
`register_map`) and `include_nvic` / `include_gpio`. Placements come from those
maps, else the session's loaded SVD, else `none`. The response reports
`resolver_sources = {clock, nvic, gpio}` and `stats.nvic_checks` / `gpio_checks`.

## Boundary / 边界

- **Agent (creative):** nothing new -- this is pure machine deepening of the judge.
- **Machine (deterministic):** plan -> the additional NVIC ISER + GPIO MODER checks,
  arch-standard placement, honest `unresolved` for any device-specific fact a
  resolver can't supply. Never a fabricated address, bit, or IRQ number.

## Status / 状态
- [x] `svd_parser.interrupt_numbers()`.
- [x] `acceptance_synth.py` -- NVIC ISER + GPIO MODER derivation + four resolvers.
- [x] `synthesize_acceptance` wiring (`irq_map`/`gpio_map`, `include_nvic`/`include_gpio`,
      `resolver_sources`).
- [x] Tests: 16 unit (`test_acceptance_synth.py`) + 3 server (`test_server_tools.py`);
      suite 514 passed / 1 skipped; ruff clean.
