# NVIC interrupt backbone (Pillar D Tier 3)

## Why / 目标

Interrupt-driven peripherals are the norm in real firmware, but the generated
init left the whole NVIC story as an implicit gap: no `HAL_NVIC_SetPriority` /
`HAL_NVIC_EnableIRQ`, and no interrupt service routine to dispatch into the HAL
handler. A senior engineer says "run USART1 on interrupt at preemption 5"; the
machine should turn that into the correct `IRQn`, the two NVIC calls, and a
`USART1_IRQHandler` that calls `HAL_UART_IRQHandler(&huart1)` -- deterministically.

## Core principle / 核心原则

The interrupt *vector name* is CMSIS/datasheet data. The regularly-named vectors
(`USARTx_IRQn`, `SPIx_IRQn`, and the `I2Cx_EV/ER` pair) are derived by universal
rule. The irregular ones -- advanced-timer combined vectors, shared ADC/DAC/timer
vectors -- are resolved from a small built-in per-family table (F4/L4) where the
mapping is well known, and otherwise **surfaced honestly** (`nvic_unresolved` with
a reason and an `irqn=` escape hatch), never guessed. The interrupt **priority** is
an engineer decision (design input); a working default is emitted with a review
note when none is given.

## Opt-in design input

Per peripheral in `design[name]`:

- `"nvic": true` -- enable the interrupt with the default priority.
- `"nvic_priority": 5` or `[preempt, sub]` -- enable with an explicit priority.
- `"nvic": {"preempt": 5, "sub": 2}` -- enable with an explicit priority.
- `"irqn": "TIM1_UP_TIM10_IRQn"` or a list -- supply/override the vector(s) for a
  peripheral the built-in tables do not know (also implies enable).

Absent -> no interrupt config for that peripheral (a polled peripheral needs none).

## Tier 1 — pure resolver (`interrupt_solver.py`)

- `resolve_vectors(name, kind, family, irqn_override=None)` -> a list of
  `{irqn, handler, isr, role, source}`, or `[]` when unresolved. Precedence:
  `irqn` override > built-in family table > universal regular rule > unresolved.
  - uart/spi -> `{name}_IRQn` (universal single vector).
  - i2c -> `{name}_EV_IRQn` + `{name}_ER_IRQn` for the EV/ER families; else unresolved.
  - timer/adc/dac -> `_NVIC_IRQ[family][name]` (F4/L4: TIM2-7, `TIM6_DAC_IRQn`, ADC, DAC).
- `build_nvic(name, kind, family, nvic, nvic_priority, irqn)` -> the block's
  `nvic` dict: `{requested, preempt, sub, priority_source, vectors, resolved,
  unresolved_reason}`. The HAL handler + ISR name are derived per kind/role
  (`HAL_UART_IRQHandler`, `HAL_I2C_EV/ER_IRQHandler`, `HAL_TIM_IRQHandler`, ...).

## Tier 2 — intent capture + render

- `build_framework_plan` pops the NVIC design keys, threads `family`, attaches the
  `nvic` block, and adds `nvic_unresolved` to `plan["unresolved"]` when an interrupt
  was requested but no vector is known.
- `render_framework` emits, inside each peripheral init, `HAL_NVIC_SetPriority` +
  `HAL_NVIC_EnableIRQ` for every resolved vector (or an honest `TODO` when
  unresolved), and appends one interrupt service routine per unique vector that
  dispatches into the HAL handler(s) -- shared vectors call every attached handle.

## Status / 状态

- [x] Tier 1 — `interrupt_solver.py` + unit tests.
- [x] Tier 2 — intent capture + NVIC/ISR render + tests.
- [x] Docs + CHANGELOG.
