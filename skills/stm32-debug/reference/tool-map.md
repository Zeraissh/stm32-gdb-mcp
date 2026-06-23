# Tool map (by purpose)

Quick index of the stm32-gdb-mcp tools. Most results carry `suggested_next_actions`.
The surface is lean: related ops are **action-dispatched families** — pass the discriminator
(`action=…` or `what=…`). The old standalone names still work if you call them.

## Session & link
- `start_debug_session` (pass `session="name"` for multi-board, `serial=` to pick a probe),
  `stop_debug_session`, `recover_session`, `list_sessions`, `close_session`
- `self_check` (run right after connecting)
- `session_diagnostics` (what = health | events | server_logs)
- `timeouts` (action = get | set)

## Build & bring-up & flashing
- `build_firmware` (Keil UV4 / CMake / make / custom — Keil .axf debugs like a .elf)
- `flash_firmware`, `flash_and_run`, `verify_flash`, `reset_target`
- `inspect_project`, `debug_profile` (action = get | set)
- `debug_config` (action = load | save | validate)

## Execution control
- `continue_execution`, `halt_execution`, `run_and_wait`, `wait_for_stop`
- `step` (kind = over | into | out | instruction), `run_to_line`
- `breakpoint` (action = set | delete | list | watch; set takes condition/temporary/ignore_count)

## Observe state (core must be halted)
- `capture_state` (one-shot), `read_call_stack`, `read_variable`
- `read_registers` (what = core | fault | cycle)
- `frame` (action = select | source | variables)
- `read_memory`, `write_memory` (guarded), `typed_memory` (action = read | write)
- `read_peripheral_register`, `decode_peripheral_register`, `load_svd`
- `disassemble`, `inspect_symbol` (what = size | type | address | resolve | functions | variables)

## Fault & crash triage
- `reconstruct_fault_context` (recovers faulting PC -> source), `diagnose_fault`
- `analyze_stack` (used/free/overflow verdict — the key tool for stack overflows)
- `read_registers` (what = fault), `snapshot` (scope = full | rtos), `coredump` (action = capture | load)

## RTOS (FreeRTOS)
- `detect_rtos`, `snapshot` (scope = rtos)
- `read_freertos` (what = current_task | tasks | task_lists | queue | mutex | heap)

## Logging & tracing
- `logging` (action = start | stop | get | clear; channel = rtt | swo | uart)

## Timing
- `read_registers` (what = cycle), `sample_pc`, `configure_debug_freeze`

## Hypothesis & verify
- `debug_until` (trap + run + decoded context)
- `expressions` (action = capture | assert | compare)
- `track_variable` (action = start | stop | get), `breakpoint` (action = watch)

## Safety
- `write_guard` (action = policy | audit)

## Determinism & observability
- `run_scenario`, `batch`, `get_session` (view = journal | timeline | metrics), `clear_session_journal`
- `export_debug_report`, `report_issue` (file a GitHub issue with the journal)
