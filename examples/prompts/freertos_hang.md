# FreeRTOS Hang Debug Prompt / FreeRTOS 卡死调试提示词

Use this MCP to diagnose a stopped or apparently hung FreeRTOS system.
使用这个 MCP 诊断已暂停或看起来卡死的 FreeRTOS 系统。

1. `debug_config(action=load, path=...)` → `start_debug_session` → `self_check` → `halt_execution`.
   / 加载配置、启动、自检并暂停目标。
2. `read_call_stack` (who is spinning) and `snapshot(scope=rtos)` for the task overview.
   / 用调用栈定位正在空转的位置，并采集 RTOS 任务总览。
3. `read_freertos(what=task_lists)` — distinguish a CPU-bound task from one blocked on time,
   suspension, or deletion. Then `read_freertos(what=queue|mutex, handle=...)` for the blockers,
   `read_freertos(what=heap)` for exhaustion.
   / 区分占满 CPU 的任务与因延时、挂起或删除而阻塞的任务，再检查队列、互斥量和堆。
4. Test hypotheses with `expressions(action=assert)` / `expressions(action=compare)`. For "where
   is it actually spinning?", `sample_pc` gives a symbolized hot-spot histogram (no SWO pin).
   / 用表达式断言/比较验证假设；需要定位实际热点时使用无需 SWO 引脚的 `sample_pc`。

Look for: a running high-priority task, tasks blocked on the same mutex, empty/full queues, heap
exhaustion, or a task stuck in a delay list longer than expected.
重点查:高优先级运行任务、阻塞在同一互斥量上的任务、空/满队列、堆耗尽、延时链表中停留过久的任务。
