# FreeRTOS Hang Debug Prompt / FreeRTOS 卡死调试提示词

English: Use this MCP to diagnose a stopped or apparently hung FreeRTOS system.

中文：使用这个 MCP 诊断已暂停或看起来卡死的 FreeRTOS 系统。

1. Load the debug config with `load_debug_config`. / 使用 `load_debug_config` 加载调试配置。
2. Halt the target. / 暂停目标。
3. Run `capture_debug_snapshot` with `include_project=true`, `include_rtos=true`, and `include_logs=true`. / 运行 `capture_debug_snapshot`，设置 `include_project=true`、`include_rtos=true` 和 `include_logs=true`。
4. Run `read_freertos_task_lists`. / 运行 `read_freertos_task_lists`。
5. Inspect queues or mutexes involved in the blocked tasks with `read_freertos_queue` or `read_freertos_mutex`. / 使用 `read_freertos_queue` 或 `read_freertos_mutex` 检查阻塞任务涉及的队列或互斥量。
6. Use `assert_expressions` or `compare_expressions_after_action` to test specific hypotheses. / 使用 `assert_expressions` 或 `compare_expressions_after_action` 验证具体假设。

English: Look for a running high-priority task, tasks blocked on the same mutex,
empty/full queues, heap exhaustion, or a task stuck in a delay list longer than expected.

中文：重点检查高优先级运行任务、阻塞在同一互斥量上的任务、空/满队列、堆耗尽，或停留在延时链表中过久的任务。
