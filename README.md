# STM32 GDB MCP Server / STM32 GDB MCP 服务器

English: This project is a Model Context Protocol (MCP) server that lets an AI
client drive STM32 debugging through GDB. It can start a supported GDB server,
connect `arm-none-eabi-gdb`, flash firmware, control execution, inspect target
state, and collect structured evidence for fault analysis.

中文：本项目是一个面向 STM32 调试的 Model Context Protocol (MCP) 服务器，让
AI 客户端可以通过 GDB 接管调试流程。它可以启动受支持的 GDB Server、连接
`arm-none-eabi-gdb`、烧录固件、控制执行、检查目标状态，并为故障分析采集结构化证据。

## Requirements / 环境要求

English: The server expects these tools to be available on `PATH`.

中文：服务器期望以下工具已经加入 `PATH`。

1. `arm-none-eabi-gdb` from the Arm GNU Toolchain / 来自 Arm GNU Toolchain 的 `arm-none-eabi-gdb`
2. `openocd` for OpenOCD targets / 用于 OpenOCD 目标的 `openocd`
3. `JLinkGDBServerCL` for J-Link targets / 用于 J-Link 目标的 `JLinkGDBServerCL`
4. `st-util` for ST-Link targets / 用于 ST-Link 目标的 `st-util`

Run the environment check / 运行环境检查：

```bash
python setup_env.py
```

## Install / 安装

```bash
pip install -e .
```

For development and tests / 开发与测试：

```bash
pip install -e ".[dev]"
python -m ruff check .
python -m pytest
python -m compileall src tests
python -m build
```

Project maintenance guides / 项目维护指南：

- `CONTRIBUTING.md`: development workflow, quality gate, and bug evidence / 开发流程、质量门禁和 Bug 证据要求
- `SECURITY.md`: private vulnerability reporting and sensitive debug data / 私密漏洞报告和敏感调试数据处理
- `docs/hil-validation.md`: self-hosted hardware-in-the-loop validation / 自托管硬件在环验证
- `docs/release.md`: release checklist / 发布检查清单

## Run / 运行

```bash
stm32-gdb-mcp
```

English: Configure your MCP client to launch that command as a stdio MCP server.

中文：在 MCP 客户端中配置上述命令，将它作为 stdio MCP 服务器启动。

Example MCP server entry / MCP 服务器配置示例：

```json
{
  "mcpServers": {
    "stm32-gdb-mcp": {
      "command": "stm32-gdb-mcp"
    }
  }
}
```

## Core Workflow / 核心流程

Typical OpenOCD session / 典型 OpenOCD 会话：

1. Use `set_debug_profile` with MCU, board, probe, `server_args`, `elf_path`, and `svd_path`.
   使用 `set_debug_profile` 设置 MCU、开发板、调试器、`server_args`、`elf_path` 和 `svd_path`。
2. Use `start_debug_session` with `server_type="openocd"` and OpenOCD config args.
   使用 `start_debug_session`，传入 `server_type="openocd"` 和 OpenOCD 配置参数。
3. Use `flash_firmware` with the ELF path.
   使用 `flash_firmware` 和 ELF 路径烧录固件。
4. Use `reset_target` with `halt=true`.
   使用 `reset_target` 并设置 `halt=true` 复位并暂停目标。
5. Use breakpoints, stepping, variables, memory, peripheral registers, and snapshots to debug.
   使用断点、单步、变量、内存、外设寄存器和快照完成调试。

## Tool Groups / 工具分组

Session and flashing / 会话与烧录：

- `start_debug_session`
- `stop_debug_session`
- `flash_firmware`
- `reset_target`
- `set_debug_profile`
- `get_debug_profile`
- `load_debug_config`
- `save_debug_config`
- `validate_debug_config`
- `inspect_project`

Execution control / 执行控制：

- `set_breakpoint`
- `delete_breakpoint`
- `continue_execution`
- `halt_execution`
- `step_over`
- `step_into`
- `set_watchpoint`

Inspection / 状态检查：

- `read_variable`
- `read_memory`
- `write_memory`
- `read_typed_memory`
- `write_typed_memory`
- `read_call_stack`
- `read_core_registers`
- `get_gdb_events`
- `get_gdb_server_logs`

STM32 fault and evidence capture / STM32 故障与证据采集：

- `read_fault_registers`
- `diagnose_fault`
- `capture_debug_snapshot`

FreeRTOS runtime inspection / FreeRTOS 运行时检查：

- `detect_rtos`
- `read_current_task`
- `read_freertos_tasks`
- `read_freertos_task_lists`
- `read_freertos_queue`
- `read_freertos_mutex`
- `read_freertos_heap`
- `capture_rtos_snapshot`

RTT log capture / RTT 日志采集：

- `start_rtt_logging`
- `stop_rtt_logging`
- `get_rtt_logs`
- `clear_rtt_logs`

SWO/ITM log capture / SWO/ITM 日志采集：

- `start_swo_logging`
- `stop_swo_logging`
- `get_swo_logs`
- `clear_swo_logs`

UART log capture / UART 日志采集：

- `start_uart_logging`
- `stop_uart_logging`
- `get_uart_logs`
- `clear_uart_logs`

Automated debug experiments / 自动化调试实验：

- `capture_expressions`
- `assert_expressions`
- `compare_expressions_after_action`

SVD-aware peripheral inspection / 基于 SVD 的外设检查：

- `load_svd`
- `read_peripheral_register`
- `decode_peripheral_register`

Variable polling / 变量轮询：

- `start_variable_tracking`
- `stop_variable_tracking`
- `get_tracked_data`

## Fault Diagnosis / 故障诊断

English: `diagnose_fault` reads Cortex-M SCB fault registers and decodes CFSR/HFSR
into active fault classes and flags such as `PRECISERR`, `BFARVALID`,
`UNALIGNED`, and `DIVBYZERO`. `capture_debug_snapshot` collects a broader bundle:

中文：`diagnose_fault` 读取 Cortex-M SCB 故障寄存器，并将 CFSR/HFSR 解码为当前
故障类别和标志位，例如 `PRECISERR`、`BFARVALID`、`UNALIGNED` 和 `DIVBYZERO`。
`capture_debug_snapshot` 会采集更完整的证据包：

- core registers / 内核寄存器
- fault registers and decoded diagnosis / 故障寄存器和解码后的诊断结果
- call stack / 调用栈
- disassembly around `$pc` / `$pc` 附近反汇编
- pending GDB events / 待处理 GDB 事件
- captured GDB server logs / 已捕获的 GDB Server 日志

English: This gives an AI client a single evidence packet for HardFault, BusFault,
UsageFault, and MemManage investigations.

中文：这样 AI 客户端可以用一个证据包分析 HardFault、BusFault、UsageFault 和
MemManage 等问题。

## SVD Decoding / SVD 解码

English: `decode_peripheral_register` reads a register by SVD name and decodes bitfields.

中文：`decode_peripheral_register` 按 SVD 名称读取寄存器并解码位域。

The parser supports / 解析器支持：

- `bitOffset` + `bitWidth`
- `bitRange`
- `lsb` + `msb`
- same-peripheral register `derivedFrom` / 同一外设内寄存器的 `derivedFrom`
- `enumeratedValues`

## Debug Config Files / 调试配置文件

English: Use YAML config files to make sessions repeatable across AI clients and
projects. The example at `examples/configs/stm32f4_jlink.yaml` shows the
supported shape.

中文：使用 YAML 配置文件可以让不同 AI 客户端和项目之间的调试会话可复现。
`examples/configs/stm32f4_jlink.yaml` 展示了支持的配置结构。

```yaml
mcu: STM32F407VG
probe: jlink
server_type: jlink
server_args:
  - -device
  - STM32F407VG
elf_path: build/app.elf
svd_path: STM32F407.svd
rtt:
  command: JLinkRTTClient
uart:
  port: COM7
  baudrate: 115200
```

English: `load_debug_config` loads YAML and applies compatible fields to the
active debug profile. `save_debug_config` writes a YAML file.
`validate_debug_config` checks server type, path fields, RTT args, and UART
settings without touching the current session.

中文：`load_debug_config` 加载 YAML，并将兼容字段应用到当前调试 profile。
`save_debug_config` 写出 YAML 文件。`validate_debug_config` 会检查 server type、
路径字段、RTT 参数和 UART 设置，但不会改动当前会话。

## Response Shape / 响应结构

English: MCP tool results use a stable JSON envelope inside the MCP `TextContent`
transport.

中文：MCP 工具结果在 MCP `TextContent` 传输外壳内使用稳定 JSON 响应包络。

```json
{
  "ok": true,
  "data": {},
  "error": null,
  "raw_response": null,
  "suggested_next_actions": []
}
```

English: Human-readable messages live inside `data.message` or `error.message`.
Raw GDB or tool output is preserved in `raw_response` where it helps diagnosis.

中文：人类可读消息位于 `data.message` 或 `error.message`。有助于诊断时，原始 GDB 或工具输出会保留在
`raw_response` 中。

## Examples / 示例

- `examples/configs/stm32f4_jlink.yaml`: editable J-Link STM32F4 config / 可编辑的 J-Link STM32F4 配置
- `examples/configs/stm32l431_openocd.yaml`: STM32L431 OpenOCD/ST-Link HIL config / STM32L431 OpenOCD/ST-Link HIL 配置
- `examples/firmware/stm32l431_blinky`: minimal STM32L431 firmware example / 最小 STM32L431 固件示例
- `examples/prompts/debug_hardfault.md`: HardFault diagnosis prompt / HardFault 诊断提示词
- `examples/prompts/freertos_hang.md`: FreeRTOS hang diagnosis prompt / FreeRTOS 卡死诊断提示词

## Hardware-in-the-loop Validation / 硬件在环验证

English: Normal CI is hardware-free and runs lint, tests, compile checks, and
packaging. Real target validation is handled by the manual GitHub Actions
workflow `Hardware-in-the-loop`, which expects a trusted self-hosted runner
labeled `stm32`.

中文：普通 CI 不依赖硬件，只运行 lint、测试、编译检查和打包。真实目标板验证由手动触发的
GitHub Actions 工作流 `Hardware-in-the-loop` 处理，该工作流需要可信的自托管 runner，并带有
`stm32` label。

See `docs/hil-validation.md` for runner requirements, smoke coverage, and the
evidence to keep from each board run.

参见 `docs/hil-validation.md`，了解 runner 要求、烟测覆盖范围以及每次板卡验证需要保留的证据。

## Completed Capabilities / 已完成能力

English: The earlier `Current Limits` items are now implemented as project
capabilities with tests, docs, or gated hardware validation paths.

中文：之前 `Current Limits` 中列出的项目已实现为项目能力，并配套测试、文档或受控的硬件验证路径。

Implemented / 已实现：

- probe-specific reset strategy profiles through `reset_target.strategy`, custom reset commands, and YAML `reset` config / 通过 `reset_target.strategy`、自定义复位命令和 YAML `reset` 配置实现面向不同调试器的复位策略 profile
- board-specific HIL smoke tests gated by `STM32_GDB_MCP_HIL=1` / 通过 `STM32_GDB_MCP_HIL=1` 显式启用的板卡级 HIL 烟测
- SWO/ITM process-output capture via `start_swo_logging`, `get_swo_logs`, and snapshot log context / 通过 `start_swo_logging`、`get_swo_logs` 和快照日志上下文实现 SWO/ITM 进程输出采集
- STM32L431 OpenOCD config and minimal example firmware under `examples/firmware/stm32l431_blinky` / `examples/firmware/stm32l431_blinky` 下的 STM32L431 OpenOCD 配置和最小示例固件
- stable JSON response envelope for MCP tool results while preserving the MCP `TextContent` transport / MCP 工具结果使用稳定 JSON 响应包络，同时保留 MCP `TextContent` 传输外壳

### Autonomous debug loop / 自主调试闭环

English: Primitives that let the AI close the *observe → orient → hypothesize →
act safely → verify* loop and run hands-off. See
`docs/superpowers/plans/2026-06-21-autonomous-debug-loop.md`.

中文：让 AI 闭合"观察 → 定位 → 假设 → 安全动作 → 验证"调试环、可放手运行的原语。
详见 `docs/superpowers/plans/2026-06-21-autonomous-debug-loop.md`。

- structured `run_and_wait` / `wait_for_stop` stop events (reason + symbolized frame) / 结构化停止事件(原因 + 符号化栈帧)
- frame navigation and source symbolization: `select_frame`, `read_frame_variables`, `list_source`, `resolve_address` / 栈帧导航与源码符号化
- conditional / temporary / ignore-count breakpoints / 条件、临时、忽略计数断点
- `reconstruct_fault_context` — unwinds the Cortex-M exception frame to the faulting source line / 还原 Cortex-M 异常压栈帧到出错源码行
- memory-write guardrails + audit log (`set_write_policy`, `get_write_audit_log`) / 内存写入护栏与审计日志
- `configure_debug_freeze` for DBGMCU watchdog/timer freeze while halted / halt 时冻结看门狗/定时器
- `check_session_health` with reconnect for long runs / 会话健康检查与重连
- depth tools: execution control, `disassemble`, symbol/type discovery, coredump capture/load, `verify_flash`, DWT timing and PC sampling / 深度工具:执行控制、反汇编、符号/类型发现、coredump、flash 校验、DWT 计时与 PC 采样

Roadmap / 路线图：

- richer vendor-specific SWO/TPIU auto-configuration / 更完整的厂商特定 SWO/TPIU 自动配置
- more board-specific firmware examples and HIL fixtures / 更多板卡专用示例固件和 HIL fixture
- deeper RTOS-aware deadlock and timing analysis / 更深入的 RTOS 死锁和时序分析
- more STM32 families in the DBGMCU freeze map (G0/G4/H7) / DBGMCU 冻结表覆盖更多 STM32 系列

## Agent guidance / 智能体引导

English: The agent is guided at three levels, so it knows *how* to drive the tools, not
just *what* they are:

1. **Inline** — most tool results carry `suggested_next_actions` (the next loop step).
2. **Always-on** — the MCP server ships `instructions` (the core debug loop + key rules)
   that any MCP client injects automatically; no setup needed.
3. **On-demand** — the `stm32-debug` skill under `skills/stm32-debug/` is a fuller playbook
   (bring-up, HardFault triage, hang finding, minimal-step repro) with replayable
   `run_scenario` templates in `skills/stm32-debug/scenarios/`.

中文：智能体在三个层面被引导,使其知道**如何**驱动工具而不仅是工具**是什么**:

1. **内联** —— 多数工具结果带 `suggested_next_actions`(下一步循环动作)。
2. **常驻** —— MCP server 自带 `instructions`(核心调试循环 + 关键规则),任何 MCP 客户端
   连上即自动注入,无需配置。
3. **按需** —— `skills/stm32-debug/` 下的 `stm32-debug` skill 是更完整的 playbook(bring-up、
   HardFault 定位、挂死排查、最少步骤复现),并在 `skills/stm32-debug/scenarios/` 提供可回放
   的 `run_scenario` 模板。

## Project Discovery / 项目发现

English: `inspect_project` scans a firmware directory and reports common debug artifacts.

中文：`inspect_project` 扫描固件目录，并报告常见调试产物。

- `.elf`, `.axf`, and `.out` firmware images / 固件镜像
- `.map` linker map files / 链接 map 文件
- `.ld` linker scripts / 链接脚本
- `.svd` peripheral descriptions / 外设描述文件
- STM32CubeMX `.ioc` files / STM32CubeMX `.ioc` 文件

English: When an `.ioc` file is present, the inspector extracts useful metadata
such as MCU name, package, project name, and target toolchain. `set_debug_profile`
can also store `project_root`, `elf_path`, `svd_path`, and `mcu`; these profile
paths are included in discovery output even when no directory scan is requested.

中文：当目录中存在 `.ioc` 文件时，检查器会提取 MCU 名称、封装、项目名和目标工具链等元数据。
`set_debug_profile` 也可以保存 `project_root`、`elf_path`、`svd_path` 和 `mcu`；
即使没有请求目录扫描，这些 profile 路径也会出现在发现结果中。

## FreeRTOS Inspection / FreeRTOS 检查

English: `detect_rtos` checks for common FreeRTOS symbols such as `pxCurrentTCB`
and `uxCurrentNumberOfTasks`. `read_current_task` reads the active TCB and
returns the task name, priority, TCB address, and stack pointers.

中文：`detect_rtos` 检查常见 FreeRTOS 符号，例如 `pxCurrentTCB` 和
`uxCurrentNumberOfTasks`。`read_current_task` 读取当前 TCB，并返回任务名、优先级、
TCB 地址和栈指针。

English: `read_freertos_tasks` walks `pxReadyTasksLists` and returns ready tasks
by priority. `read_freertos_task_lists` expands that view across ready, delayed,
suspended, and deleted task lists so the AI can distinguish a CPU-bound task
from one blocked on time, suspension, or deletion cleanup.

中文：`read_freertos_tasks` 遍历 `pxReadyTasksLists`，按优先级返回就绪任务。
`read_freertos_task_lists` 将视图扩展到 ready、delayed、suspended 和 deleted
任务链表，帮助 AI 区分 CPU 占用任务、延时阻塞任务、挂起任务和删除清理任务。

English: `read_freertos_queue` accepts a GDB expression that resolves to a
`Queue_t` pointer or handle. It reports queue capacity, message count, item size,
storage pointers, and tasks waiting to send or receive. This covers queues,
binary semaphores, counting semaphores, and mutexes at the common Queue_t layer;
richer mutex-specific decoding is available through `read_freertos_mutex`.

中文：`read_freertos_queue` 接受可解析为 `Queue_t` 指针或 handle 的 GDB 表达式。
它返回队列容量、消息数量、元素大小、存储指针，以及等待发送/接收的任务。这覆盖普通队列、
二值信号量、计数信号量和 Queue_t 公共层面的互斥量；更细的互斥量解码由
`read_freertos_mutex` 提供。

English: `read_freertos_mutex` adds mutex holder and recursive call count
decoding when `Queue_t.u.xSemaphore` is visible. The mutex holder is returned as
a normal TCB summary, which helps identify deadlocks and priority inversion candidates.

中文：当 `Queue_t.u.xSemaphore` 可见时，`read_freertos_mutex` 会额外解码互斥量持有者和
递归调用次数。互斥量持有者会以常规 TCB 摘要返回，有助于识别死锁和优先级反转候选点。

English: `read_freertos_heap` reads heap variables used by common `heap_4.c` and
`heap_5.c` configurations: `xFreeBytesRemaining`, `xMinimumEverFreeBytesRemaining`,
and `configTOTAL_HEAP_SIZE`. It also derives current and worst-ever used bytes
when enough data is available.

中文：`read_freertos_heap` 读取常见 `heap_4.c` 和 `heap_5.c` 配置使用的堆变量：
`xFreeBytesRemaining`、`xMinimumEverFreeBytesRemaining` 和 `configTOTAL_HEAP_SIZE`。
当数据足够时，它还会推导当前已用字节数和历史最大已用字节数。

English: The FreeRTOS implementation targets debug builds where types such as
`TCB_t`, `ListItem_t`, and `Queue_t` are visible to GDB. If the firmware is
heavily optimized or strips type information, the tool returns a clear error
through the MCP handler instead of guessing.

中文：FreeRTOS 检查面向调试构建，要求 GDB 能看到 `TCB_t`、`ListItem_t` 和 `Queue_t`
等类型。如果固件被高度优化或剥离类型信息，工具会通过 MCP handler 返回明确错误，而不是猜测。

English: `capture_rtos_snapshot` groups detection, current task, ready-list data,
expanded task-list view, and heap data. `capture_debug_snapshot` accepts
`include_project` and `include_rtos` flags to add project and RTOS context to the
normal register/fault/call-stack snapshot.

中文：`capture_rtos_snapshot` 汇总 RTOS 检测、当前任务、就绪链表、扩展任务链表视图和堆数据。
`capture_debug_snapshot` 接受 `include_project` 和 `include_rtos` 标志，可将项目和
RTOS 上下文加入常规寄存器/故障/调用栈快照。

## SEGGER RTT Logs / SEGGER RTT 日志

English: `start_rtt_logging` launches a background process and captures its
stdout/stderr into an in-memory ring buffer. By default it runs `JLinkRTTClient`;
pass `command` and `args` when your SEGGER installation or target setup needs a
different invocation.

中文：`start_rtt_logging` 启动后台进程，并将 stdout/stderr 捕获到内存环形缓冲区。
默认命令是 `JLinkRTTClient`；如果你的 SEGGER 安装路径或目标配置需要不同调用方式，可以传入
`command` 和 `args`。

Example / 示例：

```json
{
  "command": "JLinkRTTClient",
  "args": ["-Device", "STM32F407VG", "-If", "SWD", "-Speed", "4000"]
}
```

English: `get_rtt_logs` returns indexed log entries and supports `limit`,
`since_index`, and `clear`. `clear_rtt_logs` clears the buffer without stopping
the process. `capture_debug_snapshot` accepts `include_logs=true` and optional
`log_limit` to attach recent RTT, SWO, and UART logs to the normal debug evidence bundle.

中文：`get_rtt_logs` 返回带索引的日志条目，并支持 `limit`、`since_index` 和 `clear`。
`clear_rtt_logs` 可以在不停止进程的情况下清空缓冲区。`capture_debug_snapshot` 支持
`include_logs=true` 和可选 `log_limit`，用于把近期 RTT/SWO/UART 日志附加到调试证据包。

English: This layer intentionally captures process output only. It does not
configure RTT control blocks inside firmware; your firmware must already emit
RTT data, and the SEGGER command must be able to connect to the target.

中文：这一层只捕获进程输出，不配置固件内部的 RTT control block。你的固件必须已经输出 RTT 数据，
且 SEGGER 命令本身必须能连接目标。

## SWO/ITM Logs / SWO/ITM 日志

English: SWO/ITM capture is process-based. Start it with a decoder command that
prints decoded SWO/ITM text to stdout; the MCP stores the output in the same
indexed ring-buffer shape used by RTT and UART.

中文：SWO/ITM 采集基于进程输出。启动时传入一个会把解码后的 SWO/ITM 文本打印到 stdout 的解码命令；
MCP 会用与 RTT 和 UART 相同的带索引环形缓冲结构保存输出。

Example / 示例：

```json
{
  "command": "your-swo-decoder",
  "args": ["--device", "STM32L431CCT6", "--frequency", "80000000"]
}
```

English: `get_swo_logs` supports `limit`, `since_index`, and `clear`.
`capture_debug_snapshot` includes SWO logs under `logs.swo` when
`include_logs=true`.

中文：`get_swo_logs` 支持 `limit`、`since_index` 和 `clear`。当
`include_logs=true` 时，`capture_debug_snapshot` 会在 `logs.swo` 下包含 SWO 日志。

## UART Logs / UART 日志

English: UART logging uses `pyserial` and opens a serial port directly.

中文：UART 日志使用 `pyserial`，并直接打开串口。

Start capture with the port name and optional baudrate / 使用端口名和可选波特率启动采集：

```json
{
  "port": "COM7",
  "baudrate": 115200,
  "timeout": 0.1
}
```

English: `get_uart_logs` supports the same `limit`, `since_index`, and `clear`
arguments as RTT logging. `clear_uart_logs` clears buffered entries without
closing the port, and `stop_uart_logging` closes the serial port.

中文：`get_uart_logs` 支持与 RTT 日志相同的 `limit`、`since_index` 和 `clear` 参数。
`clear_uart_logs` 不关闭串口，只清空缓冲条目；`stop_uart_logging` 会关闭串口。

English: When `capture_debug_snapshot` is called with `include_logs=true`, the
`logs` section contains `rtt`, `swo`, and `uart` sub-sections with status and recent
entries. This makes it possible to correlate target halt state, FreeRTOS state,
and firmware text logs in one evidence bundle.

中文：当调用 `capture_debug_snapshot` 并设置 `include_logs=true` 时，`logs` 字段会包含
`rtt`、`swo` 和 `uart` 三个子段，分别给出状态和近期日志。这样可以在同一个证据包中关联目标暂停状态、
FreeRTOS 状态和固件文本日志。
