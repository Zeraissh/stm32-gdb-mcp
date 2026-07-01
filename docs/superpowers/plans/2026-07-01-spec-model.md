# Product-spec entry -- controlled-vocabulary spec to design params (Pillar D upstream)

## Why / 目标

The spec-to-silicon pipeline has exactly one hand-written link left with **no
machine guard**: turning a **product spec** (产品规格) into the per-peripheral
`design={...}` params `design_framework` consumes. Netlists already have a
deterministic parser (Pillar A); product specs do not. The agent reads a human
requirements doc and hand-writes HAL-flavoured design params -- and it is the
**most upstream** step, so a mistranslation here (wrong baud, a dropped
peripheral, an 8N1 that should have been 8E1) silently propagates through every
deterministic stage below it and generates precisely-wrong code.

`spec_model.py` closes that gap the same way every other pillar does: a
deterministic reducer, never a guess. The agent's creative job shrinks to
"translate the human doc into a controlled-vocabulary spec"; the machine does
the mechanical, error-prone part -- product terms -> HAL design params -- and
cross-checks the spec against the imported netlist.

## Boundary / 边界

- **Agent (creative):** natural-language requirements -> controlled spec dict.
- **Machine (deterministic):** controlled spec -> `design` params + honest
  `unresolved` / `conflict`. Never invents a peripheral, a pin, or a value.

## Controlled vocabulary (v1)

Per-peripheral intent keys, human/product terms (not HAL macros):

- **uart:** `baud` (int), `framing` ("8N1"/"8E1"/"9N1"...), `direction`
  ("tx"|"rx"|"txrx"), `flow_control` ("none"|"rts"|"cts"|"rtscts").
- **spi:** `role` ("master"|"slave"), `spi_mode` (0..3 -> CPOL/CPHA),
  `data_size` (8|16), `bit_order` ("msb"|"lsb").
- **i2c:** `speed` ("standard"|"fast"|100000|400000), `addressing`
  ("7bit"|"10bit"), `own_address` (int).
- **adc:** `resolution` (12|10|8|6), `conversion` ("single"|"continuous").
- **timer:** `update_hz` (number, passed through to solve_timer).
- **any kind:** `dma` (true|"rx"|"tx"|["rx","tx"]), `interrupt` (bool -> nvic),
  `priority` (int|[preempt,sub] -> nvic_priority). These pass straight through
  to the existing NVIC/DMA opt-ins.

Deterministic rules that are *rules, not guesses*:

- `framing` "DPS" -> WordLength/Parity/StopBits. HAL counts the parity bit in
  WordLength (CubeMX does the same): frame_bits = data_bits + (parity?1:0);
  8E1 -> WordLength_9B + PARITY_EVEN. A framing whose frame width is not in
  {7,8,9} -> unresolved.
- `spi_mode` 0..3 -> (CPOL, CPHA) by the standard SPI-mode table.
- `speed` "standard"/"fast" -> 100000/400000; the concrete I2C `Timing` is
  clock-dependent, so v1 records the target and leaves Timing to a TODO rather
  than fabricating a value.

## Cross-check against the netlist

Given the imported board, every peripheral the spec names is checked against
`peripherals_in_use(board)`:

- named but absent -> `spec_conflict` ("spec requires USART1 but the netlist
  has no USART1 pins").
- an intent key the machine does not model -> `spec_unresolved` (surfaced,
  never dropped, never guessed).

## Output

```
{
  "design":     {peripheral: {hal_design_keys...}},   # feeds design_framework
  "unresolved": [{"peripheral","key","value","reason"}],
  "conflicts":  [{"peripheral","reason"}],
  "notes":      [ ... ],   # e.g. the parity-bit WordLength rule
  "stats":      {"peripherals","resolved_keys","unresolved","conflicts"}
}
```

## Tier 1 -- pure reducer (unit-tested, no hardware)

- `spec_model.py`: `build_design(spec, board=None)` + per-kind translation
  tables + framing/spi-mode/speed rules + board cross-check.
- `tests/test_spec_model.py`.

## Tier 2 -- MCP tool + session state

- `import_spec` tool: store a controlled spec per session; return the
  translated design + unresolved/conflicts.
- `design_framework` gains a `from_spec` path (use the stored spec's design).
- Server tests through `handle_call_tool`.

## Status

- [x] Tier 1 -- `spec_model.py` + unit tests.
- [x] Tier 2 -- `import_spec` tool + design bridge + tests.
- [x] Docs + CHANGELOG.
