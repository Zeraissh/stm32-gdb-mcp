# Changelog / 更新日志

## Unreleased / 未发布

### Single-target excellence / 单端极致 (Phase 2)

- Comprehension layer: `read_core_registers`, `read_call_stack`, `read_frame_variables` now return decoded structured data + a one-line summary, with raw output opt-in via `include_raw`. / 理解层:核心读取工具返回解码后的结构化数据与一行摘要,原始输出通过 `include_raw` 可选。
- Minimal-step composites: `debug_until`, `capture_state`, `flash_and_run` collapse multi-step repro sequences into one call. / 最少步骤复合工具,将多步复现压缩为一次调用。
- Determinism: append-only session journal and declarative `run_scenario` replay (`get_session_journal`, `clear_session_journal`, `run_scenario`). / 确定性:仅追加会话日志与声明式 `run_scenario` 回放。
- Reliability: `self_check` (byte-order / Cortex-M core / device-family validation) and a structured error taxonomy with actionable next-actions. / 可靠性:`self_check` 链路自检与结构化错误分类。
- Observability: per-tool metrics (`get_session_metrics`), `get_session_timeline`, and run-id-correlated structured logging. / 可观测性:逐工具指标、会话时间线与按 run-id 关联的结构化日志。
- Reproducibility: `export_debug_report` bundles journal + metrics + profile (+ optional snapshot/coredump) into one run-id-keyed JSON artifact. / 可复现:`export_debug_report` 将日志+指标+profile 打包为单一工件。
- Reliability: retry/backoff for transient probe failures and `recover_session` to restart a dropped/wedged probe; centralized overridable timeouts (`get_timeouts`/`set_timeouts`). / 可靠性:瞬时探针失败的重试退避、`recover_session` 会话恢复、集中可覆盖的超时配置。
- Fixed a byte-order bug in 32-bit memory word reads, and stale first-read after reset, both found via HIL on STM32L431. / 修复 32 位内存字读取字节序 bug 与复位后首次读脏数据(均在 STM32L431 真机验证中发现)。

### Autonomous debug loop / 自主调试闭环

- Added `run_and_wait` / `wait_for_stop` structured stop events to close the observe loop. / 新增 `run_and_wait`/`wait_for_stop` 结构化停止事件以闭合观察环。
- Added source symbolization and frame navigation: `select_frame`, `read_frame_variables`, `list_source`, `resolve_address`. / 新增源码符号化和栈帧导航工具。
- Extended `set_breakpoint` with condition, temporary, and ignore_count. / 为 `set_breakpoint` 增加条件、临时和忽略计数选项。
- Added `reconstruct_fault_context` to unwind the stacked exception frame and recover the faulting PC's source line. / 新增 `reconstruct_fault_context`，展开异常压栈帧并还原出错 PC 的源码行。
- Added memory-write guardrails and audit log (`set_write_policy`, `get_write_audit_log`). / 新增内存写入护栏和审计日志。
- Added `configure_debug_freeze` to freeze IWDG/WWDG/timers via DBGMCU while halted. / 新增 `configure_debug_freeze`，halt 时通过 DBGMCU 冻结看门狗/定时器。
- Added `check_session_health` with optional reconnect for long autonomous runs. / 新增 `check_session_health` 及可选重连。
- Added Tier 3 depth tools: execution control (`step_out`, `step_instruction`, `run_to_line`), `disassemble`, symbol/type discovery, coredump capture/load, `verify_flash`, and DWT timing/PC sampling. / 新增第三梯队深度工具:执行控制、反汇编、符号/类型发现、coredump、flash 校验、DWT 计时与 PC 采样。

### Earlier in Unreleased / 早前未发布内容

- Added probe-specific reset strategy profiles and YAML reset config. / 新增面向不同调试器的复位策略 profile 和 YAML reset 配置。
- Migrated MCP tool responses to the stable JSON envelope. / 将 MCP 工具响应迁移到稳定 JSON 包络。
- Added SWO/ITM process-output log capture tools. / 新增 SWO/ITM 进程输出日志采集工具。
- Added skipped-by-default HIL smoke regression tests and STM32L431 OpenOCD config. / 新增默认跳过的 HIL 烟测回归测试和 STM32L431 OpenOCD 配置。
- Added a minimal STM32L431 example firmware project. / 新增最小 STM32L431 示例固件工程。

## 0.2.0 - 2026-06-21

- Added Cortex-M fault diagnosis and structured debug snapshots. / 新增 Cortex-M 故障诊断和结构化调试快照。
- Added SVD register bitfield decoding. / 新增 SVD 寄存器位域解码。
- Added FreeRTOS task, list, queue, mutex, and heap inspection. / 新增 FreeRTOS 任务、链表、队列、互斥量和堆检查。
- Added SEGGER RTT and UART log capture. / 新增 SEGGER RTT 和 UART 日志采集。
- Added automated debug experiment tools. / 新增自动化调试实验工具。
- Added YAML debug config load/save/validation. / 新增 YAML 调试配置加载、保存和校验。
- Added CI, examples, and repository hygiene files. / 新增 CI、示例和仓库维护文件。

## 0.1.0 - 2026-06-21

- Initial STM32 GDB MCP server prototype. / 初始 STM32 GDB MCP 服务器原型。
- Added debug server startup, GDB connection, flashing, reset, breakpoints, stepping, memory, variables, call stack, watchpoints, and basic SVD access. / 新增调试服务器启动、GDB 连接、烧录、复位、断点、单步、内存、变量、调用栈、观察点和基础 SVD 访问。
