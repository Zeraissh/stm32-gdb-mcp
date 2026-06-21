import asyncio

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .debug_config import (
    load_debug_config as load_debug_config_file,
)
from .debug_config import (
    save_debug_config as save_debug_config_file,
)
from .debug_config import (
    validate_debug_config as validate_debug_config_data,
)
from .debug_experiments import (
    assert_expressions as run_expression_assertions,
)
from .debug_experiments import (
    capture_expressions as run_expression_capture,
)
from .debug_experiments import (
    compare_expressions_after_action,
)
from .debug_profile import DebugProfileStore
from .debug_snapshot import collect_debug_snapshot
from .fault_analysis import diagnose_fault_registers
from .freertos_inspector import FreeRTOSInspector
from .gdb_client import GdbClientManager
from .gdb_manager import GdbServerManager
from .log_reader import ProcessLogReader, SerialLogReader
from .project_inspector import inspect_project
from .reset_strategy import resolve_reset_command
from .svd_parser import SVDParser
from .tool_response import content_error, content_success
from .tracker import VariableTracker

server = Server("stm32-gdb-mcp")
gdb_manager = GdbServerManager()
gdb_client = GdbClientManager()
svd_parser = SVDParser()
variable_tracker = VariableTracker(gdb_client)
debug_profile = DebugProfileStore()
freertos_inspector = FreeRTOSInspector(gdb_client)
rtt_log_reader = ProcessLogReader("rtt")
uart_log_reader = SerialLogReader()

@server.list_tools()
async def handle_list_tools() -> list[Tool]:
    return [
        # --- Step 4: Basic Control and Flashing ---
        Tool(
            name="start_debug_session",
            description="Starts the specified GDB Server (openocd, stlink, jlink) and connects the GDB Client to it.",
            inputSchema={
                "type": "object",
                "properties": {
                    "server_type": {"type": "string", "enum": ["openocd", "stlink", "jlink"], "description": "Type of debug server backend."},
                    "server_args": {"type": "array", "items": {"type": "string"}, "description": "Optional args for the server e.g. ['-f', 'interface/stlink.cfg', '-f', 'target/stm32f4x.cfg']"}
                },
                "required": ["server_type"]
            }
        ),
        Tool(
            name="stop_debug_session",
            description="Stops the GDB client and server.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="flash_firmware",
            description="Flashes a compiled firmware binary to the target device.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Absolute path to the compiled firmware file (e.g. .elf)."}
                },
                "required": ["file_path"]
            }
        ),
        Tool(
            name="reset_target",
            description="Resets the target device. Can optionally halt immediately after reset.",
            inputSchema={
                "type": "object",
                "properties": {
                    "halt": {"type": "boolean", "description": "If true, halts the CPU immediately after reset."},
                    "strategy": {"type": "string", "description": "Optional reset strategy, e.g. default, under_reset, or software."},
                    "command": {"type": "string", "description": "Optional custom GDB monitor reset command."}
                },
                "required": ["halt"]
            }
        ),
        # --- Step 5: Core Debug Interaction ---
        Tool(
            name="set_breakpoint",
            description="Sets a breakpoint at a specific function, line, or address.",
            inputSchema={
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "Location to break at, e.g., 'main', 'main.c:42', or '*0x08001000'."}
                },
                "required": ["location"]
            }
        ),
        Tool(
            name="delete_breakpoint",
            description="Deletes a breakpoint by its ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "breakpoint_id": {"type": "string", "description": "ID of the breakpoint to delete (e.g., '1')."}
                },
                "required": ["breakpoint_id"]
            }
        ),
        Tool(
            name="continue_execution",
            description="Resumes execution of the target device until the next breakpoint.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="halt_execution",
            description="Interrupts/halts the target device execution.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="step_over",
            description="Steps over the current line of code.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="step_into",
            description="Steps into the current function call.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="read_variable",
            description="Reads the value of a C variable currently in scope.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Name of the variable to read."}
                },
                "required": ["name"]
            }
        ),
        Tool(
            name="read_memory",
            description="Reads a block of memory from the target.",
            inputSchema={
                "type": "object",
                "properties": {
                    "address": {"type": "string", "description": "Hex address to read from, e.g., '0x20000000'."},
                    "length": {"type": "integer", "description": "Number of bytes to read."}
                },
                "required": ["address", "length"]
            }
        ),
        Tool(
            name="write_memory",
            description="Writes a value to a specific memory address.",
            inputSchema={
                "type": "object",
                "properties": {
                    "address": {"type": "string", "description": "Hex address to write to, e.g., '0x20000000'."},
                    "value": {"type": "string", "description": "Value to write, e.g., '0xFF' or '1234'."}
                },
                "required": ["address", "value"]
            }
        ),
        Tool(
            name="get_gdb_events",
            description="Polls GDB for any asynchronous events (like hitting a breakpoint) or stdout messages.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="get_gdb_server_logs",
            description="Returns recent logs captured from the active GDB server process.",
            inputSchema={"type": "object", "properties": {}}
        ),
        # --- Step 6: Advanced Analysis ---
        Tool(
            name="read_call_stack",
            description="Reads the current call stack (backtrace).",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="read_core_registers",
            description="Reads CPU core registers using GDB's info registers command.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="read_fault_registers",
            description="Reads Cortex-M SCB fault status registers (CFSR, HFSR, DFSR, MMFAR, BFAR, AFSR).",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="diagnose_fault",
            description="Reads and decodes Cortex-M fault registers into likely fault causes.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="capture_debug_snapshot",
            description="Captures a structured debug snapshot: core registers, fault registers, call stack, PC disassembly, GDB events, server logs, and optional project/RTOS context.",
            inputSchema={
                "type": "object",
                "properties": {
                    "include_project": {"type": "boolean", "description": "Include project discovery context."},
                    "include_rtos": {"type": "boolean", "description": "Include FreeRTOS runtime context."},
                    "include_logs": {"type": "boolean", "description": "Include captured RTT logs."},
                    "log_limit": {"type": "integer", "description": "Maximum number of RTT log entries to include."},
                    "project_root": {"type": "string", "description": "Optional project root for discovery."}
                }
            }
        ),
        Tool(
            name="inspect_project",
            description="Discovers firmware project artifacts such as ELF, map, linker script, SVD, and STM32CubeMX .ioc metadata.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_root": {"type": "string", "description": "Project directory to scan. Uses debug profile project_root if omitted."}
                }
            }
        ),
        Tool(
            name="detect_rtos",
            description="Detects whether FreeRTOS symbols are available in the current GDB session.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="read_current_task",
            description="Reads the current FreeRTOS task from pxCurrentTCB.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="read_freertos_tasks",
            description="Walks FreeRTOS ready task lists and returns task names, priorities, TCB addresses, and stack pointers.",
            inputSchema={
                "type": "object",
                "properties": {
                    "max_priorities": {"type": "integer", "description": "Optional priority count override."},
                    "max_tasks": {"type": "integer", "description": "Maximum number of tasks to return."}
                }
            }
        ),
        Tool(
            name="read_freertos_task_lists",
            description="Reads FreeRTOS ready, delayed, suspended, and deleted task lists.",
            inputSchema={
                "type": "object",
                "properties": {
                    "max_priorities": {"type": "integer", "description": "Optional priority count override."},
                    "max_tasks": {"type": "integer", "description": "Maximum number of tasks to return across lists."}
                }
            }
        ),
        Tool(
            name="read_freertos_queue",
            description="Reads a FreeRTOS Queue_t/Semaphore object and tasks waiting to send or receive.",
            inputSchema={
                "type": "object",
                "properties": {
                    "queue": {"type": "string", "description": "GDB expression resolving to a Queue_t pointer, e.g. 'myQueueHandle'."}
                },
                "required": ["queue"]
            }
        ),
        Tool(
            name="read_freertos_mutex",
            description="Reads a FreeRTOS mutex/semaphore Queue_t object, including mutex holder and recursive call count when available.",
            inputSchema={
                "type": "object",
                "properties": {
                    "mutex": {"type": "string", "description": "GDB expression resolving to a mutex/semaphore Queue_t pointer or handle."}
                },
                "required": ["mutex"]
            }
        ),
        Tool(
            name="read_freertos_heap",
            description="Reads FreeRTOS heap_4/heap_5 style heap usage variables when debug symbols are available.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="capture_rtos_snapshot",
            description="Captures FreeRTOS detection, current task, ready tasks, and task-list snapshot.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="start_rtt_logging",
            description="Starts background SEGGER RTT log capture using JLinkRTTClient or a custom command.",
            inputSchema={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Executable to launch. Defaults to JLinkRTTClient."},
                    "args": {"type": "array", "items": {"type": "string"}, "description": "Command arguments."}
                }
            }
        ),
        Tool(
            name="stop_rtt_logging",
            description="Stops the background RTT log capture process.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="get_rtt_logs",
            description="Returns captured RTT log lines.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Maximum number of recent log entries to return."},
                    "since_index": {"type": "integer", "description": "Only return entries with an index greater than this value."},
                    "clear": {"type": "boolean", "description": "Clear returned log entries after reading."}
                }
            }
        ),
        Tool(
            name="clear_rtt_logs",
            description="Clears buffered RTT log lines without stopping capture.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="start_uart_logging",
            description="Starts background UART serial log capture.",
            inputSchema={
                "type": "object",
                "properties": {
                    "port": {"type": "string", "description": "Serial port name, e.g. COM7 or /dev/ttyUSB0."},
                    "baudrate": {"type": "integer", "description": "Serial baudrate. Defaults to 115200."},
                    "timeout": {"type": "number", "description": "Serial read timeout in seconds. Defaults to 0.1."}
                },
                "required": ["port"]
            }
        ),
        Tool(
            name="stop_uart_logging",
            description="Stops background UART serial log capture.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="get_uart_logs",
            description="Returns captured UART log lines.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Maximum number of recent log entries to return."},
                    "since_index": {"type": "integer", "description": "Only return entries with an index greater than this value."},
                    "clear": {"type": "boolean", "description": "Clear returned log entries after reading."}
                }
            }
        ),
        Tool(
            name="clear_uart_logs",
            description="Clears buffered UART log lines without stopping capture.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="capture_expressions",
            description="Reads a batch of GDB expressions and returns parsed values.",
            inputSchema={
                "type": "object",
                "properties": {
                    "expressions": {"type": "array", "items": {"type": "string"}, "description": "GDB/C expressions to evaluate."}
                },
                "required": ["expressions"]
            }
        ),
        Tool(
            name="assert_expressions",
            description="Reads GDB expressions and evaluates assertions with operators ==, !=, >, >=, <, <=.",
            inputSchema={
                "type": "object",
                "properties": {
                    "assertions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "expression": {"type": "string"},
                                "operator": {"type": "string", "enum": ["==", "!=", ">", ">=", "<", "<="]},
                                "expected": {}
                            },
                            "required": ["expression", "expected"]
                        }
                    }
                },
                "required": ["assertions"]
            }
        ),
        Tool(
            name="compare_expressions_after_action",
            description="Captures expressions, performs one debug action, captures them again, and reports changes.",
            inputSchema={
                "type": "object",
                "properties": {
                    "expressions": {"type": "array", "items": {"type": "string"}},
                    "action": {"type": "string", "enum": ["step_over", "step_into", "continue", "halt", "reset_halt"]}
                },
                "required": ["expressions", "action"]
            }
        ),
        Tool(
            name="set_watchpoint",
            description="Sets a hardware watchpoint on a memory address or variable.",
            inputSchema={
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "Variable or address to watch."},
                    "access_type": {"type": "string", "enum": ["r", "w", "a"], "description": "Read (r), Write (w), or Access (a)."}
                },
                "required": ["location", "access_type"]
            }
        ),
        Tool(
            name="load_svd",
            description="Loads an SVD file for peripheral parsing.",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path to the .svd file."}
                },
                "required": ["filepath"]
            }
        ),
        Tool(
            name="read_peripheral_register",
            description="Reads a peripheral register using its name from the loaded SVD.",
            inputSchema={
                "type": "object",
                "properties": {
                    "peripheral": {"type": "string", "description": "Peripheral name (e.g., 'GPIOA')."},
                    "register": {"type": "string", "description": "Register name (e.g., 'ODR')."}
                },
                "required": ["peripheral", "register"]
            }
        ),
        Tool(
            name="decode_peripheral_register",
            description="Reads and decodes a peripheral register with SVD bitfield names and enumerated values.",
            inputSchema={
                "type": "object",
                "properties": {
                    "peripheral": {"type": "string", "description": "Peripheral name (e.g., 'GPIOA')."},
                    "register": {"type": "string", "description": "Register name (e.g., 'MODER')."}
                },
                "required": ["peripheral", "register"]
            }
        ),
        Tool(
            name="read_typed_memory",
            description="Reads memory with an explicit element width and count.",
            inputSchema={
                "type": "object",
                "properties": {
                    "address": {"type": "string", "description": "Hex address to read from, e.g., '0x20000000'."},
                    "width_bits": {"type": "integer", "enum": [8, 16, 32, 64], "description": "Element width in bits."},
                    "count": {"type": "integer", "description": "Number of elements to read."}
                },
                "required": ["address", "width_bits", "count"]
            }
        ),
        Tool(
            name="write_typed_memory",
            description="Writes memory with an explicit C integer width.",
            inputSchema={
                "type": "object",
                "properties": {
                    "address": {"type": "string", "description": "Hex address to write to, e.g., '0x20000000'."},
                    "value": {"type": "string", "description": "Value to write, e.g., '0x12345678'."},
                    "width_bits": {"type": "integer", "enum": [8, 16, 32, 64], "description": "Element width in bits."}
                },
                "required": ["address", "value", "width_bits"]
            }
        ),
        Tool(
            name="set_debug_profile",
            description="Stores board/session defaults such as MCU, probe, GDB server args, ELF path, and SVD path.",
            inputSchema={
                "type": "object",
                "properties": {
                    "mcu": {"type": "string"},
                    "board": {"type": "string"},
                    "probe": {"type": "string"},
                    "server_type": {"type": "string", "enum": ["openocd", "stlink", "jlink"]},
                    "server_args": {"type": "array", "items": {"type": "string"}},
                    "elf_path": {"type": "string"},
                    "svd_path": {"type": "string"},
                    "project_root": {"type": "string"},
                    "notes": {"type": "string"}
                }
            }
        ),
        Tool(
            name="get_debug_profile",
            description="Returns the stored board/session defaults.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="load_debug_config",
            description="Loads a YAML debug config and applies compatible fields to the active debug profile.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to a YAML debug config file."}
                },
                "required": ["path"]
            }
        ),
        Tool(
            name="save_debug_config",
            description="Saves a YAML debug config file.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Destination YAML config path."},
                    "config": {"type": "object", "description": "Config object to save."}
                },
                "required": ["path", "config"]
            }
        ),
        Tool(
            name="validate_debug_config",
            description="Validates a YAML debug config object without saving it.",
            inputSchema={
                "type": "object",
                "properties": {
                    "config": {"type": "object", "description": "Config object to validate."}
                },
                "required": ["config"]
            }
        ),
        # --- Step 7: Tracing ---
        Tool(
            name="start_variable_tracking",
            description="Starts background tracking of a variable at a specified interval.",
            inputSchema={
                "type": "object",
                "properties": {
                    "variable": {"type": "string", "description": "Variable to track."},
                    "interval_ms": {"type": "integer", "description": "Polling interval in milliseconds."}
                },
                "required": ["variable", "interval_ms"]
            }
        ),
        Tool(
            name="stop_variable_tracking",
            description="Stops background variable tracking.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="get_tracked_data",
            description="Retrieves the tracked variable data for plotting or analysis.",
            inputSchema={"type": "object", "properties": {}}
        )
    ]

@server.call_tool()
async def handle_call_tool(name: str, arguments: dict | None) -> list[TextContent]:
    if arguments is None:
        arguments = {}

    try:
        if name == "start_debug_session":
            server_type = arguments["server_type"]
            args = arguments.get("server_args", [])
            port = gdb_manager.start(server_type, args)
            gdb_client.start_gdb()
            resp = gdb_client.connect("localhost", port)
            return [content_success(
                {"message": "Debug session started", "server_type": server_type, "port": port},
                raw_response=resp,
            )]

        elif name == "stop_debug_session":
            gdb_client.stop_gdb()
            gdb_manager.stop()
            variable_tracker.stop()
            return [content_success({"message": "Debug session stopped"})]

        elif name == "flash_firmware":
            resp = gdb_client.load_firmware(arguments["file_path"])
            return [content_success({"message": "Firmware flashed", "file_path": arguments["file_path"]}, raw_response=resp)]

        elif name == "reset_target":
            halt = arguments["halt"]
            profile = debug_profile.get()
            reset_config = profile.get("reset", {})
            resolved = resolve_reset_command(
                gdb_manager.server_type or profile.get("server_type"),
                halt=halt,
                strategy=arguments.get("strategy") or reset_config.get("strategy"),
                command=arguments.get("command") or reset_config.get("command"),
            )
            resp = gdb_client.reset_halt(command=resolved["command"])
            return [content_success({"message": "Target reset", "reset": resolved}, raw_response=resp)]

        elif name == "set_breakpoint":
            resp = gdb_client.set_breakpoint(arguments["location"])
            return [content_success({"message": "Breakpoint set", "location": arguments["location"]}, raw_response=resp)]

        elif name == "delete_breakpoint":
            resp = gdb_client.delete_breakpoint(arguments["breakpoint_id"])
            return [content_success(
                {"message": "Breakpoint deleted", "breakpoint_id": arguments["breakpoint_id"]},
                raw_response=resp,
            )]

        elif name == "continue_execution":
            resp = gdb_client.continue_execution()
            return [content_success({"message": "Execution continued"}, raw_response=resp)]

        elif name == "halt_execution":
            resp = gdb_client.halt_execution()
            return [content_success({"message": "Execution halted"}, raw_response=resp)]

        elif name == "step_over":
            resp = gdb_client.step_over()
            return [content_success({"message": "Stepped over"}, raw_response=resp)]

        elif name == "step_into":
            resp = gdb_client.step_into()
            return [content_success({"message": "Stepped into"}, raw_response=resp)]

        elif name == "read_variable":
            resp = gdb_client.read_variable(arguments["name"])
            return [content_success({"message": "Variable read", "name": arguments["name"]}, raw_response=resp)]

        elif name == "read_memory":
            resp = gdb_client.read_memory(arguments["address"], arguments["length"])
            return [content_success(
                {"message": "Memory read", "address": arguments["address"], "length": arguments["length"]},
                raw_response=resp,
            )]

        elif name == "write_memory":
            resp = gdb_client.write_memory(arguments["address"], arguments["value"])
            return [content_success(
                {"message": "Memory written", "address": arguments["address"], "value": arguments["value"]},
                raw_response=resp,
            )]

        elif name == "get_gdb_events":
            resp = gdb_client.get_responses()
            return [content_success({"events": resp, "message": "GDB events read" if resp else "No new events"})]

        elif name == "get_gdb_server_logs":
            logs = gdb_manager.get_logs()
            return [content_success({"logs": logs, "message": "GDB server logs captured" if logs else "No GDB server logs captured"})]

        elif name == "read_call_stack":
            resp = gdb_client.read_call_stack()
            return [content_success({"message": "Call stack read"}, raw_response=resp)]

        elif name == "read_core_registers":
            resp = gdb_client.read_core_registers()
            return [content_success({"message": "Core registers read"}, raw_response=resp)]

        elif name == "read_fault_registers":
            resp = gdb_client.read_fault_registers()
            hex_resp = {key: f"0x{value & 0xFFFFFFFF:08x}" for key, value in resp.items()}
            return [content_success(hex_resp, raw_response=resp)]

        elif name == "diagnose_fault":
            resp = gdb_client.read_fault_registers()
            diagnosis = diagnose_fault_registers(resp)
            return [content_success(diagnosis, raw_response=resp)]

        elif name == "capture_debug_snapshot":
            profile = debug_profile.get()
            project_context = None
            rtos_context = None
            log_context = None
            if arguments.get("include_project"):
                project_context = inspect_project(arguments.get("project_root") or profile.get("project_root"), profile)
            if arguments.get("include_rtos"):
                rtos_context = freertos_inspector.capture_snapshot()
            if arguments.get("include_logs"):
                log_context = {
                    "rtt": {
                        "status": rtt_log_reader.status(),
                        "entries": rtt_log_reader.get_logs(limit=arguments.get("log_limit", 200)),
                    },
                    "uart": {
                        "status": uart_log_reader.status(),
                        "entries": uart_log_reader.get_logs(limit=arguments.get("log_limit", 200)),
                    },
                }
            snapshot = collect_debug_snapshot(
                gdb_client,
                gdb_manager,
                project_context=project_context,
                rtos_context=rtos_context,
                log_context=log_context,
            )
            return [content_success(snapshot)]

        elif name == "inspect_project":
            profile = debug_profile.get()
            result = inspect_project(arguments.get("project_root") or profile.get("project_root"), profile)
            return [content_success(result)]

        elif name == "detect_rtos":
            result = freertos_inspector.detect()
            return [content_success(result)]

        elif name == "read_current_task":
            result = freertos_inspector.read_current_task()
            return [content_success(result)]

        elif name == "read_freertos_tasks":
            result = freertos_inspector.read_tasks(
                max_priorities=arguments.get("max_priorities"),
                max_tasks=arguments.get("max_tasks", 64),
            )
            return [content_success(result)]

        elif name == "read_freertos_task_lists":
            result = freertos_inspector.read_task_lists(
                max_priorities=arguments.get("max_priorities"),
                max_tasks=arguments.get("max_tasks", 128),
            )
            return [content_success(result)]

        elif name == "read_freertos_queue":
            result = freertos_inspector.read_queue(arguments["queue"])
            return [content_success(result)]

        elif name == "read_freertos_mutex":
            result = freertos_inspector.read_mutex(arguments["mutex"])
            return [content_success(result)]

        elif name == "read_freertos_heap":
            result = freertos_inspector.read_heap()
            return [content_success(result)]

        elif name == "capture_rtos_snapshot":
            result = freertos_inspector.capture_snapshot()
            return [content_success(result)]

        elif name == "start_rtt_logging":
            command = [arguments.get("command", "JLinkRTTClient")]
            command.extend(arguments.get("args", []))
            rtt_log_reader.start(command)
            return [content_success(rtt_log_reader.status())]

        elif name == "stop_rtt_logging":
            rtt_log_reader.stop()
            return [content_success(rtt_log_reader.status())]

        elif name == "get_rtt_logs":
            result = {
                "status": rtt_log_reader.status(),
                "entries": rtt_log_reader.get_logs(
                    limit=arguments.get("limit"),
                    since_index=arguments.get("since_index"),
                    clear=arguments.get("clear", False),
                ),
            }
            return [content_success(result)]

        elif name == "clear_rtt_logs":
            rtt_log_reader.clear()
            return [content_success({"message": "RTT log buffer cleared"})]

        elif name == "start_uart_logging":
            uart_log_reader.start(
                port=arguments["port"],
                baudrate=arguments.get("baudrate", 115200),
                timeout=arguments.get("timeout", 0.1),
            )
            return [content_success(uart_log_reader.status())]

        elif name == "stop_uart_logging":
            uart_log_reader.stop()
            return [content_success(uart_log_reader.status())]

        elif name == "get_uart_logs":
            result = {
                "status": uart_log_reader.status(),
                "entries": uart_log_reader.get_logs(
                    limit=arguments.get("limit"),
                    since_index=arguments.get("since_index"),
                    clear=arguments.get("clear", False),
                ),
            }
            return [content_success(result)]

        elif name == "clear_uart_logs":
            uart_log_reader.clear()
            return [content_success({"message": "UART log buffer cleared"})]

        elif name == "capture_expressions":
            result = run_expression_capture(gdb_client, arguments["expressions"])
            return [content_success(result)]

        elif name == "assert_expressions":
            result = run_expression_assertions(gdb_client, arguments["assertions"])
            return [content_success(result)]

        elif name == "compare_expressions_after_action":
            action_name = arguments["action"]
            action_map = {
                "step_over": gdb_client.step_over,
                "step_into": gdb_client.step_into,
                "continue": gdb_client.continue_execution,
                "halt": gdb_client.halt_execution,
                "reset_halt": lambda: gdb_client.reset_halt("monitor reset halt"),
            }
            action = action_map[action_name]
            result = compare_expressions_after_action(gdb_client, arguments["expressions"], action_name, action)
            return [content_success(result)]

        elif name == "set_watchpoint":
            resp = gdb_client.set_watchpoint(arguments["location"], arguments["access_type"])
            return [content_success(
                {"message": "Watchpoint set", "location": arguments["location"], "access_type": arguments["access_type"]},
                raw_response=resp,
            )]

        elif name == "load_svd":
            svd_parser.load(arguments["filepath"])
            return [content_success({"message": "SVD file loaded successfully", "filepath": arguments["filepath"]})]

        elif name == "read_peripheral_register":
            addr = svd_parser.get_register_address(arguments["peripheral"], arguments["register"])
            resp = gdb_client.read_memory(hex(addr), 4)  # Assuming 32-bit register
            return [content_success(
                {
                    "message": "Peripheral register read",
                    "peripheral": arguments["peripheral"],
                    "register": arguments["register"],
                    "address": hex(addr),
                },
                raw_response=resp,
            )]

        elif name == "decode_peripheral_register":
            register = svd_parser.get_register(arguments["peripheral"], arguments["register"])
            resp = gdb_client.read_typed_memory(hex(register["address_int"]), width_bits=register["size"], count=1)
            value = gdb_client._extract_first_memory_word(resp)
            decoded = svd_parser.decode_register_value(arguments["peripheral"], arguments["register"], value)
            decoded["raw_response"] = resp
            return [content_success(decoded, raw_response=resp)]

        elif name == "read_typed_memory":
            resp = gdb_client.read_typed_memory(arguments["address"], arguments["width_bits"], arguments["count"])
            return [content_success(
                {
                    "message": "Typed memory read",
                    "address": arguments["address"],
                    "width_bits": arguments["width_bits"],
                    "count": arguments["count"],
                },
                raw_response=resp,
            )]

        elif name == "write_typed_memory":
            resp = gdb_client.write_typed_memory(arguments["address"], arguments["value"], arguments["width_bits"])
            return [content_success(
                {
                    "message": "Typed memory written",
                    "address": arguments["address"],
                    "value": arguments["value"],
                    "width_bits": arguments["width_bits"],
                },
                raw_response=resp,
            )]

        elif name == "set_debug_profile":
            profile = debug_profile.update(arguments)
            svd_path = profile.get("svd_path")
            if svd_path:
                svd_parser.load(svd_path)
            return [content_success(profile)]

        elif name == "get_debug_profile":
            return [content_success(debug_profile.get())]

        elif name == "load_debug_config":
            result = load_debug_config_file(arguments["path"])
            if result["validation"]["valid"]:
                debug_profile.update({
                    key: value
                    for key, value in result["config"].items()
                    if key in debug_profile.ALLOWED_FIELDS
                })
                svd_path = result["config"].get("svd_path")
                if svd_path:
                    svd_parser.load(svd_path)
            return [content_success(result)]

        elif name == "save_debug_config":
            result = save_debug_config_file(arguments["path"], arguments["config"])
            return [content_success(result)]

        elif name == "validate_debug_config":
            result = validate_debug_config_data(arguments["config"])
            return [content_success(result)]

        elif name == "start_variable_tracking":
            variable_tracker.start(arguments["variable"], arguments["interval_ms"])
            return [content_success(
                {
                    "message": "Variable tracking started",
                    "variable": arguments["variable"],
                    "interval_ms": arguments["interval_ms"],
                }
            )]

        elif name == "stop_variable_tracking":
            variable_tracker.stop()
            return [content_success({"message": "Tracking stopped"})]

        elif name == "get_tracked_data":
            data = variable_tracker.get_data()
            return [content_success(data)]

        else:
            raise ValueError(f"Unknown tool: {name}")

    except Exception as e:
        return [content_error(str(e), code="tool_execution_error", suggested_next_actions=["capture_debug_snapshot"])]

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())
