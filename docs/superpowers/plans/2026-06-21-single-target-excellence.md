# Phase 2 — Single-Target Excellence / 单端极致

> **For agentic workers:** Implement task-by-task with TDD. Each capability is a focused
> module + tool wiring + tests, committed independently. Pure decoders/builders are
> unit-tested with fixtures; hardware paths stay gated behind `STM32_GDB_MCP_HIL=1`.

**Scope decision / 范围决策:** Deliberately *defer* concurrency / multi-target isolation,
productionization, service-ization, security, and integration. First make the **single
target** excellent. Priority order: **determinism & reproducibility → reliability &
correctness → observability.**

**Two cross-cutting design laws / 两条贯穿铁律:**
1. **Low comprehension overhead / 低理解开销** — every probe result is delivered to the
   user/agent decoded, concise, precise, and unambiguous. No raw GDB/MI dumps as the
   primary signal; structured fields + a one-line `summary`; raw is opt-in.
2. **Minimal repro steps / 最少复现步骤** — reproducing a complex logic bug should take
   as few tool round-trips as possible: composite operations and declarative,
   replayable scenarios instead of long manual sequences.

---

## Workstream A — Comprehension layer (理解性能) [cross-cutting, do first]

Goal: turn raw GDB output into clean structured data with summaries.

- **A1** `gdb_decode.py`: pure decoders for register values, backtrace frames, and frame
  variables from pygdbmi records.
- **A2** Decoded `read_core_registers` via `-data-list-register-names` +
  `-data-list-register-values x` → `{name: hex}` + `summary` (PC/LR/SP).
- **A3** Decoded `read_call_stack` → clean frame list `{level, func, file, line, addr}`.
- **A4** Decoded `read_frame_variables` → `{name: value}` map + arg/local split.
- **A5** Envelope: add an optional `summary` field and make bulky `raw_response` opt-in
  (e.g. `include_raw`) for token economy, without breaking the existing contract.

## Workstream B — Minimal-step composites (最少步骤复现) [cross-cutting]

- **B1** `debug_until`: set an optional conditional/temporary breakpoint, run, and return
  the **fully decoded stop context** (stop reason + frame + locals + nearby source) in a
  single call.
- **B2** `flash_and_run` / `reset_to_main`: one call to get from an ELF to "halted at a
  known entry point".
- **B3** `capture_state` one-shot: decoded registers + backtrace + top-frame locals +
  fault summary in one call (the "where am I / what happened" button).

## Workstream C — Determinism & reproducibility (确定性/可复现) [priority 1]

- **C1** Session journal: record every tool call (timestamp, name, args, result digest,
  monotonic run sequence) in an append-only, exportable log.
- **C2** Declarative **scenario record & replay**: a saved sequence of steps
  (reset → flash → breakpoint → run → assert) that replays deterministically and reports
  pass/fail with captured state. Serves both determinism and minimal-steps.
- **C3** Centralized, explicit, **recorded timeouts** so a replay behaves identically;
  remove hardcoded per-call magic numbers.
- **C4** `export_debug_report`: bundle journal + final snapshot (+ optional coredump) tied
  to a run-id, so a bug is fully reproducible/shareable from one artifact.

## Workstream D — Reliability & correctness (可靠性/正确性) [priority 2]

- **D1** `self_check`: on connect, read known constants (CPUID, DBGMCU IDCODE), validate
  byte order/endianness, target-family match, and probe health — catch environment/config
  faults *before* the agent wastes steps. (Directly motivated by the HIL byte-order bug.)
- **D2** Precondition guard: detect "core is running" and return a clear structured error
  (`target_running`, suggest `halt_execution`) instead of a raw GDB timeout — both a
  reliability and a comprehension fix. (We hit this exact footgun on hardware.)
- **D3** Configurable retry/backoff on flaky ops + a structured error taxonomy
  (`retryable` / `fatal` / `precondition`) the agent can branch on.

## Workstream E — Observability (可观测性) [priority 3]

- **E1** Structured logging with levels and run-id correlation.
- **E2** Per-tool metrics: timing, success/failure, retry counts.
- **E3** `get_session_timeline`: human/agent-readable replay of what happened this session
  (built on the C1 journal).

---

## Execution order

A (comprehension) and B (composites) first — they directly serve the two cross-cutting
laws the user prioritized. C1/C2 (journal + scenario replay) anchor determinism and feed
E3. D1/D2 land early because the HIL session proved their necessity. Commit per capability.

---

## Status / 状态

- [x] **A — comprehension layer**: decoded registers/backtrace/variables + summaries,
  raw opt-in (`gdb_decode.py`). HIL-checked on L431.
- [x] **B — minimal-step composites**: `debug_until`, `capture_state`, `flash_and_run`
  (`composites.py`). `capture_state` HIL-checked.
- [x] **C — determinism**: session journal + replayable `run_scenario`
  (`session_journal.py`, `scenario.py`). HIL-checked (4-step scenario replayed 4/4).
- [x] **D — reliability**: `self_check` (byte-order/core/family) + structured
  `error_taxonomy` wired into the dispatcher (`self_check.py`, `error_taxonomy.py`).
  Unit-validated incl. the byte-reversed-CPUID case; HIL re-check pending a probe replug.
- [x] **E — observability**: per-tool metrics, `get_session_timeline`, run-id stderr
  logging (`metrics.py`).

Deferred by decision: C3 centralized-timeout refactor, C4 `export_debug_report`,
D3 retry/backoff + probe-reset recovery — fold into a follow-up. Total MCP tools: 92.

Lesson from the HIL session: hard-killing OpenOCD wedges the ST-Link USB endpoint
(`Error: open failed`) until a physical replug — concrete motivation for D3 (graceful
probe reset/recovery) before any multi-target work.
