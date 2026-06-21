# FreeRTOS Hang Debug Prompt

Use this MCP to diagnose a stopped or apparently hung FreeRTOS system.

1. Load the debug config with `load_debug_config`.
2. Halt the target.
3. Run `capture_debug_snapshot` with `include_project=true`, `include_rtos=true`, and `include_logs=true`.
4. Run `read_freertos_task_lists`.
5. Inspect queues or mutexes involved in the blocked tasks with `read_freertos_queue` or `read_freertos_mutex`.
6. Use `assert_expressions` or `compare_expressions_after_action` to test specific hypotheses.

Look for a running high-priority task, tasks blocked on the same mutex, empty/full queues, heap exhaustion, or a task stuck in a delay list longer than expected.
