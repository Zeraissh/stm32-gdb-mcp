# DMA association (Pillar D Tier 3)

Auto-attach DMA streams/channels to a peripheral straight from design intent, and
reuse the NVIC backbone for the DMA transfer interrupt. The natural follow-up to
the NVIC backbone: an interrupt-driven peripheral often wants DMA, and a DMA
stream needs its own NVIC vector + ISR calling `HAL_DMA_IRQHandler`.

## Determinism boundary

- The **DMA request routing** -- which `(controller, stream/channel, selector)` a
  `(peripheral, direction)` maps to -- is a fixed hardware fact from the reference
  manual. It is encoded in a small **verified** per-family table, cross-checked
  against ground-truth CubeMX output (not a single AI summary, which got SPI1/ADC1
  request numbers wrong).
  - **STM32F4** (RM0090): stream + channel. `hdma.Init.Channel = DMA_CHANNEL_n`,
    `Instance = DMAc_Streams`.
  - **STM32L4** (RM0394): channel + CSELR request. `hdma.Init.Request = DMA_REQUEST_n`,
    `Instance = DMAc_Channels`.
- The **DMA stream/channel IRQ vector** is regularly named
  (`DMAc_Streams_IRQn` / `DMAc_Channels_IRQn`) so it is *derived*, not tabled --
  this is the "reuse NVIC" part: each DMA stream contributes an NVIC vector whose
  ISR calls `HAL_DMA_IRQHandler(&hdma_...)`.
- Anything not in the table (other peripherals, other families, TIM/DAC DMA,
  DMAMUX-only parts) is surfaced as `dma_unresolved` with a reason and a `dma=`
  override escape hatch -- never a guessed stream.
- A DMA instance used by two peripherals is a hardware impossibility, so a
  cross-MCU dedup pass surfaces `dma_conflict` for the loser instead of emitting
  two inits on one stream.

## Verified table (v1 scope: USART1 / SPI1 / I2C1 / ADC1)

| periph | dir | F4 (ctrl, stream, ch) | L4 (ctrl, channel, req) |
|--------|-----|-----------------------|--------------------------|
| USART1 | rx  | DMA2 S2 C4            | DMA1 Ch5 Req2            |
| USART1 | tx  | DMA2 S7 C4            | DMA1 Ch4 Req2            |
| SPI1   | rx  | DMA2 S0 C3            | DMA1 Ch2 Req1            |
| SPI1   | tx  | DMA2 S3 C3            | DMA1 Ch3 Req1            |
| I2C1   | rx  | DMA1 S0 C1            | DMA1 Ch7 Req3            |
| I2C1   | tx  | DMA1 S6 C1            | DMA1 Ch6 Req3            |
| ADC1   | rx  | DMA2 S4 C0            | DMA1 Ch1 Req0            |

## Design input (opt-in, per peripheral in `design[name]`)

- `"dma": true` -- attach DMA for the natural directions (rx+tx for uart/spi/i2c;
  rx for adc).
- `"dma": "rx"` / `"tx"` / `["rx", "tx"]` -- specific directions.
- `"dma_priority": "low"|"medium"|"high"|"very_high"` -- DMA channel priority
  (`hdma.Init.Priority`, default LOW).
- `DMA_KEYS = ("dma", "dma_priority")` popped before the `.Init` mapping.

DMA and the peripheral's own `nvic` directive are independent opt-ins: DMA always
enables its *stream* IRQ (needed for transfer-complete), which is separate from the
peripheral global interrupt.

## Tier 1 -- dma_solver.py (pure)

`build_dma(name, kind, family, dma, dma_priority)` -> block `dma` dict or `None`.
Streams carry Instance / select field+value / direction macro / link field
(`hdmarx`/`hdmatx`/`DMA_Handle`) / clock macro / the derived stream `nvic` vector.

## Tier 2 -- intent capture + render

- `framework_solver`: pop the DMA keys, build the block, thread `family`, add
  `dma_unresolved` + plan-level `dma_conflict`, summarize.
- `framework_render`: declare DMA handles; emit clock enable + `HAL_DMA_Init` +
  `__HAL_LINKDMA` + DMA-stream `HAL_NVIC_*`; one ISR per DMA vector dispatching to
  `HAL_DMA_IRQHandler`. No new MCP tool -- DMA rides the existing `design_framework`
  design param, so the tool count is unchanged.

## Status

- [x] Tier 1 -- `dma_solver.py` + unit tests.
- [x] Tier 2 -- intent capture + DMA/LINKDMA/ISR render + tests.
- [x] Docs + CHANGELOG.
