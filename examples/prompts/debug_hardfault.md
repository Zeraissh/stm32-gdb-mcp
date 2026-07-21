# HardFault Debug Prompt / HardFault 调试提示词

Use this MCP to diagnose a Cortex-M HardFault. Prefer structured evidence over guesses.
使用这个 MCP 诊断 Cortex-M HardFault。优先用结构化证据,不要凭空猜测。

1. `debug_config(action=load, path=...)` → `start_debug_session` → `self_check`.
   / 加载配置、启动会话，并立即执行身份自检。
2. Get to the fault: catch `HardFault_Handler` (`breakpoint(action=set)` + `run_and_wait`),
   or if already faulted, `halt_execution`.
   / 捕获 `HardFault_Handler`；若目标已经故障，则直接暂停。
3. `reconstruct_fault_context` — unwinds the stacked exception frame to the **faulting PC →
   file:line**, and decodes CFSR/HFSR. This is the key step.
   / 重建异常栈帧，将真实故障 PC 解析到源码行，并解码 CFSR/HFSR；这是关键步骤。
4. `snapshot(scope=full)` with `include_project=true`, `include_rtos=true`, `include_logs=true`
   for the full evidence bundle (registers, stack, BFAR/MMFAR, recent logs).
   / 采集寄存器、栈、BFAR/MMFAR、RTOS、项目和近期日志的完整证据包。
5. Explain the root cause from CFSR/HFSR flags, BFAR/MMFAR, PC/LR/SP, the call stack, and logs;
   then give the next three concrete checks.
   / 根据上述证据解释根因，并给出接下来三个可执行检查，不要凭猜测下结论。
