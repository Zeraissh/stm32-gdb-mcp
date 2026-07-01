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

## Not in scope (future) / 暂不包含

- Clock-tree solver (`SystemClock_Config` stays an honest TODO stub).
- Register-level (non-HAL) codegen back-ends.
- Auto-deriving an AcceptanceSpec from the plan (a natural Tier 3: "after init these
  RCC bits are set / these pins are muxed" → feeds Pillar B1 automatically).

## Status / 状态
- [x] **Tier 1** — `framework_solver.py` + `framework_render.py` + tests. (16 + 12 tests)
- [x] **Tier 2** — `design/describe/render_framework` tools + per-session state + tests. (5 server tests; suite 340 passed / 1 skipped)
