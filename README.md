# STM32 GDB MCP Server

This project is a Model Context Protocol (MCP) server that lets an AI client drive
STM32 debugging through GDB. It can start a supported GDB server, connect
`arm-none-eabi-gdb`, flash firmware, control execution, inspect target state, and
collect structured evidence for fault analysis.

## Requirements

The server expects these tools to be available on `PATH`:

1. `arm-none-eabi-gdb` from the Arm GNU Toolchain
2. `openocd` for OpenOCD targets
3. `JLinkGDBServerCL` for J-Link targets
4. `st-util` for ST-Link targets

Run the environment check:

```bash
python setup_env.py
```

## Install

```bash
pip install -e .
```

For development and tests:

```bash
pip install -e ".[dev]"
python -m ruff check .
python -m pytest
python -m compileall src tests
python -m build
```

Project maintenance guides:

- `CONTRIBUTING.md`: development workflow, quality gate, and bug evidence
- `SECURITY.md`: private vulnerability reporting and sensitive debug data
- `docs/hil-validation.md`: self-hosted hardware-in-the-loop validation
- `docs/release.md`: release checklist

## Run

```bash
stm32-gdb-mcp
```

Configure your MCP client to launch that command as a stdio MCP server.

Example MCP server entry:

```json
{
  "mcpServers": {
    "stm32-gdb-mcp": {
      "command": "stm32-gdb-mcp"
    }
  }
}
```

## Core Workflow

Typical OpenOCD session:

1. `set_debug_profile` with MCU, board, probe, `server_args`, `elf_path`, and
   `svd_path`.
2. `start_debug_session` with `server_type="openocd"` and OpenOCD config args.
3. `flash_firmware` with the ELF path.
4. `reset_target` with `halt=true`.
5. Use breakpoints, stepping, variables, memory, peripheral registers, and
   snapshots to debug.

## Tool Groups

Session and flashing:

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

Execution control:

- `set_breakpoint`
- `delete_breakpoint`
- `continue_execution`
- `halt_execution`
- `step_over`
- `step_into`
- `set_watchpoint`

Inspection:

- `read_variable`
- `read_memory`
- `write_memory`
- `read_typed_memory`
- `write_typed_memory`
- `read_call_stack`
- `read_core_registers`
- `get_gdb_events`
- `get_gdb_server_logs`

STM32 fault and evidence capture:

- `read_fault_registers`
- `diagnose_fault`
- `capture_debug_snapshot`

FreeRTOS runtime inspection:

- `detect_rtos`
- `read_current_task`
- `read_freertos_tasks`
- `read_freertos_task_lists`
- `read_freertos_queue`
- `read_freertos_mutex`
- `read_freertos_heap`
- `capture_rtos_snapshot`

RTT log capture:

- `start_rtt_logging`
- `stop_rtt_logging`
- `get_rtt_logs`
- `clear_rtt_logs`

UART log capture:

- `start_uart_logging`
- `stop_uart_logging`
- `get_uart_logs`
- `clear_uart_logs`

Automated debug experiments:

- `capture_expressions`
- `assert_expressions`
- `compare_expressions_after_action`

SVD-aware peripheral inspection:

- `load_svd`
- `read_peripheral_register`
- `decode_peripheral_register`

Variable polling:

- `start_variable_tracking`
- `stop_variable_tracking`
- `get_tracked_data`

## Fault Diagnosis

`diagnose_fault` reads Cortex-M SCB fault registers and decodes CFSR/HFSR into
active fault classes and flags such as `PRECISERR`, `BFARVALID`, `UNALIGNED`,
and `DIVBYZERO`. `capture_debug_snapshot` collects a broader bundle:

- core registers
- fault registers and decoded diagnosis
- call stack
- disassembly around `$pc`
- pending GDB events
- captured GDB server logs

This gives an AI client a single evidence packet for HardFault, BusFault,
UsageFault, and MemManage investigations.

## SVD Decoding

`decode_peripheral_register` reads a register by SVD name and decodes bitfields.
The parser supports common SVD bit formats:

- `bitOffset` + `bitWidth`
- `bitRange`
- `lsb` + `msb`
- same-peripheral register `derivedFrom`
- `enumeratedValues`

## Debug Config Files

Use YAML config files to make sessions repeatable across AI clients and projects.
The example at `examples/configs/stm32f4_jlink.yaml` shows the supported shape:

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

`load_debug_config` loads YAML and applies compatible fields to the active debug
profile. `save_debug_config` writes a YAML file. `validate_debug_config` checks
server type, path fields, RTT args, and UART settings without touching the
current session.

## Response Shape

New helper modules use a stable JSON envelope for gradual adoption:

```json
{
  "ok": true,
  "data": {},
  "error": null,
  "raw_response": null,
  "suggested_next_actions": []
}
```

Existing MCP handlers still return human-readable text or JSON for backward
compatibility. New tools should prefer the stable shape where it helps the AI
client make decisions.

## Examples

- `examples/configs/stm32f4_jlink.yaml`: editable J-Link STM32F4 config
- `examples/prompts/debug_hardfault.md`: HardFault diagnosis prompt
- `examples/prompts/freertos_hang.md`: FreeRTOS hang diagnosis prompt

## Hardware-in-the-loop Validation

Normal CI is hardware-free and runs lint, tests, compile checks, and packaging.
Real target validation is handled by the manual GitHub Actions workflow
`Hardware-in-the-loop`, which expects a trusted self-hosted runner labeled
`stm32`.

See `docs/hil-validation.md` for runner requirements, smoke coverage, and the
evidence to keep from each board run.

## Current Limits

This MCP now covers a strong first debugging loop, but it is still GDB-centric.
Future high-value additions are:

- probe-specific reset strategy profiles
- board-specific regression experiments for hardware-in-the-loop tests
- SWO/ITM capture
- real hardware integration tests with example firmware
- full migration of older tool responses to the stable JSON envelope

## Project Discovery

`inspect_project` scans a firmware directory and reports common debug artifacts:

- `.elf`, `.axf`, and `.out` firmware images
- `.map` linker map files
- `.ld` linker scripts
- `.svd` peripheral descriptions
- STM32CubeMX `.ioc` files

When an `.ioc` file is present, the inspector extracts useful metadata such as
MCU name, package, project name, and target toolchain. `set_debug_profile` can
also store `project_root`, `elf_path`, `svd_path`, and `mcu`; these profile paths
are included in discovery output even when no directory scan is requested.

## FreeRTOS Inspection

`detect_rtos` checks for common FreeRTOS symbols such as `pxCurrentTCB` and
`uxCurrentNumberOfTasks`. `read_current_task` reads the active TCB and returns the
task name, priority, TCB address, and stack pointers.

`read_freertos_tasks` walks `pxReadyTasksLists` and returns ready tasks by
priority. `read_freertos_task_lists` expands that view across ready, delayed,
suspended, and deleted task lists so the AI can distinguish a CPU-bound task
from one blocked on time, suspension, or deletion cleanup.

`read_freertos_queue` accepts a GDB expression that resolves to a `Queue_t`
pointer or handle. It reports queue capacity, message count, item size, storage
pointers, and tasks waiting to send or receive. This covers queues, binary
semaphores, counting semaphores, and mutexes at the common Queue_t layer; richer
mutex-specific decoding is available through `read_freertos_mutex`.

`read_freertos_mutex` adds mutex holder and recursive call count decoding when
`Queue_t.u.xSemaphore` is visible. The mutex holder is returned as a normal TCB
summary, which helps identify deadlocks and priority inversion candidates.

`read_freertos_heap` reads heap variables used by common `heap_4.c` and
`heap_5.c` configurations: `xFreeBytesRemaining`,
`xMinimumEverFreeBytesRemaining`, and `configTOTAL_HEAP_SIZE`. It also derives
current and worst-ever used bytes when enough data is available.

The FreeRTOS implementation targets debug builds where types such as `TCB_t`,
`ListItem_t`, and `Queue_t` are visible to GDB. If the firmware is heavily
optimized or strips type information, the tool returns a clear error through the
MCP handler instead of guessing.

`capture_rtos_snapshot` groups detection, current task, ready-list data, the
expanded task-list view, and heap data. `capture_debug_snapshot` accepts
`include_project` and `include_rtos` flags to add project and RTOS context to the
normal register/fault/call-stack snapshot.

## SEGGER RTT Logs

`start_rtt_logging` launches a background process and captures its stdout/stderr
into an in-memory ring buffer. By default it runs `JLinkRTTClient`; pass
`command` and `args` when your SEGGER installation or target setup needs a
different invocation.

Example:

```json
{
  "command": "JLinkRTTClient",
  "args": ["-Device", "STM32F407VG", "-If", "SWD", "-Speed", "4000"]
}
```

`get_rtt_logs` returns indexed log entries and supports `limit`, `since_index`,
and `clear`. `clear_rtt_logs` clears the buffer without stopping the process.
`capture_debug_snapshot` accepts `include_logs=true` and optional `log_limit` to
attach recent RTT and UART logs to the normal debug evidence bundle.

This layer intentionally captures process output only. It does not configure RTT
control blocks inside firmware; your firmware must already emit RTT data, and
the SEGGER command must be able to connect to the target.

## UART Logs

UART logging uses `pyserial` and opens a serial port directly. Start capture with
the port name and optional baudrate:

```json
{
  "port": "COM7",
  "baudrate": 115200,
  "timeout": 0.1
}
```

`get_uart_logs` supports the same `limit`, `since_index`, and `clear` arguments
as RTT logging. `clear_uart_logs` clears buffered entries without closing the
port, and `stop_uart_logging` closes the serial port.

When `capture_debug_snapshot` is called with `include_logs=true`, the `logs`
section contains both `rtt` and `uart` sub-sections with status and recent
entries. This makes it possible to correlate target halt state, FreeRTOS state,
and firmware text logs in one evidence bundle.

## Automated Debug Experiments

The experiment tools let an AI client run small repeatable checks instead of
manually issuing one GDB command at a time.

`capture_expressions` evaluates a batch of C/GDB expressions and returns parsed
integer or string values:

```json
{
  "expressions": ["counter", "huart1.gState", "uxCurrentNumberOfTasks"]
}
```

`assert_expressions` reads expressions and evaluates conditions. Supported
operators are `==`, `!=`, `>`, `>=`, `<`, and `<=`:

```json
{
  "assertions": [
    {"expression": "uxCurrentNumberOfTasks", "operator": ">=", "expected": 3},
    {"expression": "system_state", "operator": "==", "expected": "RUN"}
  ]
}
```

`compare_expressions_after_action` captures values, performs one debug action,
then captures the same values again and reports changes. Supported actions are
`step_over`, `step_into`, `continue`, `halt`, and `reset_halt`.

```json
{
  "expressions": ["counter", "GPIOA->ODR"],
  "action": "step_over"
}
```
