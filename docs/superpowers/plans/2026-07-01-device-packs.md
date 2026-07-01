# Device packs — data-driven family/peripheral facts (Pillar F)

## Why / 目标

Coverage of device-specific facts (DMA request routing, NVIC irregular vectors,
clock PLL profiles, timer bus/width) is today **hardcoded** for STM32F4 / L4 in
four solvers. Adding a family means hand-coding another table — and every
hand-coded datasheet fact is a hallucination risk the machine layer must never
take. GPIO **alternate-function** numbers already avoid this: they are
data-driven (`db_path` CubeMX DB / `af_map`), so the machine does the logic and a
*verifiable* data source supplies the device fact.

Generalize that pattern to the other three fact tables: a **device pack** is a
validated JSON object of verified facts for one family. F4 / L4 ship as built-in
packs; new families become "supply a verified pack", never "trust the model".

## Core principle / 核心原则

The pack carries only **facts** (verified against the reference manual / CubeMX).
The solvers keep all the deterministic *logic*. A family with no pack stays
honestly `unresolved` — exactly as today. A malformed pack is rejected with a
list of problems, never silently half-loaded.

## Pack schema (`stm32-device-pack/v1`)

```jsonc
{
  "schema": "stm32-device-pack/v1",
  "family": "STM32F4",
  "clock": {                      // optional
    "exclude_lines": ["STM32L4R"],       // known-unmodelled line prefixes -> None
    "profiles": [
      {"match_lines": ["STM32F407", "STM32F405"],   // exact line-prefix match (phase 1)
       "match_prefix": "STM32L4",                    // broad fallback (phase 3)
       "match_family": "STM32L4",                    // family fallback (phase 3)
       "profile": { /* sysclk_pll_field, hsi_hz, pll{}, max_*_hz, flash_latency, ... */ }}
    ]
  },
  "dma": {                        // optional
    "arch": {"unit": "Stream", "select_field": "Channel", "select_prefix": "DMA_CHANNEL_"},
    "map": {"USART1": {"rx": [2, 2, 4], "tx": [2, 7, 4]}}   // [controller, unit, selector]
  },
  "nvic": {                       // optional
    "i2c_dual": true,             // family has the EV/ER split vector pair
    "irq": {"TIM6": ["TIM6_DAC_IRQn"], "ADC1": ["ADC_IRQn"]}   // irregular vectors only
  },
  "timer": {                      // optional
    "apb2": ["TIM1", "TIM8"],     // timers on APB2 (rest on APB1)
    "bits32": ["TIM2", "TIM5"]    // 32-bit counters (rest 16-bit)
  }
}
```

## Tier 1 — pure registry + rewired solvers (unit-tested)

- `device_packs.py` (NEW, pure): built-in F4/L4 packs (facts relocated verbatim
  from the solvers), `validate_pack`, `register_pack(allow_override)`, `get_pack`,
  `reset_external` (tests), `load_pack(path)` (JSON -> normalize -> validate), and
  accessors: `dma_arch` / `dma_map` / `dma_families`, `nvic_table` / `i2c_dual`,
  `timer_apb2` / `timer_bits32`, `clock_resolution_data`.
- Rewire `dma_solver` / `interrupt_solver` / `timer_solver` / `clock_solver` to
  read facts through the accessors. Public signatures unchanged, so F4/L4 behavior
  is byte-identical and the 536-test suite stays green.
- `i2c_dual` keeps a built-in seed set (F1/F2/F4/F7/L1/L4 — a naming rule, not
  full packs) that a pack's `nvic.i2c_dual` extends.

## Tier 2 — MCP surface

- `load_device_pack` tool: register a pack from `path` or inline `pack`
  (`allow_override` to shadow a built-in); with neither, reports current coverage
  (list mode). Honest: validation problems come back as a `content_error` list;
  a would-be built-in shadow is refused unless `allow_override`.

## Not fabricating / 不臆造

No unverified device facts ship in this change. Built-ins are the already-verified
F4/L4 facts, merely relocated. New families arrive as user-supplied, verifiable
packs. The schema + validator are the guard rails.

## Status / 状态
- [x] `device_packs.py` — built-ins + validate + register + load + accessors.
- [x] Rewire dma / interrupt / timer / clock solvers to the registry.
- [x] `load_device_pack` tool (register / list, honest validation).
- [x] Tests (registry + rewired solvers + server tool) + docs + suite green / ruff clean.
