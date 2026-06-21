# HardFault Debug Prompt / HardFault 调试提示词

English: Use this MCP to diagnose a Cortex-M HardFault.

中文：使用这个 MCP 诊断 Cortex-M HardFault。

1. Load the project config with `load_debug_config`. / 使用 `load_debug_config` 加载项目配置。
2. Start the debug session and flash the firmware if needed. / 启动调试会话，必要时烧录固件。
3. Reset and halt the target. / 复位并暂停目标。
4. Run `capture_debug_snapshot` with `include_project=true`, `include_rtos=true`, and `include_logs=true`. / 运行 `capture_debug_snapshot`，设置 `include_project=true`、`include_rtos=true` 和 `include_logs=true`。
5. Run `diagnose_fault`. / 运行 `diagnose_fault`。
6. Explain the likely root cause using CFSR/HFSR flags, BFAR/MMFAR, PC/LR/SP, call stack, and recent logs. / 结合 CFSR/HFSR 标志、BFAR/MMFAR、PC/LR/SP、调用栈和近期日志解释可能根因。
7. Suggest the next three concrete GDB checks. / 给出接下来三个具体 GDB 检查动作。

Prefer structured evidence over guesses.

优先使用结构化证据，不要凭空猜测。
