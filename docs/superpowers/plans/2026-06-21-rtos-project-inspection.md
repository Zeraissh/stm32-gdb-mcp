# RTOS Project Inspection Implementation Plan / RTOS 项目检查实施计划

> **For agentic workers / 面向智能体执行者：** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` (recommended) or
> `superpowers:executing-plans` to implement this plan task-by-task.
> 必需子技能：使用 `superpowers:subagent-driven-development`（推荐）或
> `superpowers:executing-plans` 按任务执行本计划。Steps use checkbox (`- [ ]`)
> syntax for tracking. 步骤使用 checkbox (`- [ ]`) 语法跟踪。

**Goal / 目标：** Add project discovery and FreeRTOS runtime inspection tools to
the STM32 GDB MCP server. 为 STM32 GDB MCP 服务器增加项目发现和 FreeRTOS 运行时检查工具。

**Architecture / 架构：** Keep project discovery, RTOS symbol detection, and
FreeRTOS runtime decoding in separate modules. MCP tool handlers in `server.py`
only translate tool arguments, call the modules, and format JSON output.
项目发现、RTOS 符号检测和 FreeRTOS 运行时解码分别放在独立模块中；`server.py` 中的 MCP
工具 handler 只负责转换参数、调用模块并格式化 JSON 输出。

**Tech Stack / 技术栈：** Python 3.10+, pytest, MCP Python SDK, pygdbmi/GDB CLI expressions.

---

### Task 1: Project Discovery / 任务 1：项目发现

**Files / 文件：**
- Create / 创建：`src/mcp_server/project_inspector.py`
- Test / 测试：`tests/test_project_inspector.py`

- [x] Write failing tests for `.ioc`, ELF, map, linker script, and SVD discovery. / 为 `.ioc`、ELF、map、链接脚本和 SVD 发现编写失败测试。
- [x] Implement directory scanning and `.ioc` metadata parsing. / 实现目录扫描和 `.ioc` 元数据解析。
- [x] Run targeted tests until green. / 运行定向测试直到通过。

### Task 2: FreeRTOS Inspection / 任务 2：FreeRTOS 检查

**Files / 文件：**
- Create / 创建：`src/mcp_server/freertos_inspector.py`
- Test / 测试：`tests/test_freertos_inspector.py`

- [x] Write failing tests for RTOS symbol detection, current task readout, and task list normalization. / 为 RTOS 符号检测、当前任务读取和任务链表归一化编写失败测试。
- [x] Implement GDB expression helpers and resilient response parsing. / 实现 GDB 表达式辅助函数和稳健响应解析。
- [x] Run targeted tests until green. / 运行定向测试直到通过。

### Task 3: MCP Tool Exposure / 任务 3：暴露 MCP 工具

**Files / 文件：**
- Modify / 修改：`src/mcp_server/server.py`
- Modify / 修改：`src/mcp_server/debug_snapshot.py`
- Test / 测试：`tests/test_server_tools.py`
- Test / 测试：`tests/test_debug_snapshot.py`

- [x] Add `inspect_project`, `detect_rtos`, `read_current_task`, `read_freertos_tasks`, and `capture_rtos_snapshot`. / 增加 `inspect_project`、`detect_rtos`、`read_current_task`、`read_freertos_tasks` 和 `capture_rtos_snapshot`。
- [x] Extend `capture_debug_snapshot` to optionally include project and RTOS data. / 扩展 `capture_debug_snapshot`，可选加入项目和 RTOS 数据。
- [x] Run full test suite. / 运行完整测试套件。

### Task 4: Documentation / 任务 4：文档

**Files / 文件：**
- Modify / 修改：`README.md`

- [x] Document project discovery and FreeRTOS tool usage. / 记录项目发现和 FreeRTOS 工具使用方式。
- [x] Run full test suite and compile check. / 运行完整测试和编译检查。

### Task 5: FreeRTOS Blocked/Suspended Task Lists / 任务 5：FreeRTOS 阻塞/挂起任务链表

**Files / 文件：**
- Modify / 修改：`src/mcp_server/freertos_inspector.py`
- Modify / 修改：`src/mcp_server/server.py`
- Test / 测试：`tests/test_freertos_inspector.py`
- Test / 测试：`tests/test_server_tools.py`

- [x] Write failing tests for delayed and suspended task-list walking. / 为 delayed 和 suspended 任务链表遍历编写失败测试。
- [x] Refactor list traversal into one reusable helper. / 将链表遍历重构为一个可复用辅助函数。
- [x] Add `read_freertos_task_lists` MCP tool. / 增加 `read_freertos_task_lists` MCP 工具。
- [x] Run targeted and full tests. / 运行定向和完整测试。

### Task 6: FreeRTOS Queue/Semaphore Inspection / 任务 6：FreeRTOS 队列/信号量检查

**Files / 文件：**
- Modify / 修改：`src/mcp_server/freertos_inspector.py`
- Modify / 修改：`src/mcp_server/server.py`
- Test / 测试：`tests/test_freertos_inspector.py`
- Test / 测试：`tests/test_server_tools.py`
- Modify / 修改：`README.md`

- [x] Write failing tests for Queue_t field parsing and waiting task lists. / 为 Queue_t 字段解析和等待任务链表编写失败测试。
- [x] Add `read_freertos_queue` MCP tool. / 增加 `read_freertos_queue` MCP 工具。
- [x] Document supported queue/semaphore fields and debug-symbol assumptions. / 记录支持的队列/信号量字段和调试符号假设。
- [x] Run full verification. / 运行完整验证。

### Task 7: FreeRTOS Mutex and Heap Diagnostics / 任务 7：FreeRTOS 互斥量与堆诊断

**Files / 文件：**
- Modify / 修改：`src/mcp_server/freertos_inspector.py`
- Modify / 修改：`src/mcp_server/server.py`
- Test / 测试：`tests/test_freertos_inspector.py`
- Test / 测试：`tests/test_server_tools.py`
- Modify / 修改：`README.md`

- [x] Write failing tests for mutex owner/recursive-call fields. / 为互斥量持有者和递归调用字段编写失败测试。
- [x] Write failing tests for heap variable parsing. / 为堆变量解析编写失败测试。
- [x] Add `read_freertos_mutex` and `read_freertos_heap` MCP tools. / 增加 `read_freertos_mutex` 和 `read_freertos_heap` MCP 工具。
- [x] Include heap data in `capture_rtos_snapshot` when symbols exist. / 符号存在时在 `capture_rtos_snapshot` 中包含堆数据。
- [x] Run full verification. / 运行完整验证。

### Task 8: SEGGER RTT Log Capture / 任务 8：SEGGER RTT 日志采集

**Files / 文件：**
- Create / 创建：`src/mcp_server/log_reader.py`
- Modify / 修改：`src/mcp_server/server.py`
- Modify / 修改：`src/mcp_server/debug_snapshot.py`
- Test / 测试：`tests/test_log_reader.py`
- Test / 测试：`tests/test_server_tools.py`
- Test / 测试：`tests/test_debug_snapshot.py`
- Modify / 修改：`README.md`

- [x] Write failing tests for ring-buffered log capture. / 为环形缓冲日志采集编写失败测试。
- [x] Add background process log reader with injectable process factory. / 增加带可注入进程工厂的后台进程日志读取器。
- [x] Add `start_rtt_logging`, `stop_rtt_logging`, `get_rtt_logs`, and `clear_rtt_logs` MCP tools. / 增加 `start_rtt_logging`、`stop_rtt_logging`、`get_rtt_logs` 和 `clear_rtt_logs` MCP 工具。
- [x] Add optional log context to `capture_debug_snapshot`. / 为 `capture_debug_snapshot` 增加可选日志上下文。
- [x] Document RTT usage and verification limits. / 记录 RTT 使用方式和验证限制。

### Task 9: UART Serial Log Capture / 任务 9：UART 串口日志采集

**Files / 文件：**
- Modify / 修改：`src/mcp_server/log_reader.py`
- Modify / 修改：`src/mcp_server/server.py`
- Modify / 修改：`pyproject.toml`
- Test / 测试：`tests/test_log_reader.py`
- Test / 测试：`tests/test_server_tools.py`
- Modify / 修改：`README.md`

- [x] Write failing tests for serial line capture with an injectable serial factory. / 为带可注入串口工厂的串口行采集编写失败测试。
- [x] Add `SerialLogReader` using lazy `pyserial` import. / 增加使用惰性 `pyserial` 导入的 `SerialLogReader`。
- [x] Add `start_uart_logging`, `stop_uart_logging`, `get_uart_logs`, and `clear_uart_logs` MCP tools. / 增加 `start_uart_logging`、`stop_uart_logging`、`get_uart_logs` 和 `clear_uart_logs` MCP 工具。
- [x] Include UART logs in debug snapshots when `include_logs=true`. / 当 `include_logs=true` 时在调试快照中包含 UART 日志。
- [x] Document UART usage and serial dependency. / 记录 UART 使用方式和串口依赖。

### Task 10: Automated Debug Experiments / 任务 10：自动化调试实验

**Files / 文件：**
- Create / 创建：`src/mcp_server/debug_experiments.py`
- Modify / 修改：`src/mcp_server/server.py`
- Test / 测试：`tests/test_debug_experiments.py`
- Test / 测试：`tests/test_server_tools.py`
- Modify / 修改：`README.md`

- [x] Write failing tests for expression sampling. / 为表达式采样编写失败测试。
- [x] Write failing tests for assertion evaluation. / 为断言求值编写失败测试。
- [x] Write failing tests for before/after comparison around a debug action. / 为调试动作前后对比编写失败测试。
- [x] Add `capture_expressions`, `assert_expressions`, and `compare_expressions_after_action` MCP tools. / 增加 `capture_expressions`、`assert_expressions` 和 `compare_expressions_after_action` MCP 工具。
- [x] Document supported operators and actions. / 记录支持的操作符和动作。
