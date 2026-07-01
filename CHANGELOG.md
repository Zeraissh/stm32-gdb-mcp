# Changelog / 更新日志

## Unreleased / 未发布

### Spec-to-silicon pipeline / 从规格到芯片流水线 (Pillar D Tier 3 — auto-derived acceptance)

- Added `synthesize_acceptance`: auto-derive a machine-checked **AcceptanceSpec** directly from the synthesized FrameworkPlan (Pillar D) and load it as the session's acceptance judge — so even the pass/fail judge is now machine-generated, welding design synthesis (D) to the acceptance judge (B1) and the bounded loop (C). It always emits a `no_fault` check (init must not HardFault — a target-independent ARM fact) and, for every clock the plan enables, a `memory_u32` `bits_set` check on the RCC enable bit, resolving each bit's placement from the session's loaded SVD or an explicit `register_map`. True to the deterministic layer, any clock whose RCC bit cannot be resolved is surfaced in `unresolved`, never guessed; the scope is deliberately bounded to `no_fault` + RCC clock-enable checks (peripheral-enable and GPIO-mode checks are deferred because their register layouts differ across families). / 新增 `synthesize_acceptance`：直接从已合成的 FrameworkPlan（Pillar D）自动推导机器可校验的 **AcceptanceSpec** 并载入为该会话的验收裁判——至此连“通过/失败”裁判也由机器生成，把设计合成（D）、验收裁判（B1）与有界闭环（C）焊接为一体。它始终生成一个 `no_fault` 断言（初始化不得触发 HardFault——与目标无关的 ARM 事实），并为计划使能的每个时钟生成一个针对 RCC 使能位的 `memory_u32` `bits_set` 断言，其位偏移从会话已加载的 SVD 或显式 `register_map` 解析。恪守确定性层原则：任何无法解析 RCC 位的时钟都列入 `unresolved` 而非臆测；范围刻意限定为 `no_fault` + RCC 时钟使能断言（外设使能与 GPIO 模式断言因各族系寄存器布局差异过大而暂缓）。

### Spec-to-silicon pipeline / 从规格到芯片流水线 (Pillar D — design synthesis)

- Added `design_framework`, `describe_framework`, and `render_framework`: synthesize a deterministic **FrameworkPlan** from the imported netlist board model (Pillar A) plus an optional per-peripheral design config, then render it to a HAL C init skeleton (`bsp_init.c` / `bsp_init.h`). The solver derives which clocks to enable, how each pin must be muxed (AF push-pull / open-drain / analog, pull, speed), and which peripheral init blocks to emit — in dependency order (clocks → GPIO → peripherals). Everything derivable from the board alone is exact; a value that needs target data (a GPIO alternate-function number) or a human decision (a baud rate) is surfaced in `unresolved` and rendered as a clearly marked `TODO`, never guessed. This closes the last hand-written link in the pipeline — 网表图 + 产品规格 → 框架设计 + 代码编写 is now machine-generated scaffolding the agent completes, flashes, and verifies via the acceptance loop (Pillar C). / 新增 `design_framework` / `describe_framework` / `render_framework`：从已导入的网表板级模型（Pillar A）及可选的逐外设设计配置合成确定性的 **FrameworkPlan**，再渲染为 HAL C 初始化骨架（`bsp_init.c` / `bsp_init.h`）。求解器推导需使能哪些时钟、每个引脚如何复用（复用推挽/开漏/模拟、上下拉、速度）、以及要生成哪些外设初始化块——并按依赖顺序（时钟 → GPIO → 外设）排列。凡从板级模型可推导的均为精确值；需要目标数据（GPIO 复用功能号）或人为决策（波特率）的值则列入 `unresolved` 并渲染为明确的 `TODO`，绝不臆测。至此流水线最后一个靠手写的环节打通——“网表图 + 产品规格 → 框架设计 + 代码编写”现为机器生成的脚手架，由 agent 补全、烧录，并经验收闭环（Pillar C）验证。

### Spec-to-silicon pipeline / 从规格到芯片流水线 (Pillar C — bounded acceptance loop)

- Added `start_acceptance_loop`, `run_acceptance_iteration`, and `acceptance_loop_status`: a **bounded, agent-driven** closed loop that ties the netlist board model (Pillar A) and the AcceptanceSpec judge (Pillar B1) together. Each iteration does one deterministic *build → flash → run-to-state → evaluate* pass and returns an objective decision (`converged` / `should_continue` / `exhausted` / `stalled`) plus the exact checks still to fix. The machine owns the mechanics and the bounds — it stops on convergence, on `max_iterations`, or when the same checks keep failing (`stall_patience`) — while the agent supplies only the creative step (the code fix) between iterations. Build or run-to failures are recorded as a `phase_error`, never a crash; a terminal loop refuses to re-run unless `force=true`. This closes the full spec-to-silicon loop: 网表图 + 产品规格 → 框架/代码 → 调试验证 → 不过则继续改代码. / 新增 `start_acceptance_loop` / `run_acceptance_iteration` / `acceptance_loop_status`：一个**有界的、由 agent 驱动**的闭环，把网表板级模型（Pillar A）与 AcceptanceSpec 裁判（Pillar B1）串起来。每次迭代执行一轮确定性的*编译 → 烧录 → 运行到指定状态 → 求值*，并返回客观决策（`converged` / `should_continue` / `exhausted` / `stalled`）及仍需修复的具体断言。机器掌控机制与边界——收敛、达到 `max_iterations`、或同一批断言反复失败（`stall_patience`）时停止——而 agent 只在迭代间负责创造性的一步（改代码）。编译或运行失败记为 `phase_error`，而非崩溃；已终止的回路除非 `force=true` 否则拒绝重跑。至此完整的“从规格到芯片”闭环打通。

### Spec-to-silicon pipeline / 从规格到芯片流水线 (Pillar B1 — acceptance)

- Added `load_acceptance`, `run_acceptance`, and `describe_acceptance`: turn a product spec into a machine-checked **AcceptanceSpec** (deterministic checks — `memory_u32` for any memory-mapped register, `variable` for a C global, `core_register`, `no_fault`, and `stopped_at`) and evaluate it against live silicon, returning a per-check pass/fail/error verdict. This is the closed-loop *judge* that lets an agent decide “verification failed → keep fixing” objectively; an unreadable target is reported as `error`, never a silent pass. / 新增 `load_acceptance` / `run_acceptance` / `describe_acceptance`：将产品规格转为机器可校验的 **AcceptanceSpec**（确定性断言——`memory_u32` 适用于任意内存映射寄存器、`variable` 读 C 全局变量、`core_register`、`no_fault`、`stopped_at`）并对真实芯片状态求值，逐项返回 通过/失败/错误 裁决。这是闭环的“裁判”，让 agent 客观地判定“验证不过→继续修改”；无法读取的目标报为 `error`，绝不静默通过。

### Spec-to-silicon pipeline / 从规格到芯片流水线 (Pillar A)

- Added `import_netlist` and `describe_board`: parse a schematic netlist (KiCad `.net`) into a machine-readable BoardDescription — MCU part/family/line, a per-pin map (package pin → port pin → net → inferred peripheral function), and power/ground nets — the input contract for automated framework design. / 新增 `import_netlist` 与 `describe_board`：将原理图网表（KiCad `.net`）解析为机器可读的 BoardDescription——MCU 型号/族系/产品线、逐脚映射（封装引脚 → 端口引脚 → 网络 → 推断的外设功能）以及电源/地网络——作为自动框架设计的输入契约。
- Added `validate_board`: check a BoardDescription for structural faults — a package pin wired to multiple nets (short), a peripheral signal routed to multiple pins, a port pin driven by multiple nets — plus missing power/ground/debug/reset nets, and (with an optional CubeMX-derived pin-capability DB via `db_path`/`STM32_GDB_MCP_PIN_DB`) alternate-function legality; unknown pins degrade to `unverified` rather than a false conflict. / 新增 `validate_board`：检测 BoardDescription 的结构性错误——同一封装引脚接到多个网络（短路）、同一外设信号布到多个引脚、同一端口引脚被多个网络驱动——以及缺失的电源/地/调试/复位网络，并在提供 CubeMX 引脚能力库（`db_path`/`STM32_GDB_MCP_PIN_DB`）时校验复用功能合法性；未知引脚降级为`unverified` 而非误报冲突。

### Toolchain & robustness / 工具链与健壮性

- Added `build_firmware`: build with Keil uVision (UV4), CMake, make, or a custom command; Keil `.axf` (ELF/DWARF) debugs through the existing tools like a `.elf`. / 新增 `build_firmware`,支持 Keil uVision(UV4)、CMake、make 或自定义命令构建;Keil 的 `.axf` 与 `.elf` 一样可被现有工具调试。
- `start_debug_session` now rejects openocd with empty `server_args` up front (clear guidance), and openocd config errors are classified as non-retryable `invalid_server_args` instead of a misleading `probe_unavailable`. / `start_debug_session` 现在对 openocd 缺少 `server_args` 提前给出清晰报错,openocd 配置错误被正确分类为不可重试的 `invalid_server_args`。

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
