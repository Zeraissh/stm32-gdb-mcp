# Tool map (by purpose)

Quick index of the stm32-gdb-mcp tools. Most results carry `suggested_next_actions`.

## Session & link
- `start_debug_session`, `stop_debug_session`, `recover_session`
- `self_check` (run right after connecting), `check_session_health`
- `get_timeouts`, `set_timeouts`

## Build & bring-up & flashing
- `build_firmware` (Keil UV4 / CMake / make / custom — Keil .axf debugs like a .elf)
- `flash_firmware`, `flash_and_run`, `verify_flash`, `reset_target`
- `inspect_project`, `set_debug_profile`, `get_debug_profile`
- `load_debug_config`, `save_debug_config`, `validate_debug_config`

## Execution control
- `continue_execution`, `halt_execution`, `run_and_wait`, `wait_for_stop`
- `step_over`, `step_into`, `step_out`, `step_instruction`, `run_to_line`
- `set_breakpoint` (condition/temporary/ignore_count), `delete_breakpoint`, `set_watchpoint`

## Observe state (core must be halted)
- `capture_state` (one-shot), `read_core_registers`, `read_call_stack`
- `select_frame`, `read_frame_variables`, `read_variable`, `list_source`, `resolve_address`
- `read_memory`, `write_memory` (guarded), `read_typed_memory`, `write_typed_memory`
- `read_peripheral_register`, `decode_peripheral_register`, `load_svd`
- `disassemble`, `list_functions`, `list_variables`, `lookup_type`, `sizeof`, `address_of`

## Fault & crash triage
- `reconstruct_fault_context` (recovers faulting PC -> source), `diagnose_fault`
- `read_fault_registers`, `capture_debug_snapshot`, `capture_coredump`, `load_coredump`

## RTOS (FreeRTOS)
- `detect_rtos`, `read_current_task`, `read_freertos_tasks`, `read_freertos_task_lists`
- `read_freertos_queue`, `read_freertos_mutex`, `read_freertos_heap`, `capture_rtos_snapshot`

## Logging & tracing
- `start_rtt_logging`/`get_rtt_logs`/..., `start_swo_logging`/..., `start_uart_logging`/...

## Timing
- `read_cycle_counter`, `sample_pc`, `configure_debug_freeze`

## Hypothesis & verify
- `debug_until` (trap + run + decoded context), `capture_expressions`,
  `assert_expressions`, `compare_expressions_after_action`
- `start_variable_tracking`, `stop_variable_tracking`, `get_tracked_data`, `set_watchpoint`

## Safety
- `set_write_policy`, `get_write_audit_log`

## Determinism & observability
- `run_scenario`, `get_session_journal`, `clear_session_journal`
- `get_session_timeline`, `get_session_metrics`, `export_debug_report`
