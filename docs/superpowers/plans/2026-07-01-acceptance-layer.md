# Acceptance Layer Implementation Plan / 验收层实现计划

> **For agentic workers:** Implement this plan task-by-task using TDD (test-first).
> Each capability is a focused module + server tool(s) + tests, committed independently,
> matching the existing `board_model.py` / `board_validation.py` style (pure functions,
> plain-dict models, no dataclasses — everything is JSON-native for the
> `content_success` envelope).

**Goal / 目标:** Turn a **product spec** into a machine-checked **AcceptanceSpec** — a set
of deterministic assertions evaluated against live silicon state (memory-mapped registers,
globals, core registers, fault status, PC) that yields an objective pass/fail verdict. This
is **Pillar B1** of the spec-to-silicon pipeline (网表+规格 → 框架设计 → 代码 → 闭环验证):
it is the *judge* that lets the closed loop decide "验证不过 → 继续改代码" without
hallucination.

**Why now / 为什么现在做:** Pillar A gives a machine-readable board. A bounded debug loop
(Pillar C) needs a machine-checked acceptance criterion to converge on — otherwise "does it
work?" is a subjective LLM guess. B1 provides the deterministic oracle. Like Pillar A, the
*authoring* of the spec (spec text → AcceptanceSpec JSON) is the engineer's / a later NLP
tier's job; B1 makes the checks **machine-checked and reproducible**, never fabricated.

**Design principle / 设计原则:** The evaluator NEVER invents a verdict. Each check reads a
concrete, observable value and compares it deterministically. If a target cannot be read,
that check is reported as `error` (not `pass`/`fail`) with the exception message — an
unreadable target never silently passes. This mirrors Pillar A Tier 3, where an unknown pin
degrades to `unverified` rather than a false positive.

**Architecture / 架构:** Keep `server.py` a thin MCP adapter.
- `acceptance_model.py` — validate/normalize an AcceptanceSpec + a summary view (pure data).
- `acceptance_eval.py` — `evaluate_acceptance(spec, reader)`; a small **reader protocol** the
  evaluator calls; `_compare` with the comparison ops; and `GdbAcceptanceReader`, the adapter
  wiring the protocol to the live `gdb_client`. The evaluator itself is reader-agnostic and
  unit-tested with a `FakeReader`.

**Tech Stack:** Python 3.10+, stdlib only, pytest, ruff (E/F/I/UP/B @120).

---

## AcceptanceSpec contract (the input schema)

```jsonc
{
  "name": "blinky-v1",
  "description": "LED on PC13 blinks; UART1 @115200; sysclk 80 MHz; no fault.",
  "checks": [
    // A 32-bit memory-mapped register (all Cortex-M peripherals are memory-mapped).
    {"id": "usart1-enabled", "kind": "memory_u32", "address": "0x40013800",
     "mask": "0x00000001", "op": "bits_set", "expect": "0x1",
     "description": "USART1->CR1 UE bit set"},

    // A C global / expression evaluated by GDB.
    {"id": "sysclk", "kind": "variable", "name": "SystemCoreClock",
     "op": "eq", "expect": 80000000},

    // A core / convenience register (pc, sp, lr, xpsr, r0..r15, msp, psp).
    {"id": "sp-in-ram", "kind": "core_register", "register": "sp",
     "op": "ge", "expect": "0x20000000"},

    // No active Cortex-M fault (CFSR/HFSR clean).
    {"id": "no-fault", "kind": "no_fault"},

    // PC is inside a symbol (reached a success marker / not in Error_Handler).
    {"id": "reached-loop", "kind": "stopped_at", "symbol": "main_loop"}
  ]
}
```

### Check kinds
| kind | fields | passes when |
|------|--------|-------------|
| `memory_u32` | `address`, `expect`, `op?`, `mask?` | `cmp((read_u32(address) & mask?), expect, op)` |
| `variable` | `name`, `expect`, `op?` | `cmp(read_variable(name), expect, op)` |
| `core_register` | `register`, `expect`, `op?`, `mask?` | `cmp((read_register(register) & mask?), expect, op)` |
| `no_fault` | — | `diagnose_fault_registers(read_fault_registers()).fault_classes == []` |
| `stopped_at` | `symbol` | `symbolize(read_register('pc')) == symbol` |

### Comparison ops
`eq` (default), `ne`, `lt`, `le`, `gt`, `ge`, `bits_set` (`a & e == e`), `bits_clear` (`a & e == 0`).
Integer `expect` accepts int or `"0x..."`/decimal string (`int(x, 0)`).

### Evaluation report (the output)
```jsonc
{
  "ok": true,                     // no failed AND no errored checks
  "results": [
    {"id": "...", "kind": "...", "status": "pass|fail|error",
     "expected": "...", "actual": "...", "detail": "..."}
  ],
  "stats": {"total": n, "passed": n, "failed": n, "errored": n}
}
```

---

## Tier 1 — Model + evaluator (pure, unit-tested)
### T1.1 `acceptance_model.py`
- `validate_acceptance_spec(spec)` → normalized spec (auto-fills `id`, defaults `op`),
  raising `ValueError` on: non-dict spec, non-list `checks`, unknown `kind`, missing
  required field, invalid `op`, duplicate `id`.
- `summarize_acceptance(spec)` → `{name, check_count, kinds}`.

### T1.2 `acceptance_eval.py`
- Reader protocol (duck-typed): `read_u32(addr)`, `read_variable(name)`, `read_register(name)`,
  `read_fault_registers()`, `symbolize(addr)`.
- `evaluate_acceptance(spec, reader)` → report. Per-check try/except → `status="error"`.
- `_compare(actual, expected, op)` + `_coerce_int`.
- Tests use a dict-backed `FakeReader` (house fakes style).

---

## Tier 2 — MCP tools + real reader
### T2.1 `GdbAcceptanceReader(gdb_client)`
- `read_u32` → `read_word`; `read_variable` → `-data-evaluate-expression` scalar; `read_register`
  → `read_register_value("$"+name)`; `read_fault_registers` → `read_fault_registers`;
  `symbolize` → `symbolize_pc`.

### T2.2 Tools + per-session persistence
- `_acceptance = {"current": None, "last_result": None}` global; add `"acceptance"` to
  `_SESSION_ATTRS` + `_DEFAULT_SESSION_GLOBALS`; `DebugSession.acceptance`; bind
  `session_acceptance` in `_dispatch_tool`.
- `load_acceptance` (`path|spec`) → validate + persist; returns summary.
- `run_acceptance` → evaluate loaded spec against the live session; store `last_result`;
  `suggested_next_actions` → on fail: re-run debug loop / inspect; on pass: framework/codegen next.
- `describe_acceptance` (`what=summary|checks|last_result`).
- Not in `_CORE_TOOLS` (reachable via `call` in compact mode), like the board tools.

---

## Cross-cutting
- Plain-dict models only (JSON-native) — no dataclasses.
- Keep `python -m ruff check .` and `python -m pytest -q` green after every task.
- Every tool populates `suggested_next_actions` toward the next pipeline stage.
- Update `CHANGELOG.md` (bilingual) once Tier 2 exposes the tools.

## Execution order
T1 → T2. Each capability committed independently.

## Status / 状态
- [x] **Tier 1** — `acceptance_model.py` + `acceptance_eval.py` + tests. *Done.*
- [x] **Tier 2** — `load/run/describe_acceptance` tools + real reader + per-session persistence + tests. *Done.*

## Deferred / 延后
- Spec authoring from natural-language product spec (spec text → AcceptanceSpec) — a later
  NLP/assist tier; B1 deliberately consumes the structured contract only.
- Peripheral-name → register-address resolution (e.g. `USART1->CR1` symbol) via the SVD /
  board model — a convenience layer on top of `memory_u32`.
- Sub-32-bit (`memory_u8`/`memory_u16`) and float/struct variable comparisons.
- Timing/waveform acceptance (needs external instruments — Pillar B2).
