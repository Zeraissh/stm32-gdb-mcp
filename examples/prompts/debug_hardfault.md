# HardFault Debug Prompt

Use this MCP to diagnose a Cortex-M HardFault.

1. Load the project config with `load_debug_config`.
2. Start the debug session and flash the firmware if needed.
3. Reset and halt the target.
4. Run `capture_debug_snapshot` with `include_project=true`, `include_rtos=true`, and `include_logs=true`.
5. Run `diagnose_fault`.
6. Explain the likely root cause using CFSR/HFSR flags, BFAR/MMFAR, PC/LR/SP, call stack, and recent logs.
7. Suggest the next three concrete GDB checks.

Prefer structured evidence over guesses.
