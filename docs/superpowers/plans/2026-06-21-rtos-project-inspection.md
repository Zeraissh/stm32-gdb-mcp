# RTOS Project Inspection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add project discovery and FreeRTOS runtime inspection tools to the STM32 GDB MCP server.

**Architecture:** Keep project discovery, RTOS symbol detection, and FreeRTOS runtime decoding in separate modules. MCP tool handlers in `server.py` only translate tool arguments, call the modules, and format JSON output.

**Tech Stack:** Python 3.10+, pytest, MCP Python SDK, pygdbmi/GDB CLI expressions.

---

### Task 1: Project Discovery

**Files:**
- Create: `src/mcp_server/project_inspector.py`
- Test: `tests/test_project_inspector.py`

- [x] Write failing tests for `.ioc`, ELF, map, linker script, and SVD discovery.
- [x] Implement directory scanning and `.ioc` metadata parsing.
- [x] Run targeted tests until green.

### Task 2: FreeRTOS Inspection

**Files:**
- Create: `src/mcp_server/freertos_inspector.py`
- Test: `tests/test_freertos_inspector.py`

- [x] Write failing tests for RTOS symbol detection, current task readout, and task list normalization.
- [x] Implement GDB expression helpers and resilient response parsing.
- [x] Run targeted tests until green.

### Task 3: MCP Tool Exposure

**Files:**
- Modify: `src/mcp_server/server.py`
- Modify: `src/mcp_server/debug_snapshot.py`
- Test: `tests/test_server_tools.py`
- Test: `tests/test_debug_snapshot.py`

- [x] Add `inspect_project`, `detect_rtos`, `read_current_task`, `read_freertos_tasks`, and `capture_rtos_snapshot`.
- [x] Extend `capture_debug_snapshot` to optionally include project and RTOS data.
- [x] Run full test suite.

### Task 4: Documentation

**Files:**
- Modify: `README.md`

- [x] Document project discovery and FreeRTOS tool usage.
- [x] Run full test suite and compile check.

### Task 5: FreeRTOS Blocked/Suspended Task Lists

**Files:**
- Modify: `src/mcp_server/freertos_inspector.py`
- Modify: `src/mcp_server/server.py`
- Test: `tests/test_freertos_inspector.py`
- Test: `tests/test_server_tools.py`

- [x] Write failing tests for delayed and suspended task-list walking.
- [x] Refactor list traversal into one reusable helper.
- [x] Add `read_freertos_task_lists` MCP tool.
- [x] Run targeted and full tests.

### Task 6: FreeRTOS Queue/Semaphore Inspection

**Files:**
- Modify: `src/mcp_server/freertos_inspector.py`
- Modify: `src/mcp_server/server.py`
- Test: `tests/test_freertos_inspector.py`
- Test: `tests/test_server_tools.py`
- Modify: `README.md`

- [x] Write failing tests for Queue_t field parsing and waiting task lists.
- [x] Add `read_freertos_queue` MCP tool.
- [x] Document supported queue/semaphore fields and debug-symbol assumptions.
- [x] Run full verification.

### Task 7: FreeRTOS Mutex and Heap Diagnostics

**Files:**
- Modify: `src/mcp_server/freertos_inspector.py`
- Modify: `src/mcp_server/server.py`
- Test: `tests/test_freertos_inspector.py`
- Test: `tests/test_server_tools.py`
- Modify: `README.md`

- [x] Write failing tests for mutex owner/recursive-call fields.
- [x] Write failing tests for heap variable parsing.
- [x] Add `read_freertos_mutex` and `read_freertos_heap` MCP tools.
- [x] Include heap data in `capture_rtos_snapshot` when symbols exist.
- [x] Run full verification.

### Task 8: SEGGER RTT Log Capture

**Files:**
- Create: `src/mcp_server/log_reader.py`
- Modify: `src/mcp_server/server.py`
- Modify: `src/mcp_server/debug_snapshot.py`
- Test: `tests/test_log_reader.py`
- Test: `tests/test_server_tools.py`
- Test: `tests/test_debug_snapshot.py`
- Modify: `README.md`

- [x] Write failing tests for ring-buffered log capture.
- [x] Add background process log reader with injectable process factory.
- [x] Add `start_rtt_logging`, `stop_rtt_logging`, `get_rtt_logs`, and `clear_rtt_logs` MCP tools.
- [x] Add optional log context to `capture_debug_snapshot`.
- [x] Document RTT usage and verification limits.

### Task 9: UART Serial Log Capture

**Files:**
- Modify: `src/mcp_server/log_reader.py`
- Modify: `src/mcp_server/server.py`
- Modify: `pyproject.toml`
- Test: `tests/test_log_reader.py`
- Test: `tests/test_server_tools.py`
- Modify: `README.md`

- [x] Write failing tests for serial line capture with an injectable serial factory.
- [x] Add `SerialLogReader` using lazy `pyserial` import.
- [x] Add `start_uart_logging`, `stop_uart_logging`, `get_uart_logs`, and `clear_uart_logs` MCP tools.
- [x] Include UART logs in debug snapshots when `include_logs=true`.
- [x] Document UART usage and serial dependency.

### Task 10: Automated Debug Experiments

**Files:**
- Create: `src/mcp_server/debug_experiments.py`
- Modify: `src/mcp_server/server.py`
- Test: `tests/test_debug_experiments.py`
- Test: `tests/test_server_tools.py`
- Modify: `README.md`

- [x] Write failing tests for expression sampling.
- [x] Write failing tests for assertion evaluation.
- [x] Write failing tests for before/after comparison around a debug action.
- [x] Add `capture_expressions`, `assert_expressions`, and `compare_expressions_after_action` MCP tools.
- [x] Document supported operators and actions.
