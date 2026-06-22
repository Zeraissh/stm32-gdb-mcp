import asyncio
import json
import logging
import sys
import time

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .composites import capture_state, debug_until, flash_and_run
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
from .debug_freeze import plan_freeze_writes, resolve_freeze_targets, supported_families
from .debug_profile import DebugProfileStore
from .debug_report import build_report, write_report
from .debug_snapshot import collect_debug_snapshot
from .error_taxonomy import classify_error
from .exception_frame import build_fault_context
from .fault_analysis import diagnose_fault_registers
from .freertos_inspector import FreeRTOSInspector
from .gdb_client import GdbClientManager
from .gdb_decode import registers_summary
from .gdb_manager import GdbServerManager
from .log_reader import ProcessLogReader, SerialLogReader
from .memory_guard import MemoryWriteGuard
from .metrics import compute_metrics
from .project_inspector import inspect_project
from .reliability import retry_call
from .reset_strategy import resolve_reset_command
from .scenario import load_scenario, replay_scenario, step_summary
from .self_check import evaluate_self_check
from .session_journal import SessionJournal
from .svd_parser import SVDParser
from .tool_response import content_error, content_success
from .tracker import VariableTracker

SERVER_INSTRUCTIONS = """\
STM32 on-chip debugging over GDB + OpenOCD/ST-Link/J-Link. Drive it as a loop:
observe -> orient (symbolize) -> hypothesize -> act safely -> verify.

Core workflow:
1. start_debug_session, then ALWAYS run self_check first — it validates byte order,
   the Cortex-M core, and the device family, catching link/config faults early.
2. Optionally set_debug_profile (mcu, elf_path, svd_path) so symbols/peripherals resolve.
3. Reproduce with the fewest calls: prefer the composites over manual sequences —
   flash_and_run (ELF -> halted at entry), debug_until (conditional breakpoint + run +
   decoded backtrace/locals in one call), capture_state ("where am I" in one call).
4. Diagnose a crash with reconstruct_fault_context: it unwinds the stacked exception
   frame and resolves the true faulting PC to source file:line.
5. Verify a fix with compare_expressions_after_action / assert_expressions.

Key rules (the target must cooperate):
- Reads (registers/memory/frames) require a HALTED core. If a read fails with
  target_unresponsive, the core is running — call halt_execution first.
- run_and_wait returns a structured stop event; on timeout it leaves the core RUNNING.
- Memory writes are guarded: option bytes, IWDG, and WWDG are blocked by default;
  use set_write_policy to allow, or dry_run to simulate. Every write is audited.
- If halting causes mysterious resets, configure_debug_freeze (freeze IWDG/WWDG/timers).
- On probe_unavailable / connection_lost, call recover_session; tune flaky probes with
  set_timeouts.

Determinism & sharing: every call is journaled (get_session_journal / get_session_timeline
/ get_session_metrics). Replay a repro with run_scenario; bundle a full, shareable report
with export_debug_report. Most results carry suggested_next_actions — follow them.
"""

server = Server("stm32-gdb-mcp", instructions=SERVER_INSTRUCTIONS)
gdb_manager = GdbServerManager()
gdb_client = GdbClientManager()
svd_parser = SVDParser()
variable_tracker = VariableTracker(gdb_client)
debug_profile = DebugProfileStore()
freertos_inspector = FreeRTOSInspector(gdb_client)
rtt_log_reader = ProcessLogReader("rtt")
swo_log_reader = ProcessLogReader("swo")
uart_log_reader = SerialLogReader()
memory_guard = MemoryWriteGuard()
session_journal = SessionJournal()
_last_session = {"server_type": None, "server_args": []}

# Structured logging to stderr (stdout is the MCP transport), correlated by run-id.
logger = logging.getLogger("stm32-gdb-mcp")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)

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
            name="self_check",
            description="Validates the link right after connecting: reads CPUID and DBGMCU IDCODE "
                        "and checks byte order, that a real Cortex-M is present, and that the device "
                        "matches the expected family (from the profile MCU or the 'expected_family' "
                        "arg). Run this first to catch endianness/config faults before debugging.",
            inputSchema={
                "type": "object",
                "properties": {
                    "expected_family": {"type": "string", "description": "Expected MCU/family, e.g. 'STM32L431'. Defaults to the profile MCU."}
                }
            }
        ),
        Tool(
            name="recover_session",
            description="Recovers a dropped or wedged session: cleanly tears down the GDB client and "
                        "server, then restarts the server (with retry/backoff for a busy probe) using "
                        "the last start_debug_session arguments and reconnects. Use after a "
                        "probe_unavailable or connection_lost error.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="check_session_health",
            description="Reports whether the GDB client, GDB server process, and target are still "
                        "alive and responsive. With reconnect=true, attempts to restart the GDB "
                        "client and reconnect to the running server. Use this on long autonomous "
                        "runs to detect a dropped session before it derails debugging.",
            inputSchema={
                "type": "object",
                "properties": {
                    "reconnect": {"type": "boolean", "description": "If true, try to reconnect the GDB client to the running server."}
                }
            }
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
            description="Sets a breakpoint at a function, line, or address. Supports an optional "
                        "condition (break only when true), temporary (auto-delete on first hit), "
                        "and ignore_count (skip N hits) so the AI can set a hypothesis trap and resume.",
            inputSchema={
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "Location to break at, e.g., 'main', 'main.c:42', or '*0x08001000'."},
                    "condition": {"type": "string", "description": "Optional C expression; break only when it is non-zero, e.g. 'count > 5'."},
                    "temporary": {"type": "boolean", "description": "If true, the breakpoint is deleted after its first hit."},
                    "ignore_count": {"type": "integer", "description": "Number of hits to ignore before stopping."}
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
            name="run_and_wait",
            description="Resumes the target and waits, returning a structured stop event "
                        "(reason, symbolized frame, breakpoint id, signal) or a timeout. "
                        "Use this instead of continue_execution + polling to close the debug loop.",
            inputSchema={
                "type": "object",
                "properties": {
                    "timeout_sec": {"type": "number", "description": "Max seconds to wait for a stop (default 10)."}
                }
            }
        ),
        Tool(
            name="debug_until",
            description="One-call repro step: set an optional conditional/temporary breakpoint at "
                        "a location, run, and return the stop event PLUS the decoded backtrace and "
                        "innermost-frame locals. Collapses set_breakpoint + run + read frame/vars "
                        "into a single round-trip.",
            inputSchema={
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "Where to break, e.g. 'trigger_divzero' or 'main.c:21'."},
                    "condition": {"type": "string", "description": "Optional C condition, e.g. 'g_divisor == 0'."},
                    "temporary": {"type": "boolean", "description": "Auto-delete the breakpoint after the first hit (default true)."},
                    "ignore_count": {"type": "integer", "description": "Hits to ignore before stopping."},
                    "timeout_sec": {"type": "number", "description": "Max seconds to wait (default 10)."}
                },
                "required": ["location"]
            }
        ),
        Tool(
            name="capture_state",
            description="One-call 'where am I': decoded core registers + a PC/LR/SP summary, the "
                        "decoded backtrace, and the innermost-frame locals. The fastest way to get "
                        "full halted context.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="flash_and_run",
            description="One-call bring-up: flash an ELF (loads symbols), reset-halt, set a "
                        "temporary breakpoint at an entry point (default 'main'), run to it, and "
                        "return the decoded stop context.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Path to the ELF to flash."},
                    "run_to": {"type": "string", "description": "Entry point to stop at (default 'main')."},
                    "timeout_sec": {"type": "number", "description": "Max seconds to wait (default 10)."}
                },
                "required": ["file_path"]
            }
        ),
        Tool(
            name="wait_for_stop",
            description="Waits for the next stop event WITHOUT resuming the target, returning "
                        "a structured stop event or a timeout.",
            inputSchema={
                "type": "object",
                "properties": {
                    "timeout_sec": {"type": "number", "description": "Max seconds to wait for a stop (default 10)."}
                }
            }
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
            name="set_write_policy",
            description="Configures memory-write guardrails: mode ('enforce' or 'dry_run'), and "
                        "optional allow/protected regions. Protected regions (option bytes, IWDG, "
                        "WWDG) block writes by default; dry_run simulates every write.",
            inputSchema={
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["enforce", "dry_run"], "description": "Guard mode."},
                    "add_allow": {
                        "type": "array",
                        "description": "Regions to explicitly allow, overriding protection.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "start": {"type": "integer"},
                                "end": {"type": "integer"}
                            }
                        }
                    },
                    "add_protected": {
                        "type": "array",
                        "description": "Additional regions to protect from writes.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "start": {"type": "integer"},
                                "end": {"type": "integer"}
                            }
                        }
                    }
                }
            }
        ),
        Tool(
            name="get_write_audit_log",
            description="Returns the append-only audit log of every memory-write decision "
                        "(written, blocked, or simulated).",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Return only the most recent N entries."}
                }
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
            description="Reads the call stack as a decoded list of frames "
                        "{level, func, file, line, addr} plus a one-line summary. "
                        "Set include_raw=true to also get the raw GDB output.",
            inputSchema={
                "type": "object",
                "properties": {
                    "include_raw": {"type": "boolean", "description": "Include the raw GDB transcript (default false)."}
                }
            }
        ),
        Tool(
            name="read_core_registers",
            description="Reads CPU core registers as a decoded {name: hex} map plus a one-line "
                        "summary of PC/LR/SP. Set include_raw=true to also get the raw GDB output.",
            inputSchema={
                "type": "object",
                "properties": {
                    "include_raw": {"type": "boolean", "description": "Include the raw GDB transcript (default false)."}
                }
            }
        ),
        Tool(
            name="select_frame",
            description="Selects a stack frame by level (0 = innermost) for subsequent variable reads.",
            inputSchema={
                "type": "object",
                "properties": {
                    "level": {"type": "integer", "description": "Frame level, 0 is the innermost/current frame."}
                },
                "required": ["level"]
            }
        ),
        Tool(
            name="read_frame_variables",
            description="Returns a decoded {name: value} map of locals and arguments for a stack "
                        "frame, plus a count summary. Set include_raw=true for the raw GDB output.",
            inputSchema={
                "type": "object",
                "properties": {
                    "level": {"type": "integer", "description": "Optional frame level to select first (0 = innermost)."},
                    "include_raw": {"type": "boolean", "description": "Include the raw GDB transcript (default false)."}
                }
            }
        ),
        Tool(
            name="list_source",
            description="Lists source lines around a location (function, 'file.c:42', or '*0xADDR').",
            inputSchema={
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "Where to list around. Omit to continue from current."},
                    "count": {"type": "integer", "description": "Approximate number of lines (default 10)."}
                }
            }
        ),
        Tool(
            name="resolve_address",
            description="Maps an address or expression (e.g. '$pc', '0x08001234') to its source "
                        "file:line and nearest symbol.",
            inputSchema={
                "type": "object",
                "properties": {
                    "expr": {"type": "string", "description": "Address or expression to resolve, e.g. '$pc' or '0x08001234'."}
                },
                "required": ["expr"]
            }
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
            name="configure_debug_freeze",
            description="Freezes peripherals (IWDG, WWDG, RTC, timers) while the core is halted via "
                        "the DBGMCU freeze registers, so the watchdog cannot reset the target out "
                        "from under the debugger. Family is taken from the debug profile MCU if omitted.",
            inputSchema={
                "type": "object",
                "properties": {
                    "peripherals": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Peripherals to freeze, e.g. ['iwdg', 'wwdg', 'rtc', 'tim2']."
                    },
                    "family": {"type": "string", "description": f"STM32 family or part number. Known: {supported_families()}."},
                    "apply": {"type": "boolean", "description": "If false, only return the planned register writes (default true)."}
                },
                "required": ["peripherals"]
            }
        ),
        Tool(
            name="reconstruct_fault_context",
            description="Reconstructs the full crash site after a HardFault: decodes fault "
                        "registers, unwinds the auto-stacked exception frame from MSP/PSP via "
                        "EXC_RETURN to recover the true faulting PC, and resolves it to source "
                        "file:line. Run this when halted in a fault handler.",
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
            name="start_swo_logging",
            description="Starts background SWO/ITM log capture using a caller-provided decoder command.",
            inputSchema={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Executable to launch for SWO/ITM decoding."},
                    "args": {"type": "array", "items": {"type": "string"}, "description": "Command arguments."}
                },
                "required": ["command"]
            }
        ),
        Tool(
            name="stop_swo_logging",
            description="Stops the background SWO/ITM log capture process.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="get_swo_logs",
            description="Returns captured SWO/ITM log lines.",
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
            name="clear_swo_logs",
            description="Clears buffered SWO/ITM log lines without stopping capture.",
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
        ),
        # --- Phase 2: determinism (journal + replayable scenarios) ---
        Tool(
            name="get_session_journal",
            description="Returns the append-only journal of every tool call this session "
                        "(sequence, timestamp, tool, args, ok, summary, duration) for reproducibility.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Return only the most recent N entries."}
                }
            }
        ),
        Tool(
            name="clear_session_journal",
            description="Clears the session journal (keeps the run-id).",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="get_timeouts",
            description="Returns the current named GDB operation timeouts (connect, reset, memory, "
                        "registers, source, run, download).",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="set_timeouts",
            description="Overrides one or more named timeouts (positive seconds). Useful for a slow "
                        "or flaky probe. Recorded in the journal so replays are deterministic.",
            inputSchema={
                "type": "object",
                "properties": {
                    "overrides": {
                        "type": "object",
                        "description": "Map of timeout name -> seconds, e.g. {\"memory\": 4.0, \"connect\": 8.0}."
                    }
                },
                "required": ["overrides"]
            }
        ),
        Tool(
            name="get_session_timeline",
            description="Returns a compact, human-readable timeline of every tool call this session "
                        "(built on the journal) for a quick replay of what happened.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="get_session_metrics",
            description="Returns per-tool metrics for this session: call counts, success/failure, "
                        "and average/total duration, plus totals.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="export_debug_report",
            description="Writes a single self-contained JSON report (journal + metrics + profile, "
                        "optionally a state snapshot and a coredump) tied to the run-id, so a bug "
                        "session is fully reproducible and shareable from one artifact.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Output path for the report JSON."},
                    "include_snapshot": {"type": "boolean", "description": "Capture and embed a debug snapshot (requires a halted target)."},
                    "coredump_path": {"type": "string", "description": "If set, capture a coredump there and reference it in the report."}
                },
                "required": ["path"]
            }
        ),
        Tool(
            name="run_scenario",
            description="Replays a declarative scenario — a list of {tool, args} steps — "
                        "deterministically and returns a per-step pass/fail report. Provide inline "
                        "'steps' or a 'path' to a JSON scenario file. The minimal-step way to "
                        "re-run a complex bug repro.",
            inputSchema={
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "description": "Ordered steps, each {tool, args}.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "tool": {"type": "string"},
                                "args": {"type": "object"}
                            },
                            "required": ["tool"]
                        }
                    },
                    "path": {"type": "string", "description": "Path to a JSON scenario file (alternative to inline steps)."},
                    "stop_on_failure": {"type": "boolean", "description": "Stop at the first failing step (default true)."}
                }
            }
        ),
        # --- Tier 3: Execution control, symbol discovery, postmortem, timing ---
        Tool(
            name="step_out",
            description="Runs until the current function returns (GDB finish).",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="step_instruction",
            description="Steps a single machine instruction. Set over=true to step over calls.",
            inputSchema={
                "type": "object",
                "properties": {
                    "over": {"type": "boolean", "description": "If true, step over a called function instead of into it."}
                }
            }
        ),
        Tool(
            name="run_to_line",
            description="Runs until a given location is reached (function, 'file.c:42', or '*0xADDR').",
            inputSchema={
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "Where to run to."}
                },
                "required": ["location"]
            }
        ),
        Tool(
            name="disassemble",
            description="Disassembles N instructions at a location (default $pc).",
            inputSchema={
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "Where to disassemble from (default '$pc')."},
                    "instructions": {"type": "integer", "description": "Number of instructions (default 8)."}
                }
            }
        ),
        Tool(
            name="list_functions",
            description="Lists functions in the loaded symbols, optionally filtered by a regex.",
            inputSchema={
                "type": "object",
                "properties": {
                    "regex": {"type": "string", "description": "Optional regex to filter function names."}
                }
            }
        ),
        Tool(
            name="list_variables",
            description="Lists global/static variables in the loaded symbols, optionally filtered by a regex.",
            inputSchema={
                "type": "object",
                "properties": {
                    "regex": {"type": "string", "description": "Optional regex to filter variable names."}
                }
            }
        ),
        Tool(
            name="lookup_type",
            description="Shows the type/layout of an expression or type name (GDB ptype).",
            inputSchema={
                "type": "object",
                "properties": {
                    "expr": {"type": "string", "description": "Expression or type name, e.g. 'my_struct' or 'g_state'."}
                },
                "required": ["expr"]
            }
        ),
        Tool(
            name="sizeof",
            description="Evaluates sizeof(expr) against the loaded symbols.",
            inputSchema={
                "type": "object",
                "properties": {
                    "expr": {"type": "string", "description": "Type or expression to size, e.g. 'struct foo'."}
                },
                "required": ["expr"]
            }
        ),
        Tool(
            name="address_of",
            description="Resolves the address of a symbol (&symbol).",
            inputSchema={
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Symbol name, e.g. 'g_state'."}
                },
                "required": ["symbol"]
            }
        ),
        Tool(
            name="capture_coredump",
            description="Writes a core dump (RAM + registers) of the halted target to a file for "
                        "offline postmortem analysis.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Output path for the core file."}
                },
                "required": ["path"]
            }
        ),
        Tool(
            name="load_coredump",
            description="Loads a previously captured core file for offline analysis.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the core file."}
                },
                "required": ["path"]
            }
        ),
        Tool(
            name="verify_flash",
            description="Verifies that target flash matches an ELF by comparing loaded sections "
                        "(GDB compare-sections).",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Path to the ELF to verify against."}
                },
                "required": ["file_path"]
            }
        ),
        Tool(
            name="read_cycle_counter",
            description="Enables (if needed) and reads the DWT cycle counter (DWT_CYCCNT) for "
                        "on-chip timing measurements.",
            inputSchema={
                "type": "object",
                "properties": {
                    "enable": {"type": "boolean", "description": "If true, enable and zero the counter before reading."}
                }
            }
        ),
        Tool(
            name="sample_pc",
            description="Statistically samples the program counter via DWT_PCSR to locate hangs or "
                        "hot spots. Returns the raw PC samples.",
            inputSchema={
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "description": "Number of samples (default 64)."}
                }
            }
        )
    ]

def _stop_event_next_actions(event: dict) -> list[str]:
    """Guide the model to the natural next loop step for a stop event."""
    reason = event.get("reason")
    if reason in ("signal-received", "exited-signalled"):
        return ["diagnose_fault", "reconstruct_fault_context", "read_call_stack"]
    if reason == "timeout":
        return ["halt_execution", "get_gdb_server_logs"]
    if event.get("stopped"):
        return ["read_frame_variables", "list_source", "read_call_stack"]
    return []


async def _dispatch_tool(name: str, arguments: dict | None) -> list[TextContent]:
    if arguments is None:
        arguments = {}

    try:
        if name == "start_debug_session":
            server_type = arguments["server_type"]
            args = arguments.get("server_args", [])
            port = gdb_manager.start(server_type, args)
            gdb_client.start_gdb()
            resp = gdb_client.connect("localhost", port)
            _last_session["server_type"] = server_type
            _last_session["server_args"] = args
            return [content_success(
                {"message": "Debug session started", "server_type": server_type, "port": port},
                raw_response=resp,
            )]

        elif name == "recover_session":
            if not _last_session.get("server_type"):
                return [content_error(
                    "No prior session to recover; call start_debug_session first.",
                    code="no_session",
                    suggested_next_actions=["start_debug_session"],
                )]
            for teardown in (gdb_client.stop_gdb, gdb_manager.stop):
                try:
                    teardown()
                except Exception:
                    pass

            def _restart():
                return gdb_manager.start(_last_session["server_type"], _last_session["server_args"])

            port = retry_call(_restart, attempts=3, backoff_base=0.8)
            gdb_client.start_gdb()
            resp = gdb_client.connect("localhost", port)
            return [content_success(
                {"message": "Session recovered", "server_type": _last_session["server_type"], "port": port},
                raw_response=resp,
                suggested_next_actions=["self_check", "check_session_health"],
            )]

        elif name == "stop_debug_session":
            gdb_client.stop_gdb()
            gdb_manager.stop()
            variable_tracker.stop()
            return [content_success({"message": "Debug session stopped"})]

        elif name == "self_check":
            cpuid = gdb_client.read_word(0xE000ED00)
            dbgmcu_idcode = gdb_client.read_word(0xE0042000)
            expected = arguments.get("expected_family") or debug_profile.get().get("mcu")
            result = evaluate_self_check(cpuid, dbgmcu_idcode, expected_family=expected)
            next_actions = [] if result["ok"] else ["check_session_health", "start_debug_session"]
            return [content_success(result, suggested_next_actions=next_actions)]

        elif name == "check_session_health":
            reconnected = False
            if arguments.get("reconnect") and gdb_manager.is_alive():
                gdb_client.start_gdb()
                gdb_client.connect("localhost", gdb_manager.port)
                reconnected = True
            health = {
                "gdb_alive": gdb_client.is_alive(),
                "server_alive": gdb_manager.is_alive(),
                "target_responsive": gdb_client.probe_target(),
                "server_type": gdb_manager.server_type,
                "port": gdb_manager.port,
                "reconnected": reconnected,
            }
            next_actions = [] if health["target_responsive"] else ["start_debug_session", "get_gdb_server_logs"]
            return [content_success(health, suggested_next_actions=next_actions)]

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
            resp = gdb_client.set_breakpoint(
                arguments["location"],
                condition=arguments.get("condition"),
                temporary=arguments.get("temporary", False),
                ignore_count=arguments.get("ignore_count"),
            )
            return [content_success(
                {
                    "message": "Breakpoint set",
                    "location": arguments["location"],
                    "condition": arguments.get("condition"),
                    "temporary": arguments.get("temporary", False),
                },
                raw_response=resp,
                suggested_next_actions=["run_and_wait"],
            )]

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

        elif name == "debug_until":
            result = debug_until(
                gdb_client,
                location=arguments["location"],
                condition=arguments.get("condition"),
                temporary=arguments.get("temporary", True),
                ignore_count=arguments.get("ignore_count"),
                timeout_sec=arguments.get("timeout_sec", 10.0),
            )
            next_actions = ["capture_state", "list_source"] if result["stopped"] else ["halt_execution"]
            return [content_success(result, suggested_next_actions=next_actions)]

        elif name == "capture_state":
            return [content_success(capture_state(gdb_client), suggested_next_actions=["list_source", "disassemble"])]

        elif name == "flash_and_run":
            result = flash_and_run(
                gdb_client,
                file_path=arguments["file_path"],
                run_to=arguments.get("run_to", "main"),
                timeout_sec=arguments.get("timeout_sec", 10.0),
            )
            return [content_success(result, suggested_next_actions=["capture_state", "debug_until"])]

        elif name == "run_and_wait":
            event = gdb_client.run_and_wait(timeout_sec=arguments.get("timeout_sec", 10.0))
            raw = event.pop("raw_response", None)
            next_actions = _stop_event_next_actions(event)
            return [content_success(event, raw_response=raw, suggested_next_actions=next_actions)]

        elif name == "wait_for_stop":
            event = gdb_client.wait_for_stop(timeout_sec=arguments.get("timeout_sec", 10.0))
            raw = event.pop("raw_response", None)
            next_actions = _stop_event_next_actions(event)
            return [content_success(event, raw_response=raw, suggested_next_actions=next_actions)]

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
            address = arguments["address"]
            value = arguments["value"]
            decision = memory_guard.evaluate(int(address, 0), width_bits=32)
            memory_guard.audit("write_memory", address, value, decision)
            if decision["action"] == "blocked":
                return [content_error(
                    f"Write to {address} blocked: {decision['reason']}",
                    code="memory_write_blocked",
                    raw_response=decision,
                    suggested_next_actions=["set_write_policy", "get_write_audit_log"],
                )]
            if decision["action"] == "simulated":
                return [content_success(
                    {"message": "Memory write simulated (dry_run)", "address": address, "value": value, "guard": decision},
                )]
            resp = gdb_client.write_memory(address, value)
            return [content_success(
                {"message": "Memory written", "address": address, "value": value, "guard": decision},
                raw_response=resp,
            )]

        elif name == "set_write_policy":
            policy = memory_guard.set_policy(
                mode=arguments.get("mode"),
                add_allow=arguments.get("add_allow"),
                add_protected=arguments.get("add_protected"),
            )
            return [content_success({"message": "Write policy updated", "policy": policy})]

        elif name == "get_write_audit_log":
            log = memory_guard.get_audit_log(limit=arguments.get("limit"))
            return [content_success({"audit_log": log, "count": len(log)})]

        elif name == "get_gdb_events":
            resp = gdb_client.get_responses()
            return [content_success({"events": resp, "message": "GDB events read" if resp else "No new events"})]

        elif name == "get_gdb_server_logs":
            logs = gdb_manager.get_logs()
            return [content_success({"logs": logs, "message": "GDB server logs captured" if logs else "No GDB server logs captured"})]

        elif name == "read_call_stack":
            frames = gdb_client.read_call_stack_decoded()
            if frames:
                top = frames[0]
                summary = f"{len(frames)} frames; top: {top['func']} at {top['file']}:{top['line']}"
            else:
                summary = "no frames available (target running or no symbols)"
            raw = gdb_client.read_call_stack() if arguments.get("include_raw") else None
            return [content_success(
                {"frames": frames, "summary": summary},
                raw_response=raw,
                suggested_next_actions=["read_frame_variables", "list_source"],
            )]

        elif name == "read_core_registers":
            registers = gdb_client.read_core_registers_decoded()
            raw = gdb_client.read_core_registers() if arguments.get("include_raw") else None
            return [content_success(
                {"registers": registers, "summary": registers_summary(registers)},
                raw_response=raw,
            )]

        elif name == "select_frame":
            resp = gdb_client.select_frame(arguments["level"])
            return [content_success({"message": "Frame selected", "level": arguments["level"]}, raw_response=resp)]

        elif name == "read_frame_variables":
            variables = gdb_client.read_frame_variables_decoded(arguments.get("level"))
            raw = gdb_client.read_frame_variables(arguments.get("level")) if arguments.get("include_raw") else None
            return [content_success(
                {
                    "level": arguments.get("level"),
                    "variables": variables,
                    "summary": f"{len(variables)} variables in scope",
                },
                raw_response=raw,
                suggested_next_actions=["list_source", "read_variable"],
            )]

        elif name == "list_source":
            resp = gdb_client.list_source(arguments.get("location"), arguments.get("count", 10))
            return [content_success(
                {"message": "Source listed", "location": arguments.get("location")},
                raw_response=resp,
            )]

        elif name == "resolve_address":
            resp = gdb_client.resolve_address(arguments["expr"])
            return [content_success(
                {"message": "Address resolved", "expr": arguments["expr"]},
                raw_response=resp,
                suggested_next_actions=["list_source", "read_frame_variables"],
            )]

        elif name == "read_fault_registers":
            resp = gdb_client.read_fault_registers()
            hex_resp = {key: f"0x{value & 0xFFFFFFFF:08x}" for key, value in resp.items()}
            return [content_success(hex_resp, raw_response=resp)]

        elif name == "diagnose_fault":
            resp = gdb_client.read_fault_registers()
            diagnosis = diagnose_fault_registers(resp)
            return [content_success(diagnosis, raw_response=resp)]

        elif name == "configure_debug_freeze":
            family = arguments.get("family") or debug_profile.get().get("mcu")
            if not family:
                return [content_error(
                    "No STM32 family given and no MCU in the debug profile.",
                    code="missing_family",
                    suggested_next_actions=["set_debug_profile"],
                )]
            targets = resolve_freeze_targets(family, arguments["peripherals"])
            plans = plan_freeze_writes(targets, gdb_client.read_word)
            applied = arguments.get("apply", True)
            if applied:
                for plan in plans:
                    gdb_client.write_memory(hex(plan["address"]), hex(plan["new_value"]))
            return [content_success({
                "message": "Debug freeze applied" if applied else "Debug freeze planned (not applied)",
                "family": family,
                "applied": applied,
                "plans": plans,
            })]

        elif name == "reconstruct_fault_context":
            context = build_fault_context(gdb_client)
            return [content_success(
                context,
                suggested_next_actions=["list_source", "read_frame_variables", "read_call_stack"],
            )]

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
                    "swo": {
                        "status": swo_log_reader.status(),
                        "entries": swo_log_reader.get_logs(limit=arguments.get("log_limit", 200)),
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

        elif name == "start_swo_logging":
            command = [arguments["command"]]
            command.extend(arguments.get("args", []))
            swo_log_reader.start(command)
            return [content_success(swo_log_reader.status())]

        elif name == "stop_swo_logging":
            swo_log_reader.stop()
            return [content_success(swo_log_reader.status())]

        elif name == "get_swo_logs":
            result = {
                "status": swo_log_reader.status(),
                "entries": swo_log_reader.get_logs(
                    limit=arguments.get("limit"),
                    since_index=arguments.get("since_index"),
                    clear=arguments.get("clear", False),
                ),
            }
            return [content_success(result)]

        elif name == "clear_swo_logs":
            swo_log_reader.clear()
            return [content_success({"message": "SWO log buffer cleared"})]

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

        elif name == "get_session_journal":
            entries = session_journal.get(limit=arguments.get("limit"))
            return [content_success({"run_id": session_journal.run_id, "count": len(entries), "entries": entries})]

        elif name == "clear_session_journal":
            session_journal.clear()
            return [content_success({"message": "Session journal cleared", "run_id": session_journal.run_id})]

        elif name == "get_timeouts":
            return [content_success({"timeouts": gdb_client.timeouts.as_dict()})]

        elif name == "set_timeouts":
            updated = gdb_client.timeouts.set(arguments["overrides"])
            return [content_success({"message": "Timeouts updated", "timeouts": updated})]

        elif name == "get_session_timeline":
            return [content_success({"run_id": session_journal.run_id, "timeline": session_journal.timeline()})]

        elif name == "get_session_metrics":
            metrics = compute_metrics(session_journal.get())
            return [content_success({"run_id": session_journal.run_id, **metrics})]

        elif name == "export_debug_report":
            snapshot = None
            if arguments.get("include_snapshot"):
                snapshot = collect_debug_snapshot(gdb_client, gdb_manager)
            coredump_path = arguments.get("coredump_path")
            if coredump_path:
                gdb_client.capture_coredump(coredump_path)
            report = build_report(
                run_id=session_journal.run_id,
                journal_entries=session_journal.get(),
                profile=debug_profile.get(),
                snapshot=snapshot,
                coredump_path=coredump_path,
            )
            written = write_report(arguments["path"], report)
            return [content_success({
                "message": "Debug report exported",
                "path": written,
                "run_id": session_journal.run_id,
                "entries": len(report["journal"]),
                "included_snapshot": snapshot is not None,
                "coredump": coredump_path,
            })]

        elif name == "step_out":
            resp = gdb_client.step_out()
            return [content_success({"message": "Stepped out"}, raw_response=resp)]

        elif name == "step_instruction":
            resp = gdb_client.step_instruction(over=arguments.get("over", False))
            return [content_success({"message": "Stepped one instruction"}, raw_response=resp)]

        elif name == "run_to_line":
            resp = gdb_client.run_to_line(arguments["location"])
            return [content_success(
                {"message": "Ran to location", "location": arguments["location"]},
                raw_response=resp,
            )]

        elif name == "disassemble":
            resp = gdb_client.disassemble(arguments.get("location", "$pc"), arguments.get("instructions", 8))
            return [content_success({"message": "Disassembled"}, raw_response=resp)]

        elif name == "list_functions":
            resp = gdb_client.list_functions(arguments.get("regex"))
            return [content_success({"message": "Functions listed"}, raw_response=resp)]

        elif name == "list_variables":
            resp = gdb_client.list_variables(arguments.get("regex"))
            return [content_success({"message": "Variables listed"}, raw_response=resp)]

        elif name == "lookup_type":
            resp = gdb_client.lookup_type(arguments["expr"])
            return [content_success({"message": "Type looked up", "expr": arguments["expr"]}, raw_response=resp)]

        elif name == "sizeof":
            resp = gdb_client.sizeof(arguments["expr"])
            return [content_success({"message": "Size evaluated", "expr": arguments["expr"]}, raw_response=resp)]

        elif name == "address_of":
            resp = gdb_client.address_of(arguments["symbol"])
            return [content_success({"message": "Address resolved", "symbol": arguments["symbol"]}, raw_response=resp)]

        elif name == "capture_coredump":
            resp = gdb_client.capture_coredump(arguments["path"])
            return [content_success(
                {"message": "Core dump captured", "path": arguments["path"]},
                raw_response=resp,
                suggested_next_actions=["load_coredump"],
            )]

        elif name == "load_coredump":
            resp = gdb_client.load_coredump(arguments["path"])
            return [content_success({"message": "Core dump loaded", "path": arguments["path"]}, raw_response=resp)]

        elif name == "verify_flash":
            resp = gdb_client.verify_flash(arguments["file_path"])
            return [content_success(
                {"message": "Flash verified", "file_path": arguments["file_path"]},
                raw_response=resp,
            )]

        elif name == "read_cycle_counter":
            if arguments.get("enable"):
                gdb_client.enable_cycle_counter()
            cycles = gdb_client.read_cycle_counter()
            return [content_success({"message": "Cycle counter read", "cycles": cycles})]

        elif name == "sample_pc":
            samples = gdb_client.sample_pc(arguments.get("count", 64))
            return [content_success({
                "message": "PC sampled",
                "count": len(samples),
                "samples": [f"0x{s & 0xFFFFFFFF:08x}" for s in samples],
            })]

        else:
            raise ValueError(f"Unknown tool: {name}")

    except Exception as e:
        classification = classify_error(str(e))
        message = str(e)
        if classification.get("hint"):
            message = f"{message} — {classification['hint']}"
        return [content_error(
            message,
            code=classification["code"],
            raw_response={"retryable": classification["retryable"]},
            suggested_next_actions=classification["suggested_next_actions"],
        )]


# Meta tools that operate on the journal itself are not journaled (avoids noise/recursion).
_JOURNAL_SKIP = {"get_session_journal", "clear_session_journal", "get_session_timeline", "get_session_metrics"}


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict | None) -> list[TextContent]:
    if arguments is None:
        arguments = {}

    if name == "run_scenario":
        return await _run_scenario(arguments)

    start = time.monotonic()
    result = await _dispatch_tool(name, arguments)
    duration_ms = round((time.monotonic() - start) * 1000, 1)

    if name not in _JOURNAL_SKIP:
        try:
            payload = json.loads(result[0].text)
            ok = payload.get("ok")
            session_journal.record(
                name,
                arguments,
                ok=ok,
                summary=step_summary(payload),
                error=(payload.get("error") if not ok else None),
                duration_ms=duration_ms,
            )
            logger.info("[%s] %s ok=%s %sms", session_journal.run_id, name, ok, duration_ms)
        except (ValueError, IndexError, AttributeError):
            pass

    return result


async def _run_scenario(arguments: dict) -> list[TextContent]:
    steps = arguments.get("steps")
    if not steps and arguments.get("path"):
        steps = load_scenario(arguments["path"])
    if not steps:
        return [content_error("run_scenario needs 'steps' or a 'path' to a scenario file.", code="invalid_scenario")]

    async def run_step(tool, args):
        return json.loads((await handle_call_tool(tool, args))[0].text)

    report = await replay_scenario(steps, run_step, stop_on_failure=arguments.get("stop_on_failure", True))
    return [content_success(report)]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def cli_main():
    asyncio.run(main())


if __name__ == "__main__":
    cli_main()
