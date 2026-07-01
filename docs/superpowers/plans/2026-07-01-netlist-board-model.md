# Netlist → Board Model Implementation Plan / 网表 → 板级模型实现计划

> **For agentic workers:** Implement this plan task-by-task using TDD (test-first).
> Each capability is a focused module + server tool(s) + tests, committed independently,
> matching the existing `reset_strategy.py` / `svd_parser.py` style (pure functions,
> plain-dict models, no dataclasses — everything is JSON-native for the
> `content_success` envelope).

**Goal / 目标:** Turn a schematic **netlist** into a machine-readable **BoardDescription**
(a BSP model) — the *input contract* that lets the agent auto-derive pin mux, clock
sources, and the peripheral inventory for framework design. This is Pillar A of the
spec-to-silicon pipeline (网表+规格 → 框架设计 → 代码 → 闭环验证). It is pure software,
unit-tested with fixtures/fakes, and needs **no hardware**.

**Why first / 为什么先做:** Without a machine-readable board, "auto framework design" is
guesswork. Every downstream stage (clock-tree solver, pin-mux validator, init codegen,
acceptance synthesis) consumes this model. Highest ROI, fully offline-testable.

**Architecture / 架构:** Keep `server.py` a thin MCP adapter. Put parsing + modeling in
two focused modules with unit tests. `netlist_parser.py` turns netlist text into raw
`components` + `nets`; `board_model.py` assembles the normalized BoardDescription and
infers pin functions. All tool results use the existing `content_success` /
`content_error` envelope and populate `suggested_next_actions` toward the next pipeline
stage.

**Tech Stack:** Python 3.10+, stdlib only for parsing (a tiny S-expression reader),
pytest, ruff (E/F/I/UP/B @120).

---

## BoardDescription contract (the output schema)

```jsonc
{
  "source": "<path|memory>",
  "format": "kicad",
  "mcu": {
    "ref": "U1",
    "part": "STM32L431CBT6",
    "part_normalized": "STM32L431CBT6",
    "family": "STM32L4",
    "line": "STM32L431",
    "pins": [
      {"package_pin": "42", "port_pin": "PA9", "net": "/USART1_TX",
       "function": {"peripheral": "USART1", "signal": "TX"}}
    ]
  },
  "components": [
    {"ref": "U1", "value": "STM32L431CBT6", "footprint": "…", "pins": {"42": "/USART1_TX"}}
  ],
  "nets": [
    {"name": "/USART1_TX", "nodes": [{"ref": "U1", "pin": "42", "port_pin": "PA9"}]}
  ],
  "power_nets": {"power": ["+3V3"], "ground": ["GND"]},
  "warnings": [],
  "stats": {"component_count": 3, "net_count": 4, "mcu_pin_count": 2}
}
```

---

## Tier 1 — Parse & model (offline, this change)

### T1.1 `board_model.py` — normalized model + inference
- Pure helpers (plain dicts, no dataclasses):
  - `infer_pin_function(net_name) -> {peripheral, signal} | None` (net-name heuristics for
    USART/UART/LPUART, I2C, SPI, CAN/FDCAN, USB/OTG, TIM channels, ADC/DAC, SDMMC/SDIO,
    QSPI/OCTOSPI, plus system pins SWD/JTAG/NRST/BOOT/OSC/MCO). Tolerant of hierarchical
    (`/sheet/USART1_TX`) and prefixed (`MCU_I2C1_SDA`) labels.
  - `normalize_mcu_part(value) -> {part, part_normalized, family, line} | None`.
  - `is_mcu_value(value) -> bool` (STM32 today; extension point for other vendors).
  - `classify_power_net(name) -> "power" | "ground" | None`.
  - `build_board_description(components, nets, source, fmt, warnings) -> dict` — identifies
    the MCU, assembles its pin list with inferred functions, classifies power nets, emits
    warnings (no MCU / multiple MCU candidates), fills `stats`.
- Tests: `tests/test_board_model.py` (inference table, part normalization, power
  classification, assembly + warning paths).

### T1.2 `netlist_parser.py` — KiCad `.net` reader
- Minimal S-expression tokenizer/parser (stdlib only), then:
  - `parse_kicad_netlist(text) -> (components, nets)` (reads `(comp …)` and `(net … (node …))`,
    capturing KiCad `pinfunction` as `port_pin`).
  - `detect_format(text) -> "kicad" | "unknown"`.
  - `parse_netlist(text, fmt="auto", source) -> BoardDescription` (delegates to `board_model`).
  - `load_netlist_file(path, fmt="auto") -> BoardDescription`.
- Tests: `tests/test_netlist_parser.py` (components, net→pinmap, full BoardDescription,
  format detection, unsupported-format error, file loading via `tmp_path`).

---

## Tier 2 — Expose as MCP tools

### T2.1 `import_netlist` / `describe_board`
- `import_netlist(path | text, format="auto")` → parse, stash on the session, return summary.
- `describe_board(what=summary|pins|nets|power|peripherals)` → filtered views.
- Register in `handle_list_tools` schema + `_dispatch_tool`; results carry
  `suggested_next_actions` → `["validate_board", "plan_framework"]`.
- Tests: `tests/test_server_tools.py` exposure + argument passthrough with a fake.

### T2.2 Persist board on the session
- Add a per-session `board` attribute on `DebugSession` (and the `default` namespace) so
  downstream design tools read a single source of truth. Tests.

---

## Tier 3 — Validate against MCU pinout / AF database

### T3.1 AF-legality + conflict detection
- Optional MCU pin/AF database adapter (CubeMX MCU DB or a bundled JSON subset):
  validate each net's inferred function is a legal alternate function for that package
  pin; detect two nets on one pin, illegal AF, DMA-stream / timer-channel collisions.
- Tests with a small fixture DB.

### T3.2 `validate_board` tool
- Returns `{conflicts, unassigned, warnings}` + `suggested_next_actions` → `plan_framework`.

---

## Tier 4 — More input formats
- Altium `.NET` (Protel), OrCAD, and a generic CSV pin-map importer, with format
  autodetect. Tests per format.

---

## Cross-cutting
- Plain-dict models only (JSON-native, matches house style) — no dataclasses.
- Keep `python -m ruff check .` and `python -m pytest -q` green after every task.
- Every tool populates `suggested_next_actions` pointing to the next pipeline stage.
- Update `README.md` (Roadmap) + `CHANGELOG.md` once Tier 2 exposes the tools.

## Execution order
T1 (this change) → T2 → T3 → T4. Each capability committed independently, mirroring
existing history.

## Status / 状态
- [~] **Tier 1** — `board_model.py` + `netlist_parser.py` + tests. *In progress (this change).*
- [ ] **Tier 2** — `import_netlist` / `describe_board`, session persistence.
- [ ] **Tier 3** — AF-legality validation + `validate_board`.
- [ ] **Tier 4** — Altium / OrCAD / CSV importers.
