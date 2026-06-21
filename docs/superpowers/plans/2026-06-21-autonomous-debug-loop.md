# Autonomous Debug Loop Implementation Plan / 自主调试闭环实现计划

> **For agentic workers:** Implement this plan task-by-task using TDD (test-first).
> Each capability is a focused module + server tool(s) + tests, committed independently,
> matching the existing `reset_strategy.py` / `hil_smoke.py` style.

**Goal / 目标:** Close the autonomous debug loop — *observe → orient (symbolize) →
hypothesize → act safely → verify* — so an engineer can hand a hardware bug to the AI
and walk away. The perception layer (flashing, breakpoints, typed memory, SVD decode,
FreeRTOS introspection, RTT/SWO/UART, fault-register decode, snapshots, experiments,
HIL smoke) already exists. This plan adds the missing control-loop, safety, and depth
primitives.

**Architecture / 架构:** Keep `server.py` as the thin MCP adapter. Put new behavior in
focused modules with unit tests using fakes. Hardware-facing paths stay gated behind
the existing `STM32_GDB_MCP_HIL=1` env. All new tool results use the existing
`content_success` / `content_error` JSON envelope and populate `suggested_next_actions`
so the model is guided through the loop.

**Tech Stack:** Python 3.10+, MCP Python SDK, pygdbmi (GDB/MI3), pytest, PyYAML.

---

## Tier 1 — Loop-closing primitives (without these the AI cannot close the loop)

### T1.1 Structured run-and-wait stop events
- Create `src/mcp_server/stop_event.py`: `parse_stop_event(mi_records) -> dict` returning
  `{reason, signal, frame: {func, file, line, addr}, breakpoint_id, stopped: bool}` from
  GDB/MI `*stopped` async records (`reason=breakpoint-hit|watchpoint-trigger|signal-received|
  end-stepping-range|exited|func-finished`).
- Add `GdbClientManager.run_and_wait(timeout_sec)`: issue `-exec-continue`, drain responses
  until a `*stopped` record or timeout, return parsed stop event (reason `timeout` if none).
- Server tools: `run_and_wait` (continue then report why it stopped), `wait_for_stop`
  (no resume; just collect the next stop event within a timeout).
- Tests: `tests/test_stop_event.py` (pure parser, several MI fixtures),
  `tests/test_server_tools.py` (tool exposure + timeout path with a fake client).

### T1.2 Source-level symbolization & frame navigation
- Add `GdbClientManager` methods: `select_frame(level)` (`-stack-select-frame`),
  `read_frame_variables(level)` (`-stack-list-variables --all-values`),
  `read_frame_arguments(level)` (`-stack-list-arguments`), `list_source(location, count)`
  (`list`), `resolve_address(expr)` (`info line *EXPR` / `info symbol`).
- Server tools: `select_frame`, `read_frame_variables`, `list_source`, `resolve_address`.
- Tests: `tests/test_server_tools.py` exposure + argument-passthrough with a fake client.

### T1.3 Conditional / temporary / command breakpoints
- Extend `GdbClientManager.set_breakpoint(location, condition=None, temporary=False,
  ignore_count=None)`: use `-break-insert -t -c "<cond>" -i <n> <location>`.
- Extend `set_breakpoint` tool schema with optional `condition`, `temporary`,
  `ignore_count` (keep `location` required, backward compatible).
- Tests: command-string construction unit test + tool schema test.

### T1.4 HardFault exception-frame reconstruction
- Create `src/mcp_server/exception_frame.py`: `reconstruct_exception_frame(lr, msp, psp,
  read_word)` — pick MSP/PSP from `EXC_RETURN` bit 2, decode the auto-stacked frame
  `{R0..R3, R12, LR, PC, xPSR}`, flag FPU-extended frame (bit 4), report stacked SP and
  whether alignment padding (xPSR bit 9) is present.
- Add `GdbClientManager` helpers to read `$lr`, `$msp`, `$psp` and a word at an address.
- Server tool `reconstruct_fault_context`: combine `diagnose_fault` + reconstructed frame
  + `resolve_address(stacked PC)` into one "crash site" report.
- Tests: `tests/test_exception_frame.py` (pure decoder, MSP/PSP selection, FPU frame),
  server exposure test.

---

## Tier 2 — Safety & robustness (the prerequisites to actually "let go")

### T2.1 Memory-write guardrails + audit log
- Create `src/mcp_server/memory_guard.py`: region allow/deny list (defaults deny option
  bytes, flash control, IWDG/WWDG key registers), `check_write(address, width) -> decision`,
  `dry_run` mode, and an append-only in-memory audit log of every mutating action.
- Wire `write_memory` / `write_typed_memory` handlers through the guard; add tools
  `set_write_policy`, `get_write_audit_log`.
- Tests: `tests/test_memory_guard.py` (deny/allow/dry-run/audit), handler integration test.

### T2.2 DBGMCU debug-freeze configuration
- Create `src/mcp_server/debug_freeze.py`: map common freeze targets (IWDG, WWDG, TIMx,
  RTC) to the right `DBGMCU_APB1_FZ/APB2_FZ` bits per family, build the register writes.
- Server tool `configure_debug_freeze` (freeze watchdogs/timers while halted) and surface
  freeze state in snapshots.
- Tests: `tests/test_debug_freeze.py` (bit/address resolution per family), exposure test.

### T2.3 Session health & auto-reconnect
- Add `GdbClientManager.is_alive()` / `GdbServerManager` liveness probe; server tool
  `check_session_health` returning `{gdb_alive, server_alive, target_responsive}` and an
  optional `reconnect=true` path that re-runs start_gdb/connect.
- Tests: health-report shape with fakes; reconnect path.

---

## Tier 3 — Depth that makes it genuinely useful

### T3.1 Symbol & type discovery
- `GdbClientManager`: `list_functions(regex)`, `list_variables(regex)`, `lookup_type(expr)`
  (`ptype`), `sizeof(expr)`, `address_of(symbol)`. Tools + tests.

### T3.2 Coredump capture / load
- Tools `capture_coredump(path)` (`gcore`-style dump of RAM + registers) and
  `load_coredump(path)` for offline postmortem. Tests with fake client.

### T3.3 Expose disassembly
- Register existing `GdbClientManager.disassemble_around_pc` as tool `disassemble`
  (params: location, instruction count). Test.

### T3.4 Execution-control completion
- `step_out` (`-exec-finish`), `step_instruction` (`-exec-next-instruction` /
  `-exec-step-instruction`), `run_to_line` (`-exec-until LOCATION`). Tools + tests.

### T3.5 Flash verify / readback
- `verify_flash(file_path)` via `-target-download`-then-compare or `compare-sections`.
  Tool + test.

### T3.6 DWT timing & PC sampling
- `read_cycle_counter` (DWT_CYCCNT @ 0xE0001004, enable via DEMCR/DWT_CTRL),
  `sample_pc(count, interval)` for hang/hot-spot localization. Tools + tests.

---

## Cross-cutting

- Every new tool populates `suggested_next_actions` with the natural next loop step
  (e.g. `reconstruct_fault_context` → `["list_source", "read_frame_variables"]`).
- Update `README.md` (Roadmap → Completed) and `CHANGELOG.md` per tier completed.
- Keep `python -m ruff check .` and `python -m pytest -q` green after every task.

---

## Execution order

Tier 1 (T1.1 → T1.4) first — highest ROI, each committed separately. Then Tier 2, then
Tier 3. Commit message per capability, mirroring existing history.

---

## Status / 状态

- [x] **Tier 1** — T1.1 run_and_wait, T1.2 frame navigation, T1.3 conditional breakpoints,
  T1.4 fault-context reconstruction. *Done, each committed.*
- [x] **Tier 2** — T2.1 write guardrails + audit, T2.2 DBGMCU debug-freeze, T2.3 session
  health/reconnect. *Done, each committed.*
- [x] **Tier 3** — execution control, disassemble, symbol/type discovery, coredump,
  verify_flash, DWT timing/PC sampling. *Done, committed.*

All work is covered by unit tests (pure modules with fakes) and server exposure/behavior
tests; hardware paths remain gated behind `STM32_GDB_MCP_HIL=1`. Total MCP tools: 83.
