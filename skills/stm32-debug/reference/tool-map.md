# Tool map by purpose / 按用途分类的工具索引

Most results include `suggested_next_actions`. Related operations use an `action=` or
`what=` discriminator. Hidden tools remain callable through `call`; query their complete
schema with `tool_help`.

大多数结果包含 `suggested_next_actions`。相关操作通过 `action=` 或 `what=` 合并成工具族。
隐藏工具仍可由 `call` 调用，并可用 `tool_help` 查询完整 schema。

## Session and link / 会话与连接

- `start_debug_session`, `stop_debug_session`, `recover_session`
- `list_sessions`, `close_session`; pass `session="name"` for multiple boards /
  多板卡时向任意工具传 `session="name"`
- `self_check`: run immediately after connecting / 连接后立即运行
- `session_diagnostics(what=health|events|server_logs)`
- `timeouts(action=get|set)`

## Build, bring-up, and flash / 构建、启动与烧录

- `build_firmware`: Keil UV4, CMake, make, or custom commands / 支持 Keil、CMake、make 和自定义命令
- `flash_firmware`, `flash_and_run`, `verify_flash`, `reset_target`
- `inspect_project`, `debug_profile(action=get|set)`
- `debug_config(action=load|save|validate)`
- `detect_probe`: physical USB probe inventory with serials; never guesses among multiple probes / 读取带序列号的物理 USB 探针；多探针时不猜测
- `suggest_server_args`: validated backend arguments / 生成已验证的 GDB Server 参数

## Execution control / 执行控制

- `continue_execution`, `halt_execution`, `run_and_wait`, `wait_for_stop`
- `run_for_duration`: natural run, optional low-rate sample, then halt/capture /
  自然运行，可选低速采样，结束后暂停并采集
- `step(kind=over|into|out|instruction)`, `run_to_line`
- `breakpoint(action=set|delete|list|watch)`

## State inspection (halted core) / 状态检查（内核需暂停）

- `capture_state`, `read_call_stack`, `read_variable`
- `read_registers(what=core|fault|cycle)`
- `frame(action=select|source|variables)`
- `read_memory`, guarded `write_memory`, `typed_memory(action=read|write)`
- `load_svd`, `read_peripheral_register`, `decode_peripheral_register`
- `disassemble`, `inspect_symbol(what=size|type|address|resolve|functions|variables)`

## Fault and crash triage / 故障与崩溃诊断

- `reconstruct_fault_context`: recover the true faulting PC and source /
  恢复真实故障 PC 与源码位置
- `diagnose_fault`
- `analyze_stack`: used/free/overflow verdict / 栈使用量、余量与越界结论
- `read_registers(what=fault)`, `snapshot(scope=full|rtos)`
- `coredump(action=capture|load)`

## RTOS (FreeRTOS) / 实时系统（FreeRTOS）

- `detect_rtos`, `snapshot(scope=rtos)`
- `read_freertos(what=current_task|tasks|task_lists|queue|mutex|heap)`

## Logging and tracing / 日志与追踪

- `logging(action=start|stop|get|clear, channel=rtt|swo|uart)`
- `setup_swo(hclk_hz, swo_hz)` configures TPIU+ITM from the debugger; then
  `logging(action=start, channel="swo", file=<output>)` tails OpenOCD's ITM decode.
  The SWO pin must be wired. /
  `setup_swo` 由调试器配置 TPIU+ITM，随后用 `logging` 读取 OpenOCD 解码输出；板卡必须连接 SWO 引脚。

## Timing and profiling / 时序与性能分析

- `read_registers(what=cycle)`: DWT cycle counter / DWT 周期计数器
- `sample_pc`: non-intrusive symbolized PC histogram over SWD / 基于 SWD 的非侵入式符号化 PC 直方图
- `configure_debug_freeze`

## Hypothesis and verification / 假设与验证

- `debug_until`: condition trap, run, and decoded context / 条件陷阱、运行和解码上下文
- `run_for_duration`
- `expressions(action=capture|assert|compare)`
- `track_variable(action=start|stop|get)`
- `breakpoint(action=watch)`

`run_for_duration(sample={"interval_ms": 500, "expressions": [...]})` uses best-effort
debugger polling. It is not high-speed trace; use SWO or a firmware ring buffer when timing
must be preserved or capture rate guaranteed.

`run_for_duration(..., sample=...)` 是尽力而为的调试器轮询，并非高速追踪。需要保持严格时序或
保证采样率时，请使用 SWO 或固件环形缓冲。

## Safety / 安全

- `write_guard(action=policy|audit)`
- Flashing, reset-strategy changes, Debug Authentication, and target security changes
  remain explicit operations. /
  烧录、复位策略、Debug Authentication 和目标安全状态修改必须显式执行。

## Determinism and observability / 可复现性与可观测性

- `run_scenario`, `batch`
- `get_session(view=journal|timeline|metrics)`, `clear_session_journal`
- `export_debug_report`, `report_issue`
- `tool_help(name=...|query=...)`, `call(tool=..., args=...)`
