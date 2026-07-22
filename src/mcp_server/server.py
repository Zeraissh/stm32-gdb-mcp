import asyncio
import copy
import json
import logging
import os
import sys
import tempfile
import threading
import time
import types

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool

from . import build as build_mod
from . import device_packs, pipeline
from .acceptance_eval import GdbAcceptanceReader, evaluate_acceptance
from .acceptance_model import summarize_acceptance, validate_acceptance_spec
from .acceptance_synth import (
    derive_acceptance_spec,
    dict_clock_resolver,
    dict_gpio_resolver,
    dict_irq_resolver,
    svd_clock_resolver,
    svd_gpio_resolver,
    svd_irq_resolver,
)
from .board_model import board_view, summarize_board
from .board_validation import load_capability_db, validate_board
from .clock_solver import resolve_profile, solve_clock_tree, summarize_clock_solution
from .composites import capture_state, debug_until, flash_and_run, run_for_duration
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
from .debug_session import SessionManager, teardown_debug_session
from .debug_snapshot import collect_debug_snapshot
from .error_taxonomy import classify_error
from .exception_frame import build_fault_context
from .fault_analysis import diagnose_fault_registers
from .framework_render import render_framework
from .framework_solver import build_framework_plan, framework_view, merge_af_maps, summarize_framework
from .freertos_inspector import FreeRTOSInspector
from .gdb_client import GdbClientManager
from .gdb_decode import decode_evaluated_value, decode_memory_bytes
from .gdb_manager import GdbServerManager
from .issue_reporter import DEFAULT_REPO, build_issue_body, file_issue, issue_fingerprint
from .log_reader import FileLogReader, ProcessLogReader, SerialLogReader
from .loop_control import loop_decision, new_loop_state, summarize_loop
from .loop_orchestrator import GdbLoopSteps, run_iteration
from .memory_guard import MemoryWriteGuard
from .metrics import compute_metrics
from .netlist_parser import load_netlist_file, parse_netlist
from .openocd_config import detect_probe, find_openocd_scripts, suggest_server_args
from .project_inspector import inspect_project
from .provenance import annotate_spec_sources
from .reliability import retry_call
from .reset_strategy import resolve_reset_command
from .scenario import load_scenario, replay_scenario, step_summary
from .self_check import evaluate_self_check
from .server_metadata import SERVER_INSTRUCTIONS
from .server_metadata import mcp_version as _mcp_version
from .session_journal import SessionJournal
from .spec_model import build_design
from .svd_parser import SVDParser
from .timer_solver import solve_timers_in_plan
from .tool_response import call_tool_result, content_error, content_success
from .tool_surface import (
    CORE_TOOLS as _CORE_TOOLS,
)
from .tool_surface import (
    MERGED as _MERGED,
)
from .tool_surface import (
    RENAMED_TOOLS as _RENAMED_TOOLS,
)
from .tool_surface import (
    advertised_tools as _advertised_tools,
)
from .tools import REGISTRY as _TOOL_REGISTRY
from .tools import TOOL_ORDER as _TOOL_ORDER
from .tools import load_all as _load_tool_modules
from .tools.context import ToolContext
from .tracker import VariableTracker

_load_tool_modules()  # populate the tool registry (schema + handler per domain module)

server = Server("stm32-gdb-mcp", instructions=SERVER_INSTRUCTIONS)
gdb_manager = GdbServerManager()
gdb_client = GdbClientManager()
svd_parser = SVDParser()
variable_tracker = VariableTracker(gdb_client)
debug_profile = DebugProfileStore()
freertos_inspector = FreeRTOSInspector(gdb_client)
rtt_log_reader = ProcessLogReader("rtt")
swo_log_reader = ProcessLogReader("swo")
swo_file_reader = FileLogReader("swo")
uart_log_reader = SerialLogReader()
memory_guard = MemoryWriteGuard()
session_journal = SessionJournal()
_last_session = {"server_type": None, "server_args": []}
_board = {"current": None}  # imported BoardDescription (netlist -> BSP model) for the default session
_acceptance = {"current": None, "last_result": None}  # loaded AcceptanceSpec + last verdict (default session)
_loop = {"current": None}  # bounded acceptance-loop state (Pillar C) for the default session
_design = {"current": None, "last_render": None}  # FrameworkPlan + last render (Pillar D) for default session
_spec = {"current": None}  # translated product spec (spec_model -> design params) for the default session
_reported_issues = {}  # fingerprint -> issue url (in-session dedup)
_tool_catalog = {}

# Phase 3: named per-target sessions for multi-board / CI. The "default" session reuses
# the module globals above (single-target back-compat + existing tests); named sessions get
# fully isolated objects from the SessionManager.
session_manager = SessionManager()
_SESSION_ATTRS = ("gdb_manager", "gdb_client", "svd_parser", "variable_tracker",
                  "debug_profile", "freertos_inspector", "rtt_log_reader", "swo_log_reader",
                  "swo_file_reader", "uart_log_reader", "memory_guard", "last_session", "board",
                  "acceptance", "loop", "design", "spec")
# Session attrs whose "default" backing global is named differently from the attribute.
_DEFAULT_SESSION_GLOBALS = {"last_session": "_last_session", "board": "_board",
                            "acceptance": "_acceptance", "loop": "_loop", "design": "_design",
                            "spec": "_spec"}


def _resolve_session(arguments: dict):
    """Return the per-target object bundle for arguments['session'] (default 'default')."""
    sid = arguments.get("session") or "default"
    if sid == "default":
        g = globals()
        return types.SimpleNamespace(id="default", **{a: g[_DEFAULT_SESSION_GLOBALS.get(a, a)]
                                                       for a in _SESSION_ATTRS})
    return session_manager.get(sid)


# One threading.Lock per session id. GDB dispatch is synchronous and blocking, and every
# session owns a single GdbController pipe, so calls targeting the SAME session must be
# serialized to avoid interleaving on that pipe; different sessions (boards) still run
# concurrently. The lock is acquired inside the dispatch worker thread (see handle_call_tool),
# so a waiting call blocks that thread — never the event loop — and it stays loop-agnostic
# (module-global asyncio.Locks would break across the many event loops the tests create).
# _lock_for_session is only ever called from the event-loop thread, so the dict needs no guard.
_session_locks: dict[str, threading.Lock] = {}


def _lock_for_session(session_id: str) -> threading.Lock:
    lock = _session_locks.get(session_id)
    if lock is None:
        lock = threading.Lock()
        _session_locks[session_id] = lock
    return lock


# Structured logging to stderr (stdout is the MCP transport), correlated by run-id.
logger = logging.getLogger("stm32-gdb-mcp")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)


def _make_context(sess) -> ToolContext:
    """Build the per-dispatch ToolContext for registered handlers.

    Reads this module's globals at CALL time so tests that monkeypatch
    mcp_server.server attributes (gdb_client, detect_probe, _dispatch_tool, ...)
    keep working — the context is a per-call view, never a cached snapshot.
    """
    g = globals()
    return ToolContext(
        session_id=sess.id,
        sess=sess,
        gdb_manager=sess.gdb_manager,
        gdb_client=sess.gdb_client,
        svd_parser=sess.svd_parser,
        variable_tracker=sess.variable_tracker,
        debug_profile=sess.debug_profile,
        freertos_inspector=sess.freertos_inspector,
        memory_guard=sess.memory_guard,
        rtt_log_reader=sess.rtt_log_reader,
        swo_log_reader=sess.swo_log_reader,
        swo_file_reader=sess.swo_file_reader,
        uart_log_reader=sess.uart_log_reader,
        last_session=sess.last_session,
        board=sess.board,
        acceptance=sess.acceptance,
        loop=sess.loop,
        design=sess.design,
        spec=sess.spec,
        session_journal=g["session_journal"],
        session_manager=g["session_manager"],
        reported_issues=g["_reported_issues"],
        tool_catalog=lambda: g["_tool_catalog"],
        logger=logger,
        fns=types.SimpleNamespace(
            detect_probe=g["detect_probe"],
            suggest_server_args=g["suggest_server_args"],
            find_openocd_scripts=g["find_openocd_scripts"],
            flash_and_run=g["flash_and_run"],
            run_for_duration=g["run_for_duration"],
            capture_state=g["capture_state"],
            debug_until=g["debug_until"],
            file_issue=g["file_issue"],
        ),
        dispatch=lambda tool_name, tool_args: g["_dispatch_tool"](tool_name, tool_args),
        default_session=lambda: _resolve_session({}),
    )


def _probe_selection(arguments: dict, profile: dict) -> tuple[str | None, str | None, dict | None, dict | None]:
    probe = arguments.get("probe")
    if probe:
        return probe, "argument", None, None
    probe = profile.get("probe")
    if probe:
        return probe, "profile", None, None

    detection = detect_probe()
    probes = detection.get("probes") or []
    if len(probes) == 1:
        return probes[0].get("type"), "detected", probes[0], detection
    return None, None, None, detection

@server.list_tools()
async def handle_list_tools() -> list[Tool]:
    global _tool_catalog
    _tools = [
        # --- Step 4: Basic Control and Flashing ---
        Tool(
            name="start_debug_session",
            description="Starts the specified GDB Server (openocd, stlink, jlink) and connects the GDB Client to it. "
                        "openocd REQUIRES server_args naming the probe and target, e.g. "
                        "['-f','interface/stlink.cfg','-f','target/stm32l4x.cfg'] — without them OpenOCD cannot "
                        "find a config or adapter. Omitted values come from the active debug profile.",
            inputSchema={
                "type": "object",
                "properties": {
                    "server_type": {"type": "string", "enum": ["openocd", "stlink", "jlink"], "description": "Type of debug server backend."},
                    "server_args": {"type": "array", "items": {"type": "string"}, "description": "Optional args for the server e.g. ['-f', 'interface/stlink.cfg', '-f', 'target/stm32f4x.cfg']"},
                    "probe": {"type": "string", "description": "Optional OpenOCD probe type: stlink, jlink, or cmsis-dap. Falls back to the profile, then a unique detected probe."},
                    "serial": {"type": "string", "description": "Probe/ST-Link serial to select a specific board (for concurrent multi-target). Auto-added as 'adapter serial <serial>'."},
                    "speed_khz": {"type": "integer", "description": "OpenOCD adapter clock used when server_args are inferred (default 4000)."}
                }
            }
        ),
        Tool(
            name="stop_debug_session",
            description="Stops the GDB client/server, variable tracking, and all active RTT/SWO/UART readers.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="self_check",
            description="Validates the link right after connecting: reads CPUID and DBGMCU IDCODE "
                        "and checks byte order, that a real Cortex-M is present, and that the device "
                        "matches the expected family (from the profile MCU or the 'expected_family' "
                        "arg). Run this first to catch endianness/config faults before debugging. "
                        "Halts the core first (identity reads are unreliable while running); pass halt=false to skip.",
            inputSchema={
                "type": "object",
                "properties": {
                    "expected_family": {"type": "string", "description": "Expected MCU/family, e.g. 'STM32L431'. Defaults to the profile MCU."},
                    "halt": {"type": "boolean", "description": "Halt the core before reading (default true)."}
                }
            }
        ),
        Tool(
            name="detect_probe",
            description="Lists physical ST-Link, J-Link, and CMSIS-DAP probes currently connected over USB. "
                        "Preserves serial numbers so multiple identical probes are never silently collapsed.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="suggest_server_args",
            description="Returns the correct OpenOCD server_args for an MCU family + probe (e.g. "
                        "STM32L431 + stlink -> ['-f','interface/stlink.cfg','-f','target/stm32l4x.cfg']) "
                        "and validates the config files exist in OpenOCD's bundled scripts dir. Call "
                        "this to get start_debug_session args — do NOT search the disk for .cfg files.",
            inputSchema={
                "type": "object",
                "properties": {
                    "mcu": {"type": "string", "description": "MCU or family, e.g. 'STM32L431' or 'STM32F4'."},
                    "probe": {"type": "string", "description": "Optional. Debug probe: stlink, jlink, or cmsis-dap. If omitted, uses debug profile probe."},
                    "speed_khz": {"type": "integer", "description": "Adapter clock in kHz (default 4000; 0 keeps the config default)."}
                },
                "required": ["mcu"]
            }
        ),
        Tool(
            name="set_adapter_speed",
            description="Sets the SWD/JTAG adapter clock (kHz) at runtime. The default ST-Link "
                        "speed is only ~480 kHz; raising it (e.g. 4000) speeds up flashing and "
                        "memory reads ~8x. Lower it if reads/flash become unreliable.",
            inputSchema={
                "type": "object",
                "properties": {
                    "khz": {"type": "integer", "description": "Adapter clock in kHz, e.g. 4000."}
                },
                "required": ["khz"]
            }
        ),
        Tool(
            name="batch",
            description="Runs several tool calls in ONE round trip and returns ALL their full "
                        "results — the fastest way to do a sequence of operations (e.g. read "
                        "registers + backtrace + several variables) without per-call latency.",
            inputSchema={
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "description": "Ordered tool calls, each {tool, args}.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "tool": {"type": "string"},
                                "args": {"type": "object"}
                            },
                            "required": ["tool"]
                        }
                    },
                    "stop_on_error": {"type": "boolean", "description": "Stop at the first failing step (default false)."}
                },
                "required": ["steps"]
            }
        ),
        Tool(
            name="call",
            description="Invoke ANY stm32-gdb-mcp tool by name — including one that is NOT in your "
                        "current tool list (clients with tool-count limits may hide some). Use this to "
                        "reach e.g. start_debug_session when it isn't directly listed: "
                        "call(tool='start_debug_session', args={...}). Returns that tool's result.",
            inputSchema={
                "type": "object",
                "properties": {
                    "tool": {"type": "string", "description": "Name of the tool to invoke."},
                    "args": {"type": "object", "description": "Arguments for that tool."}
                },
                "required": ["tool"]
            }
        ),
        Tool(
            name="tool_help",
            description="Returns descriptions and complete schemas for full-surface tools, including tools hidden in compact mode.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Exact tool name."},
                    "query": {"type": "string", "description": "Case-insensitive name/description search."},
                },
            },
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
            name="build_firmware",
            description="Builds firmware with Keil uVision (UV4), CMake, make, or a custom command, "
                        "so the AI can rebuild after a fix. Keil emits a .axf (ELF/DWARF) that the "
                        "debug tools load like any .elf. Returns the exit code, success flag, and "
                        "build log tail.",
            inputSchema={
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["keil", "cmake", "make", "custom"], "description": "Toolchain to build with."},
                    "project": {"type": "string", "description": "keil: path to the .uvprojx/.uvproj project."},
                    "rebuild": {"type": "boolean", "description": "keil: rebuild all (-r) instead of incremental (-b)."},
                    "uv4_path": {"type": "string", "description": "keil: path to UV4.exe (auto-detected if omitted)."},
                    "build_dir": {"type": "string", "description": "cmake: the configured build directory."},
                    "directory": {"type": "string", "description": "make: directory containing the Makefile."},
                    "target": {"type": "string", "description": "Keil/CMake/make build target."},
                    "config": {"type": "string", "description": "cmake: build config, e.g. Debug/Release."},
                    "command": {"type": "array", "items": {"type": "string"}, "description": "custom: full argv to run."},
                    "cwd": {"type": "string", "description": "Working directory for the build."},
                    "timeout_sec": {"type": "number", "description": "Max build seconds (default 600)."}
                },
                "required": ["kind"]
            }
        ),
        Tool(
            name="load_symbols",
            description="Loads symbols from an ELF/AXF into the current GDB session WITHOUT flashing. "
                        "Symbols are per-session, so after a fresh connect or recover_session you need "
                        "this (or flash_firmware) before symbol breakpoints resolve. Falls back to the "
                        "debug profile's elf_path if elf_path is omitted.",
            inputSchema={
                "type": "object",
                "properties": {
                    "elf_path": {"type": "string", "description": "Path to the ELF/AXF. Defaults to the profile elf_path."}
                }
            }
        ),
        Tool(
            name="flash_firmware",
            description="Flashes a compiled firmware binary to the target. Accepts GCC .elf or Keil "
                        ".axf. By default it then resets and RUNS the firmware (Keil-style 'Load + "
                        "Run'). Pass reset_run=false to flash only (e.g. to set breakpoints before "
                        "the firmware starts).",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Absolute path to the compiled firmware file (e.g. .elf/.axf)."},
                    "reset_run": {"type": "boolean", "description": "Reset and run after flashing (default true). False = flash only, leave halted."}
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
        # (set_breakpoint .. delete_breakpoint moved to tools/breakpoint_tools.py)
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
            name="run_for_duration",
            description="Runs the target freely for a wall-clock duration, halts it, and returns "
                        "one structured report with final frame/context and optional expression "
                        "captures. Useful for counters, telemetry buffers, polling loops, and "
                        "timing-sensitive bus diagnostics without breakpoint perturbation.",
            inputSchema={
                "type": "object",
                "properties": {
                    "duration_sec": {"type": "number", "description": "Seconds to let the target run freely."},
                    "then": {"type": "string", "enum": ["halt"], "description": "Action after duration (default halt)."},
                    "capture": {
                        "type": "object",
                        "properties": {
                            "expressions": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Optional GDB/C expressions to capture after halting.",
                            },
                            "table": {
                                "type": "object",
                                "description": "Optional indexed table capture after halting: {index_range:[start,end], columns:[prefix,...]}.",
                            },
                        },
                    },
                    "sample": {
                        "type": "object",
                        "properties": {
                            "interval_sec": {"type": "number", "description": "Low-rate polling interval in seconds."},
                            "interval_ms": {"type": "number", "description": "Low-rate polling interval in milliseconds."},
                            "expressions": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "GDB/C expressions to poll while the target runs.",
                            },
                            "table": {
                                "type": "object",
                                "description": "Optional indexed table to poll: {index_range:[start,end], columns:[prefix,...]}.",
                            },
                            "max_samples": {
                                "type": "integer",
                                "description": "Safety cap on generated samples (default 10000).",
                            },
                        },
                        "description": "Best-effort low-rate debugger polling while running; does not halt the target.",
                    },
                    "resume_after": {"type": "boolean", "description": "Resume execution after capture (default false)."},
                },
                "required": ["duration_sec"],
            },
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
            name="step",
            description="Single-steps the target: kind='over' (over the line), 'into' (into calls), "
                        "'out' (run until the function returns), or 'instruction' (one machine "
                        "instruction; set over=true to step over a call).",
            inputSchema={
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["over", "into", "out", "instruction"], "description": "Step kind (default 'over')."},
                    "over": {"type": "boolean", "description": "For kind='instruction', step over a called function."}
                }
            }
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
        # (read_call_stack .. resolve_address moved to tools/inspect_tools.py)
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
        # (inspect_project moved to tools/config_tools.py)
        # (detect_rtos .. capture_rtos_snapshot moved to tools/rtos_tools.py)
        # (start_logging .. clear_logs moved to tools/logging_tools.py)
        Tool(
            name="capture_expressions",
            description="Reads GDB expressions and returns parsed values. Optionally builds an indexed table from "
                        "array-like expression prefixes while preserving the raw per-expression values.",
            inputSchema={
                "type": "object",
                "properties": {
                    "expressions": {"type": "array", "items": {"type": "string"}, "description": "GDB/C expressions to evaluate."},
                    "table": {
                        "type": "object",
                        "description": "Optional indexed table request, e.g. columns=['count'], index_range=[2,6].",
                        "properties": {
                            "index_range": {
                                "type": "array",
                                "items": {"type": "integer"},
                                "description": "Inclusive [start, end] index range.",
                            },
                            "columns": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Expression prefixes; each cell reads prefix[index].",
                            },
                        },
                    },
                },
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
        # (set_watchpoint moved to tools/breakpoint_tools.py)
        # (load_svd .. decode_peripheral_register moved to tools/peripheral_tools.py)
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
        # (set_debug_profile .. validate_debug_config moved to tools/config_tools.py)
        # --- Step 7: Tracing ---
        Tool(
            name="track_variable",
            description="Background variable tracking: action='start' (needs variable + interval_ms), "
                        "'stop', or 'get' (returns the sampled data for trend/leak analysis).",
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["start", "stop", "get"], "description": "What to do."},
                    "variable": {"type": "string", "description": "For 'start': the variable/expression to sample."},
                    "interval_ms": {"type": "integer", "description": "For 'start': polling interval in ms."}
                },
                "required": ["action"]
            }
        ),
        # --- Phase 2: determinism (journal + replayable scenarios) ---
        Tool(
            name="get_session",
            description="Returns this session's record by view: 'journal' (every tool call with "
                        "args/ok/summary/duration; supports limit), 'timeline' (compact human-readable "
                        "replay), or 'metrics' (per-tool call counts, success/failure, durations).",
            inputSchema={
                "type": "object",
                "properties": {
                    "view": {"type": "string", "enum": ["journal", "timeline", "metrics"], "description": "Which view (default journal)."},
                    "limit": {"type": "integer", "description": "journal: return only the most recent N entries."}
                }
            }
        ),
        Tool(
            name="clear_session_journal",
            description="Clears the session journal (keeps the run-id).",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="list_sessions",
            description="Lists all debug target sessions (the 'default' one plus any named) with "
                        "their liveness and port. To debug MULTIPLE boards at once, pass a 'session' "
                        "argument (any string) to any tool — e.g. start_debug_session(session='rackA', "
                        "...) — and each gets fully isolated state.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="close_session",
            description="Closes a named debug session, tearing down its GDB client and server.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "Name of the session to close, e.g. 'rackA'."}
                },
                "required": ["session_id"]
            }
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
            name="report_issue",
            description="Files a GitHub issue about an MCP problem from THIS session — auto-bundling "
                        "the session journal, metrics, and MCP version into a structured report (via "
                        "the gh CLI). Call this when a tool misbehaves or confuses you. Deduplicated "
                        "per session so retries don't spam. Defaults to the stm32-gdb-mcp repo.",
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short issue title, e.g. '[agent] start_debug_session ignores server_args'."},
                    "description": {"type": "string", "description": "What you were doing and what went wrong."},
                    "env": {"type": "string", "description": "Agent/IDE + model, e.g. 'Cursor / deepseek-v4-pro'."},
                    "repo": {"type": "string", "description": "owner/repo to file under (default the MCP repo)."}
                },
                "required": ["title", "description"]
            }
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
        # (disassemble .. address_of moved to tools/inspect_tools.py)
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
        # (setup_swo moved to tools/logging_tools.py)
        Tool(
            name="sample_pc",
            description="Statistical PC profiler: enables DWT and samples the program counter while "
                        "the core RUNS (non-intrusive, over SWD — no SWO pin or firmware change). "
                        "Returns a symbolized hot-spot histogram (top functions by %), not raw hex — "
                        "the fast way to find where firmware spends time or what loop it is stuck in. "
                        "A high 'unsampleable' count means the core is halted/asleep.",
            inputSchema={
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "description": "Number of samples (default 128; more = better statistics, slower)."},
                    "enable": {"type": "boolean", "description": "Enable DWT trace before sampling (default true)."}
                }
            }
        ),
        Tool(
            name="import_netlist",
            description="Parse a schematic netlist (KiCad .net today) into a machine-readable "
                        "BoardDescription: the MCU part/family/line, a per-pin map (package pin -> "
                        "port pin -> net -> inferred peripheral function), and the power/ground nets. "
                        "This is the input contract for automated framework design. Pass 'path' or "
                        "'text'. The result is stored on the session; read views with describe_board.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to a netlist file (e.g. board.net)."},
                    "text": {"type": "string", "description": "Netlist contents inline (alternative to path)."},
                    "format": {"type": "string", "description": "Netlist format: auto (default) or kicad."},
                    "session": {"type": "string", "description": "Target session id (default 'default')."}
                }
            }
        ),
        Tool(
            name="describe_board",
            description="Read the BoardDescription imported by import_netlist. what=summary (MCU + "
                        "peripherals + counts), pins (full MCU pin map), nets, power (power/ground "
                        "nets), or peripherals (distinct peripherals in use).",
            inputSchema={
                "type": "object",
                "properties": {
                    "what": {"type": "string", "description": "summary|pins|nets|power|peripherals (default summary)."},
                    "session": {"type": "string", "description": "Target session id (default 'default')."}
                }
            }
        ),
        Tool(
            name="validate_board",
            description="Validate the imported BoardDescription: detect a package pin wired to "
                        "multiple nets (short), a peripheral signal routed to multiple pins, a port "
                        "pin driven by multiple nets, and missing power/ground/debug/reset nets. With "
                        "a pin-capability DB (db_path or the STM32_GDB_MCP_PIN_DB env) it also checks "
                        "alternate-function legality; unknown pins degrade to 'unverified', never a "
                        "false conflict. Run import_netlist first.",
            inputSchema={
                "type": "object",
                "properties": {
                    "db_path": {"type": "string", "description": "Optional JSON pin-capability DB (CubeMX-derived) for AF-legality checks."},
                    "session": {"type": "string", "description": "Target session id (default 'default')."}
                }
            }
        ),
        Tool(
            name="load_acceptance",
            description="Load a machine-checked AcceptanceSpec (product-spec → deterministic checks) "
                        "from an inline 'spec' object or a JSON 'path'. Checks: memory_u32 (any "
                        "memory-mapped register), variable (C global), core_register, no_fault, "
                        "stopped_at (PC in a symbol). Stored on the session; evaluate with run_acceptance.",
            inputSchema={
                "type": "object",
                "properties": {
                    "spec": {"type": "object", "description": "Inline AcceptanceSpec {name, description, checks:[...]}."},
                    "path": {"type": "string", "description": "Path to a JSON AcceptanceSpec (alternative to 'spec')."},
                    "session": {"type": "string", "description": "Target session id (default 'default')."}
                }
            }
        ),
        Tool(
            name="run_acceptance",
            description="Evaluate the loaded AcceptanceSpec against live silicon state and return a "
                        "deterministic pass/fail/error verdict per check (the closed-loop judge). An "
                        "unreadable target is reported as 'error', never a silent pass. A failing/errored "
                        "check derived by synthesize_acceptance carries provenance.source — the init "
                        "function + line that should satisfy it — so fixes are precision-guided. Run "
                        "load_acceptance first; halt the target at the state you want to assert.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session": {"type": "string", "description": "Target session id (default 'default')."}
                }
            }
        ),
        Tool(
            name="describe_acceptance",
            description="Read the loaded AcceptanceSpec or the last verdict. what=summary (name + "
                        "check counts), checks (full check list), or last_result (the most recent "
                        "run_acceptance report).",
            inputSchema={
                "type": "object",
                "properties": {
                    "what": {"type": "string", "description": "summary|checks|last_result (default summary)."},
                    "session": {"type": "string", "description": "Target session id (default 'default')."}
                }
            }
        ),
        Tool(
            name="start_acceptance_loop",
            description="Start a bounded spec-to-silicon acceptance loop (Pillar C) for the loaded "
                        "AcceptanceSpec. Each run_acceptance_iteration does one build → flash → "
                        "run-to-state → run_acceptance pass; the loop stops on convergence, on "
                        "max_iterations, or on a stall (same checks failing repeatedly). Omit build/"
                        "flash to run evaluate-only iterations (you flash manually). Load a spec first.",
            inputSchema={
                "type": "object",
                "properties": {
                    "max_iterations": {"type": "integer", "description": "Hard bound on iterations (default 10)."},
                    "stall_patience": {"type": "integer", "description": "Stop if the same checks fail this many times in a row (default 3)."},
                    "build": {"type": "object", "description": "Optional build_firmware config {kind, project|build_dir|directory, target, config, rebuild, command, cwd}."},
                    "flash": {"type": "object", "description": "Optional flash+run config {file_path, run_to (default 'main'), timeout_sec}."},
                    "session": {"type": "string", "description": "Target session id (default 'default')."}
                }
            }
        ),
        Tool(
            name="run_acceptance_iteration",
            description="Run one bounded loop iteration: (optional build) → (optional flash+run-to) → "
                        "evaluate the AcceptanceSpec against live silicon, then record the verdict and "
                        "return a decision (converged / should_continue / exhausted / stalled) plus the "
                        "checks to fix. A build or run-to failure is recorded as a phase_error, not a "
                        "crash. Refuses to run a terminal loop unless force=true. start_acceptance_loop first.",
            inputSchema={
                "type": "object",
                "properties": {
                    "force": {"type": "boolean", "description": "Run even if the loop already converged/exhausted/stalled."},
                    "session": {"type": "string", "description": "Target session id (default 'default')."}
                }
            }
        ),
        Tool(
            name="acceptance_loop_status",
            description="Read the acceptance loop's trajectory (per-iteration pass/fail counts, status) "
                        "and the current decision without running an iteration.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session": {"type": "string", "description": "Target session id (default 'default')."}
                }
            }
        ),
        Tool(
            name="import_spec",
            description="Translate a controlled-vocabulary product spec (human/product terms) into the "
                        "per-peripheral design params design_framework consumes, and cross-check it against the "
                        "imported netlist. This is the upstream guard the pipeline lacked: instead of hand-writing "
                        "HAL macros, supply intent -- UART framing '8N1', direction 'txrx', flow_control 'rtscts'; "
                        "SPI role 'master', spi_mode 0..3, data_size, bit_order; I2C speed 'fast', addressing "
                        "'7bit'; ADC resolution 12, conversion 'continuous'; a timer update_hz; plus dma / "
                        "interrupt / priority opt-ins -- and the machine expands it deterministically (8E1 -> "
                        "UART_WORDLENGTH_9B + UART_PARITY_EVEN, honoring HAL's parity-bit-in-word-length rule). A "
                        "peripheral named in the spec but absent from the netlist is a conflict; an intent key or "
                        "value the machine does not model is surfaced as unresolved -- never guessed. Then "
                        "design_framework(from_spec=true) builds the plan from the translated params. Import a "
                        "netlist first so the spec is cross-checked.",
            inputSchema={
                "type": "object",
                "properties": {
                    "spec": {"type": "object", "description": "Per-peripheral intent, e.g. {'USART1': {'baud': 115200, 'framing': '8N1', 'direction': 'txrx'}, 'ADC1': {'resolution': 12, 'conversion': 'continuous', 'dma': true}}."},
                    "session": {"type": "string", "description": "Target session id (default 'default')."}
                }
            }
        ),
        Tool(
            name="design_framework",
            description="Synthesize a deterministic FrameworkPlan (Pillar D) from the session's imported "
                        "board: which clocks to enable, how each pin must be muxed, and which peripheral "
                        "init blocks to emit, in dependency order. Supply per-peripheral HAL .Init "
                        "parameters via design={'USART1': {'baud': 115200, ...}} and optional AF numbers "
                        "via af_map. Mandatory .Init members are auto-filled with HAL-standard defaults "
                        "(a complete, valid init struct, not a half-initialized one) and a few are derived "
                        "from the netlist (UART flow control from RTS/CTS pins, SPI NSS from an NSS pin); "
                        "each field is tagged explicit/derived/default. Alternate-function numbers are also "
                        "auto-derived from a pin-capability DB (db_path or the STM32_GDB_MCP_PIN_DB env) "
                        "when its entries carry an 'af' field; an explicit af_map overrides the DB per pin. "
                        "Anything that needs a human decision (baud, timer period, I2C timing) is surfaced "
                        "as unresolved, never guessed. Import a netlist first (import_netlist).",
            inputSchema={
                "type": "object",
                "properties": {
                    "design": {"type": "object", "description": "Per-peripheral config, e.g. {'USART1': {'baud': 115200, 'word_length': 'UART_WORDLENGTH_8B'}}."},
                    "from_spec": {"type": "boolean", "description": "Build from the session's imported product spec (import_spec) instead of, or merged under, an explicit design (explicit keys win)."},
                    "af_map": {"type": "object", "description": "Optional alternate-function numbers: {line_or_family: {port_pin: {'USART1_TX': 7}}}. Overrides db_path per pin."},
                    "db_path": {"type": "string", "description": "Optional JSON pin-capability DB (CubeMX-derived); entries with an 'af' field auto-fill alternate-function numbers."},
                    "session": {"type": "string", "description": "Target session id (default 'default')."}
                }
            }
        ),
        Tool(
            name="describe_framework",
            description="Read the synthesized FrameworkPlan. what=summary (mcu + clocks + peripherals), "
                        "clocks, gpio (per-pin config), peripherals (init blocks), init_order, or "
                        "unresolved (the TODO holes that need target data or a design decision). "
                        "Run design_framework first.",
            inputSchema={
                "type": "object",
                "properties": {
                    "what": {"type": "string", "description": "summary|clocks|gpio|peripherals|init_order|unresolved (default summary)."},
                    "session": {"type": "string", "description": "Target session id (default 'default')."}
                }
            }
        ),
        Tool(
            name="render_framework",
            description="Render the synthesized FrameworkPlan to a HAL C init skeleton (bsp_init.c + "
                        "bsp_init.h). Every derived fact (clock enables, GPIO modes, mapped .Init fields) "
                        "is concrete; every unresolved value is a clearly marked TODO — nothing is "
                        "fabricated. Returns the files, their content, and a todo_count. Run "
                        "design_framework first.",
            inputSchema={
                "type": "object",
                "properties": {
                    "style": {"type": "string", "description": "Code style (default 'hal')."},
                    "session": {"type": "string", "description": "Target session id (default 'default')."}
                }
            }
        ),
        Tool(
            name="synthesize_acceptance",
            description="Auto-derive a machine-checked AcceptanceSpec from the synthesized FrameworkPlan "
                        "(Pillar D Tier 3) and load it as the session's acceptance judge — welding design "
                        "synthesis to the acceptance loop. Always emits a no_fault check (init must not "
                        "HardFault); adds a memory_u32 bits_set check per clock the plan enables (RCC "
                        "enable bit), a memory_u32 bits_set check per interrupt the plan enables (arch-standard "
                        "NVIC ISER bit, from the resolved IRQ number), and a masked memory_u32 eq check per "
                        "configured pin (GPIO MODER = AF/analog; F1's CRL/CRH is skipped). Register/IRQ/port "
                        "placements come from the session's loaded SVD or explicit register_map/irq_map/gpio_map; "
                        "anything unresolvable is surfaced, never guessed. Each derived check also carries source "
                        "provenance (the init function + line that should satisfy it), so a later failure points "
                        "straight at the fix site. Run design_framework first; load an "
                        "SVD (start_debug_session/set svd) for clock/NVIC/GPIO checks.",
            inputSchema={
                "type": "object",
                "properties": {
                    "register_map": {"type": "object", "description": "Optional explicit RCC placements {line_or_family: {clock: {address, bit}}}; overrides the SVD."},
                    "irq_map": {"type": "object", "description": "Optional explicit IRQ numbers {line_or_family: {irq_name: number}} (name with or without _IRQn); overrides the SVD."},
                    "gpio_map": {"type": "object", "description": "Optional explicit GPIO port bases {line_or_family: {port_letter: MODER_address}}; overrides the SVD."},
                    "stopped_at": {"type": "string", "description": "Optional symbol to also assert the PC reached after init (e.g. 'main')."},
                    "include_no_fault": {"type": "boolean", "description": "Emit the no_fault check (default true)."},
                    "include_nvic": {"type": "boolean", "description": "Emit NVIC ISER checks for enabled interrupts (default true)."},
                    "include_gpio": {"type": "boolean", "description": "Emit GPIO MODER checks for configured pins (default true)."},
                    "load": {"type": "boolean", "description": "Load the derived spec as the session acceptance judge (default true)."},
                    "name": {"type": "string", "description": "Optional spec name."},
                    "session": {"type": "string", "description": "Target session id (default 'default')."}
                }
            }
        ),
        Tool(
            name="solve_clock_tree",
            description="Synthesize a concrete SystemClock_Config() for the session's FrameworkPlan "
                        "(Pillar D Tier 3) - the last hand-written gap in generated init code. Given a "
                        "clock source (HSE + crystal Hz, or HSI) and a target SYSCLK, it computes the exact "
                        "PLL dividers (M/N/P or R, Q for 48 MHz USB), AHB/APB bus prescalers, and flash "
                        "wait-states via pure datasheet math, then stores the result so the next "
                        "render_framework emits real clock code instead of a TODO stub. Deterministic and "
                        "honest: an unmodelled device or an infeasible target is surfaced, never guessed. "
                        "Built-in profiles: STM32F401/F407/F411 and mainstream L4 (<=80 MHz); pass an "
                        "explicit profile for others. Run design_framework first.",
            inputSchema={
                "type": "object",
                "properties": {
                    "sysclk_hz": {"type": "integer", "description": "Target SYSCLK in Hz (e.g. 168000000)."},
                    "source": {"type": "string", "description": "Clock source: 'HSE' or 'HSI' (default 'HSI')."},
                    "source_hz": {"type": "integer", "description": "HSE crystal frequency in Hz (required when source=HSE)."},
                    "need_48mhz": {"type": "boolean", "description": "Require an exact 48 MHz PLL output for USB/SDIO/RNG (default false)."},
                    "profile": {"type": "object", "description": "Optional explicit device profile; overrides the built-in table."},
                    "load": {"type": "boolean", "description": "Store the solution into the plan so render_framework uses it (default true)."},
                    "session": {"type": "string", "description": "Target session id (default 'default')."}
                }
            }
        ),
        Tool(
            name="solve_timer",
            description="Solve a timer's Prescaler/Period (PSC/ARR) for a target update frequency "
                        "(Pillar D Tier 3). Turns intent ('TIM3 at 1 kHz') into concrete register values "
                        "using the timer input clock (TIMxCLK) derived from the solved clock tree. Record a "
                        "target via design_framework(design={'TIM3': {'update_hz': 1000}}) then run "
                        "solve_clock_tree first, or pass timer_clock_hz directly for a what-if. Deterministic "
                        "and honest: an exact target yields zero-error PSC/ARR, an inexact one yields the "
                        "closest pair plus the achieved frequency and ppm error, and an unrepresentable target "
                        "or an unknown timer bus is surfaced, never guessed. Injects the result into the plan "
                        "so render_framework emits concrete values instead of a TODO.",
            inputSchema={
                "type": "object",
                "properties": {
                    "timer": {"type": "string", "description": "Timer to solve, e.g. 'TIM3'. Omit to solve every timer that has a recorded target."},
                    "target_hz": {"type": "number", "description": "Target update frequency in Hz; overrides the recorded design target (requires timer)."},
                    "timer_clock_hz": {"type": "integer", "description": "Explicit TIMxCLK in Hz; bypasses the clock-solution + bus derivation (pure what-if)."},
                    "bus": {"type": "string", "description": "Override the timer's APB bus: 'apb1' or 'apb2'."},
                    "arr_bits": {"type": "integer", "description": "ARR width override: 16 or 32 (for 32-bit timers TIM2/TIM5)."},
                    "load": {"type": "boolean", "description": "Persist the solved PSC/ARR into the plan (default true)."},
                    "session": {"type": "string", "description": "Target session id (default 'default')."}
                }
            }
        ),
        Tool(
            name="load_device_pack",
            description="Register a verified device-fact pack (Pillar F) so the deterministic solvers cover a new "
                        "STM32 family: its DMA request routing, irregular NVIC vectors, clock PLL profile, and "
                        "timer bus/width. Facts are DATA, never guessed -- STM32F4/L4 ship built-in; add a family "
                        "by supplying a validated pack (schema 'stm32-device-pack/v1') via path= (a JSON file) or "
                        "inline pack=. Call with no arguments to report current coverage. Honest by design: a "
                        "malformed pack is rejected with the list of problems and never half-loaded; shadowing a "
                        "built-in family needs allow_override=true.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to a device-pack JSON file to load and register."},
                    "pack": {"type": "object", "description": "Inline device-pack object (takes precedence over path)."},
                    "allow_override": {"type": "boolean", "description": "Permit shadowing a built-in family pack (default false)."}
                }
            }
        ),
        Tool(
            name="run_pipeline",
            description="Spec-to-silicon capstone (Pillar G): run the whole deterministic DESIGN half in one call. "
                        "Chains the existing, individually verified tools in dependency order -- import_netlist? -> "
                        "import_spec? -> design_framework -> solve_clock_tree? -> solve_timer? -> render_framework -> "
                        "synthesize_acceptance -- turning a netlist plus a product spec into a flashable HAL skeleton "
                        "and a machine-checked acceptance spec, then hands off to the build/verify loop. Optional "
                        "stages (marked ?) run only when their input is present. Honest by construction: it GUESSES "
                        "NOTHING -- it sequences verified tools and aggregates every stage's own unresolved/conflict "
                        "gaps into one 'human decisions / data still needed' list. A required-stage hard error (no "
                        "board, invalid input) stops with pipeline_status=blocked at that stage; expected gaps (an AF "
                        "needing a pin DB, an unmodelled clock) surface as complete_with_unresolved; a clean run is "
                        "complete. Operates on the session's board unless netlist= is supplied.",
            inputSchema={
                "type": "object",
                "properties": {
                    "netlist": {"type": "object", "description": "Netlist to import first: {path|text, format?}. Omit to use the board already in this session."},
                    "spec": {"type": "object", "description": "Product spec object to import and design from (import_spec)."},
                    "design": {"type": "object", "description": "Peripheral config overrides merged into the design {PERIPH: {..}}."},
                    "af_map": {"type": "object", "description": "Alternate-function map for GPIO AF resolution {line_or_family: {port_pin: {'PERIPH_SIG': af}}}."},
                    "db_path": {"type": "string", "description": "Path to a pin-capability DB for AF resolution (else STM32_GDB_MCP_PIN_DB)."},
                    "sysclk_hz": {"type": "integer", "description": "Target SYSCLK in Hz; presence enables the solve_clock_tree stage."},
                    "source": {"type": "string", "description": "Clock source for solve_clock_tree (e.g. hse/hsi)."},
                    "source_hz": {"type": "integer", "description": "Clock source frequency in Hz for solve_clock_tree."},
                    "need_48mhz": {"type": "boolean", "description": "Require a 48 MHz domain (USB/SDMMC) in the clock solve."},
                    "register_map": {"type": "object", "description": "Clock-enable register map for acceptance placement {line_or_family: {clock: {address, bit}}}."},
                    "irq_map": {"type": "object", "description": "IRQ-number map for NVIC acceptance checks {line_or_family: {irq_name: number}}."},
                    "gpio_map": {"type": "object", "description": "GPIO base-address map for pin acceptance checks {line_or_family: {port_letter: base_address}}."},
                    "acceptance_name": {"type": "string", "description": "Name for the derived acceptance spec."},
                    "stopped_at": {"type": "string", "description": "Symbol the acceptance spec should assert the core halts at."},
                    "style": {"type": "string", "description": "Render style for the framework skeleton (default hal)."},
                    "synthesize": {"type": "boolean", "description": "Set false to stop after render_framework and skip acceptance synthesis (default true)."}
                }
            }
        )
    ]
    # Assemble the advertised base list in the pinned TOOL_ORDER: inline schemas above
    # plus registry-provided ones (domain modules). Extraction order never changes the
    # advertised order; the golden surface snapshot test holds this constant.
    by_name = {tool.name: tool for tool in _tools}
    by_name.update({tool_name: spec.tool for tool_name, spec in _TOOL_REGISTRY.items()})
    missing = [n for n in _TOOL_ORDER if n not in by_name]
    assert not missing, f"tools in TOOL_ORDER with no schema (inline or registered): {missing}"
    _tools = [by_name[tool_name] for tool_name in _TOOL_ORDER]
    # Compact mode (STM32_GDB_MCP_COMPACT=1): expose only a small core so nothing gets
    # truncated under tight client tool-count caps. Every other tool is still reachable
    # via call(tool, args).
    advertised = _advertised_tools(_tools)
    _tool_catalog = {tool.name: tool for tool in advertised}
    if os.environ.get("STM32_GDB_MCP_COMPACT"):
        return [t for t in advertised if t.name in _CORE_TOOLS]
    return advertised


def _autoload_symbols(sess) -> bool:
    """Load symbols from the profile's elf_path after a connect, if configured."""
    elf_path = sess.debug_profile.get().get("elf_path")
    if not elf_path:
        return False
    try:
        sess.gdb_client.load_symbols(elf_path)
        return True
    except Exception:
        return False


def _adapter_speed_khz(args: list[str]) -> int | None:
    for token in args:
        parts = token.split() if isinstance(token, str) else []
        if len(parts) == 3 and parts[:2] == ["adapter", "speed"] and parts[2].isdigit():
            return int(parts[2])
    return None


def _recover_current_session(gdb_client, gdb_manager, last_session: dict, sess) -> dict:
    if not last_session.get("server_type"):
        raise RuntimeError("No prior session to recover; call start_debug_session first.")
    for teardown in (gdb_client.stop_gdb, gdb_manager.stop):
        try:
            teardown()
        except Exception:
            pass

    port = retry_call(
        lambda: gdb_manager.start(last_session["server_type"], last_session["server_args"]),
        attempts=3,
        backoff_base=0.8,
    )
    gdb_client.start_gdb()
    resp = gdb_client.connect("localhost", port)
    symbols = _autoload_symbols(sess)
    return {
        "message": "Session recovered",
        "server_type": last_session["server_type"],
        "port": port,
        "symbols_loaded": symbols,
        "raw_response": resp,
    }


def _stop_event_next_actions(event: dict) -> list[str]:
    """Guide the model to the natural next loop step for a stop event."""
    reason = event.get("reason")
    if reason in ("signal-received", "exited-signalled"):
        return ["diagnose_fault", "reconstruct_fault_context", "read_call_stack"]
    if reason == "timeout":
        # Timeout means the breakpoint's code path was NOT reached — do not just retry.
        # Halt, see where execution actually is, and check whether the breakpoint was
        # ever hit (hit_count=0 => the precondition/flag to reach it was not satisfied).
        return ["halt_execution", "capture_state", "list_breakpoints"]
    if event.get("stopped"):
        return ["read_frame_variables", "list_source", "read_call_stack"]
    return []


def _pipeline_stage_summary(stage: str, data: dict) -> dict:
    """A compact, display-only highlight of one pipeline stage's envelope."""
    data = data or {}
    if stage == "import_netlist":
        mcu = data.get("mcu") or {}
        return {"mcu": mcu.get("part_normalized") or mcu.get("part"),
                "peripherals": len(data.get("peripherals") or [])}
    if stage == "import_spec":
        return {"design_peripherals": len(data.get("design") or {}),
                "conflicts": len(data.get("conflicts") or []),
                "cross_checked": data.get("cross_checked")}
    if stage == "design_framework":
        return {"peripherals": len(data.get("peripherals") or []),
                "clocks": len(data.get("clocks") or []),
                "unresolved": data.get("unresolved_count")}
    if stage == "solve_clock_tree":
        if data.get("feasible"):
            return {"feasible": True, "sysclk_mhz": (data.get("clock") or {}).get("sysclk_mhz")}
        return {"feasible": False}
    if stage == "solve_timer":
        return {"solved": data.get("solved_count"), "timers": data.get("timer_count")}
    if stage == "render_framework":
        return {"files": [f.get("path") for f in data.get("files") or []],
                "todo_count": data.get("todo_count")}
    if stage == "synthesize_acceptance":
        spec = data.get("spec") or {}
        return {"checks": spec.get("check_count"), "kinds": spec.get("kinds")}
    return {}


def _pipeline_next(report: dict) -> list[str]:
    """Suggested next actions tailored to the pipeline outcome."""
    status = report["pipeline_status"]
    if status == "blocked":
        blocked = report.get("blocked") or {}
        return [f"fix the blocked stage: {blocked.get('stage')} ({blocked.get('code')})"]
    if status == "complete_with_unresolved":
        return ["review 'unresolved' (supply af_map/db_path, a device pack, or a clock profile)",
                "build_firmware", "start_acceptance_loop"]
    return ["build_firmware", "start_acceptance_loop"]


def _dispatch_tool(name: str, arguments: dict | None) -> list[TextContent]:
    if arguments is None:
        arguments = {}

    # Resolve the per-target session and bind its objects as locals; handlers below use
    # these names. The "default" session reads the module globals (back-compat).
    _sess = _resolve_session(arguments)
    if "session" in arguments:
        arguments = {k: v for k, v in arguments.items() if k != "session"}  # handlers don't see the selector
    gdb_manager = _sess.gdb_manager
    gdb_client = _sess.gdb_client
    svd_parser = _sess.svd_parser
    variable_tracker = _sess.variable_tracker
    debug_profile = _sess.debug_profile
    freertos_inspector = _sess.freertos_inspector
    memory_guard = _sess.memory_guard
    rtt_log_reader = _sess.rtt_log_reader
    swo_log_reader = _sess.swo_log_reader
    uart_log_reader = _sess.uart_log_reader
    _last_session = _sess.last_session
    session_board = _sess.board
    session_acceptance = _sess.acceptance
    session_loop = _sess.loop
    session_design = _sess.design
    session_spec = _sess.spec

    try:
        # Action-dispatched families: translate to the underlying tool and reuse its handler.
        if name in _MERGED:
            disc, mapping, _, _ = _MERGED[name]
            choice = arguments.get(disc)
            if choice not in mapping:
                return [content_error(
                    f"{name} requires '{disc}' to be one of {list(mapping)}.",
                    code="missing_argument",
                    suggested_next_actions=[f"call {name} with {disc}=<one of {list(mapping)}>"],
                )]
            forwarded = {k: v for k, v in arguments.items() if k != disc}
            forwarded["session"] = _sess.id  # preserve session across the internal hop
            return _dispatch_tool(mapping[choice], forwarded)

        # Registry-backed tools (domain modules under mcp_server/tools/).
        _spec_entry = _TOOL_REGISTRY.get(name)
        if _spec_entry is not None:
            return _spec_entry.handler(_make_context(_sess), arguments)

        if name == "tool_help":
            requested_name = arguments.get("name")
            query = (arguments.get("query") or "").strip().lower()
            if requested_name:
                matches = [_tool_catalog[requested_name]] if requested_name in _tool_catalog else []
            elif query:
                matches = [
                    tool
                    for tool in _tool_catalog.values()
                    if query in tool.name.lower() or query in (tool.description or "").lower()
                ]
            else:
                return [content_error(
                    "tool_help requires name or query.",
                    code="missing_argument",
                )]
            return [content_success({
                "count": len(matches),
                "tools": [tool.model_dump(by_alias=True, exclude_none=True) for tool in matches],
            })]

        if name == "start_debug_session":
            profile = debug_profile.get()
            server_type = arguments.get("server_type") or profile.get("server_type")
            if not server_type:
                return [content_error(
                    "server_type is required when the active debug profile does not define it.",
                    code="missing_argument",
                    suggested_next_actions=["load_debug_config", "set_debug_profile"],
                )]
            if "server_args" in arguments:
                configured_args = arguments["server_args"]
                server_args_source = "arguments"
            else:
                configured_args = profile.get("server_args", [])
                server_args_source = "profile" if configured_args else "arguments"
            args = list(configured_args or [])
            detected_probe = None
            if server_type == "openocd" and not args:
                mcu = profile.get("mcu")
                probe, probe_source, detected_probe, detection = _probe_selection(arguments, profile)
                if mcu and probe:
                    try:
                        inferred = suggest_server_args(
                            mcu,
                            probe,
                            scripts_dir=find_openocd_scripts(),
                            speed_khz=arguments.get("speed_khz", 4000),
                        )
                    except ValueError as exc:
                        return [content_error(
                            f"Could not infer openocd server_args from debug profile: {exc}",
                            code="invalid_target_config",
                            suggested_next_actions=["set_debug_profile", "load_debug_config", "suggest_server_args"],
                        )]
                    args = list(inferred["server_args"])
                    server_args_source = probe_source
                else:
                    if detection and detection.get("count", 0) > 1:
                        return [content_error(
                            "Multiple debug probes are connected. Select one with profile probe/serial or pass "
                            "explicit server_args; no probe was chosen automatically.",
                            code="multiple_probes",
                            raw_response=detection,
                            suggested_next_actions=["detect_probe", "set_debug_profile", "start_debug_session"],
                        )]
                    missing = [field for field, value in (("mcu", mcu), ("probe", probe)) if not value]
                    return [content_error(
                        "openocd requires server_args naming the probe interface and target, e.g. "
                        "['-f','interface/stlink.cfg','-f','target/stm32l4x.cfg']. Pass server_args, "
                        f"or set debug profile fields first (missing: {', '.join(missing)}).",
                        code="invalid_target_config",
                        suggested_next_actions=["set_debug_profile", "load_debug_config", "suggest_server_args"],
                    )]
            if server_type == "openocd":
                # Concurrency: a named session gets a distinct gdb_port, and a per-board
                # probe is selected by 'serial', so multiple OpenOCD instances coexist.
                _argstr = " ".join(a for a in args if isinstance(a, str))
                if _sess.id != "default" and "gdb_port" not in _argstr:
                    args += ["-c", f"gdb_port {_sess.gdb_port}"]
                    # We never use OpenOCD's telnet/tcl ports; disable them so a second
                    # instance doesn't collide on the default 4444/6666.
                    if "telnet_port" not in _argstr:
                        args += ["-c", "telnet_port disabled"]
                    if "tcl_port" not in _argstr:
                        args += ["-c", "tcl_port disabled"]
                serial = (
                    arguments.get("serial")
                    or profile.get("serial")
                    or (detected_probe or {}).get("serial")
                    or getattr(_sess, "serial", None)
                )
                if serial and "adapter serial" not in _argstr:
                    args += ["-c", f"adapter serial {serial}"]
                    _sess.serial = serial
            # A previous session may not have fully released the probe/port yet, so the
            # restart can transiently fail with "open failed". retry_call backs off and
            # retries those, so stop -> start (and CI loops) work without a manual restart.
            if gdb_manager.is_alive():
                try:
                    gdb_client.stop_gdb()
                except Exception:
                    pass
                gdb_manager.stop()
            try:
                port = retry_call(lambda: gdb_manager.start(server_type, args), attempts=3, backoff_base=0.8)
                gdb_client.start_gdb()
                resp = gdb_client.connect("localhost", port)
            except Exception as exc:
                server_log = gdb_manager.get_logs() if hasattr(gdb_manager, "get_logs") else ""
                classification = classify_error(f"{exc}\n{server_log}")
                for teardown in (gdb_client.stop_gdb, gdb_manager.stop):
                    try:
                        teardown()
                    except Exception:
                        pass
                message = str(exc)[-1000:]
                if classification.get("hint"):
                    message = f"{message} — {classification['hint']}"
                return [content_error(
                    message,
                    code=classification["code"],
                    raw_response={
                        "attempted": {
                            "backend": server_type,
                            "server_args": args,
                            "speed_khz": _adapter_speed_khz(args),
                        },
                        "server_log": server_log[-4000:],
                        "retryable": classification["retryable"],
                    },
                    suggested_next_actions=classification["suggested_next_actions"],
                )]
            _last_session["server_type"] = server_type
            _last_session["server_args"] = args
            symbols = _autoload_symbols(_sess)
            return [content_success(
                {
                    "message": "Debug session started",
                    "server_type": server_type,
                    "port": port,
                    "symbols_loaded": symbols,
                    "server_args_source": server_args_source,
                    "detected_probe": detected_probe,
                },
                raw_response=resp,
            )]

        elif name == "detect_probe":
            detected = detect_probe()
            if detected.get("error"):
                return [content_error(
                    f"Could not enumerate host USB debug probes: {detected['error']}",
                    code="probe_detection_failed",
                    raw_response=detected,
                    suggested_next_actions=["set_debug_profile", "suggest_server_args"],
                )]
            actions = ["suggest_server_args", "start_debug_session"] if detected.get("count") == 1 else []
            return [content_success(detected, suggested_next_actions=actions)]

        elif name == "suggest_server_args":
            scripts_dir = find_openocd_scripts()
            profile = debug_profile.get()
            probe, probe_source, detected_probe, detection = _probe_selection(arguments, profile)
            if not probe:
                if detection and detection.get("count", 0) > 1:
                    return [content_error(
                        "Multiple debug probes are connected. Pass probe explicitly or select one in the debug profile.",
                        code="multiple_probes",
                        raw_response=detection,
                        suggested_next_actions=["detect_probe", "set_debug_profile", "suggest_server_args"],
                    )]
                return [content_error(
                    "Missing required argument: 'probe'. Provide it directly, or set debug profile probe "
                    "first, or connect exactly one supported probe. Known probes: stlink, jlink, cmsis-dap.",
                    code="missing_argument",
                    raw_response=detection,
                    suggested_next_actions=["set_debug_profile", "load_debug_config", "suggest_server_args"],
                )]
            result = suggest_server_args(
                arguments["mcu"],
                probe,
                scripts_dir=scripts_dir,
                speed_khz=arguments.get("speed_khz", 4000),
            )
            result["probe"] = probe
            result["probe_source"] = probe_source
            if detected_probe:
                result["detected_probe"] = detected_probe
            return [content_success(result, suggested_next_actions=["start_debug_session", "self_check"])]

        elif name == "set_adapter_speed":
            resp = gdb_client.set_adapter_speed(arguments["khz"])
            return [content_success({"message": "Adapter speed set", "khz": arguments["khz"]}, raw_response=resp)]

        elif name == "recover_session":
            if not _last_session.get("server_type"):
                return [content_error(
                    "No prior session to recover; call start_debug_session first.",
                    code="no_session",
                    suggested_next_actions=["start_debug_session"],
                )]
            recovered = _recover_current_session(gdb_client, gdb_manager, _last_session, _sess)
            return [content_success(
                {
                    "message": recovered["message"],
                    "server_type": recovered["server_type"],
                    "port": recovered["port"],
                    "symbols_loaded": recovered["symbols_loaded"],
                },
                raw_response=recovered["raw_response"],
                suggested_next_actions=["self_check", "check_session_health"],
            )]

        elif name == "stop_debug_session":
            teardown_debug_session(_sess)
            return [content_success({"message": "Debug session stopped"})]

        elif name == "self_check":
            # Identity registers can't be read reliably while the core is running, so halt
            # first (a self_check is a deliberate diagnostic). Pass halt=false to skip.
            halted = False
            if arguments.get("halt", True):
                try:
                    gdb_client.halt_execution()
                    halted = True
                except Exception:
                    pass
            cpuid = gdb_client.read_word(0xE000ED00)
            dbgmcu_idcode = gdb_client.read_word(0xE0042000)
            expected = arguments.get("expected_family") or debug_profile.get().get("mcu")
            result = evaluate_self_check(cpuid, dbgmcu_idcode, expected_family=expected)
            result["halted_for_check"] = halted
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

        elif name == "build_firmware":
            kind = arguments["kind"]
            log_path = None
            uv4_path = arguments.get("uv4_path")
            if kind == "keil":
                uv4_path = uv4_path or build_mod.find_uv4()
                log_path = os.path.join(tempfile.gettempdir(), f"uv4_build_{session_journal.run_id}.log")
            cmd = build_mod.resolve_build_command(
                kind,
                project=arguments.get("project"),
                build_dir=arguments.get("build_dir"),
                directory=arguments.get("directory"),
                target=arguments.get("target"),
                config=arguments.get("config"),
                rebuild=arguments.get("rebuild", False),
                uv4_path=uv4_path,
                log_path=log_path,
                command=arguments.get("command"),
            )
            result = build_mod.run_build(
                cmd, timeout=arguments.get("timeout_sec", 600), cwd=arguments.get("cwd"), log_path=log_path
            )
            success = build_mod.is_build_success(kind, result["returncode"])
            built_target = build_mod.parse_keil_built_target(result["output"]) if kind == "keil" else None
            requested_target = arguments.get("target")
            target_mismatch = bool(requested_target and built_target and requested_target != built_target)
            payload = {
                "kind": kind,
                "command": cmd,
                "returncode": result["returncode"],
                "success": success,
                "log_tail": result["output"][-4000:],
                "requested_target": requested_target,
                "built_target": built_target,
                "target_mismatch": target_mismatch,
            }
            if not success:
                return [content_error(
                    f"Build failed (exit {result['returncode']})",
                    code="build_failed",
                    raw_response=payload,
                    suggested_next_actions=["get_session"],
                )]
            if target_mismatch:
                payload["success"] = False
                return [content_error(
                    f"Keil built target '{built_target}', not requested target '{requested_target}'.",
                    code="build_target_mismatch",
                    raw_response=payload,
                    suggested_next_actions=["inspect_project", "build_firmware"],
                )]
            return [content_success(payload, suggested_next_actions=["flash_firmware", "flash_and_run"])]

        elif name == "load_symbols":
            elf_path = arguments.get("elf_path") or debug_profile.get().get("elf_path")
            if not elf_path:
                return [content_error(
                    "No elf_path given and none in the debug profile.",
                    code="missing_elf",
                    suggested_next_actions=["set_debug_profile"],
                )]
            resp = gdb_client.load_symbols(elf_path)
            return [content_success(
                {"message": "Symbols loaded", "elf_path": elf_path},
                raw_response=resp,
                suggested_next_actions=["set_breakpoint", "list_functions", "analyze_stack"],
            )]

        elif name == "flash_firmware":
            resp = gdb_client.load_firmware(arguments["file_path"])
            data = {"message": "Firmware flashed", "file_path": arguments["file_path"], "reset_run": False}
            if arguments.get("reset_run", True):
                profile = debug_profile.get()
                reset_config = profile.get("reset", {})
                resolved = resolve_reset_command(
                    gdb_manager.server_type or profile.get("server_type"),
                    halt=False,
                    strategy=reset_config.get("strategy"),
                    command=reset_config.get("command"),
                )
                resp = (resp or []) + gdb_client.reset_run(command=resolved["command"])
                data["reset_run"] = True
                data["message"] = "Firmware flashed; target reset and running"
            return [content_success(data, raw_response=resp)]

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
            profile = debug_profile.get()
            reset_config = profile.get("reset", {})
            reset = resolve_reset_command(
                gdb_manager.server_type or profile.get("server_type"),
                halt=True,
                strategy=reset_config.get("strategy"),
                command=reset_config.get("command"),
            )
            result = flash_and_run(
                gdb_client,
                file_path=arguments["file_path"],
                run_to=arguments.get("run_to", "main"),
                timeout_sec=arguments.get("timeout_sec", 10.0),
                reset_command=reset["command"],
            )
            return [content_success(result, suggested_next_actions=["capture_state", "debug_until"])]

        elif name == "run_for_duration":
            result = run_for_duration(
                gdb_client,
                duration_sec=arguments["duration_sec"],
                then=arguments.get("then", "halt"),
                capture=arguments.get("capture"),
                sample=arguments.get("sample"),
                resume_after=arguments.get("resume_after", False),
                recover=lambda: _recover_current_session(gdb_client, gdb_manager, _last_session, _sess),
            )
            next_actions = ["capture_state", "read_memory"]
            if result.get("resume_after"):
                next_actions = ["wait_for_stop", "halt_execution"]
            return [content_success(result, suggested_next_actions=next_actions)]

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

        elif name == "step":
            kind = arguments.get("kind", "over")
            if kind == "over":
                resp = gdb_client.step_over()
            elif kind == "into":
                resp = gdb_client.step_into()
            elif kind == "out":
                resp = gdb_client.step_out()
            elif kind == "instruction":
                resp = gdb_client.step_instruction(over=arguments.get("over", False))
            else:
                raise ValueError(f"Unknown step kind '{kind}'. Use over, into, out, or instruction.")
            return [content_success({"message": f"Stepped ({kind})", "kind": kind}, raw_response=resp)]

        elif name == "read_variable":
            resp = gdb_client.read_variable(arguments["name"])
            value = decode_evaluated_value(resp)
            if value is None:
                return [content_error(
                    f"No value returned for expression {arguments['name']!r}. Target may be running or symbols may be missing.",
                    code="no_value_returned",
                    raw_response=resp,
                    suggested_next_actions=["halt", "load_symbols", "expressions"],
                )]
            return [content_success(
                {"message": "Variable read", "name": arguments["name"], "value": value},
                raw_response=resp,
            )]

        elif name == "read_memory":
            resp = gdb_client.read_memory(arguments["address"], arguments["length"])
            contents = decode_memory_bytes(resp)
            if contents is None:
                return [content_error(
                    f"No memory bytes returned for address {arguments['address']}. Target may be running or inaccessible.",
                    code="no_value_returned",
                    raw_response=resp,
                    suggested_next_actions=["halt", "self_check", "capture_state"],
                )]
            return [content_success(
                {
                    "message": "Memory read",
                    "address": arguments["address"],
                    "length": arguments["length"],
                    "bytes": contents,
                },
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

        elif name == "capture_expressions":
            result = run_expression_capture(
                gdb_client,
                expressions=arguments.get("expressions"),
                table=arguments.get("table"),
            )
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

        elif name == "track_variable":
            action = arguments["action"]
            if action == "start":
                variable_tracker.start(arguments["variable"], arguments["interval_ms"])
                return [content_success({
                    "message": "Variable tracking started",
                    "variable": arguments["variable"],
                    "interval_ms": arguments["interval_ms"],
                })]
            if action == "stop":
                variable_tracker.stop()
                return [content_success({"message": "Tracking stopped"})]
            if action == "get":
                return [content_success({"data": variable_tracker.get_data()})]
            raise ValueError(f"Unknown track_variable action '{action}'. Use start, stop, or get.")

        elif name == "get_session":
            view = arguments.get("view", "journal")
            if view == "timeline":
                return [content_success({"run_id": session_journal.run_id, "timeline": session_journal.timeline()})]
            if view == "metrics":
                return [content_success({"run_id": session_journal.run_id, **compute_metrics(session_journal.get())})]
            if view == "journal":
                entries = session_journal.get(limit=arguments.get("limit"))
                return [content_success({"run_id": session_journal.run_id, "count": len(entries), "entries": entries})]
            raise ValueError(f"Unknown session view '{view}'. Use journal, timeline, or metrics.")

        elif name == "clear_session_journal":
            session_journal.clear()
            return [content_success({"message": "Session journal cleared", "run_id": session_journal.run_id})]

        elif name == "list_sessions":
            g = globals()
            default_row = {
                "session": "default",
                "server_alive": g["gdb_manager"].is_alive(),
                "gdb_alive": g["gdb_client"].is_alive(),
                "server_type": g["gdb_manager"].server_type,
                "port": g["gdb_manager"].port,
            }
            return [content_success({"sessions": [default_row] + session_manager.list()})]

        elif name == "close_session":
            sid = arguments["session_id"]
            if sid == "default":
                g = globals()
                for stop in (g["gdb_client"].stop_gdb, g["gdb_manager"].stop):
                    try:
                        stop()
                    except Exception:
                        pass
                return [content_success({"message": "Default session stopped", "session_id": "default"})]
            closed = session_manager.close(sid)
            return [content_success({
                "message": "Session closed" if closed else "No such session",
                "closed": closed, "session_id": sid,
            })]

        elif name == "get_timeouts":
            return [content_success({"timeouts": gdb_client.timeouts.as_dict()})]

        elif name == "set_timeouts":
            updated = gdb_client.timeouts.set(arguments["overrides"])
            return [content_success({"message": "Timeouts updated", "timeouts": updated})]

        elif name == "report_issue":
            title = arguments["title"]
            fp = issue_fingerprint(title)
            if fp in _reported_issues:
                return [content_success({
                    "message": "Already reported this session (deduplicated)",
                    "url": _reported_issues[fp],
                })]
            body = build_issue_body(
                description=arguments.get("description"),
                env=arguments.get("env"),
                version=_mcp_version(),
                journal=session_journal.get(limit=25),
                metrics=compute_metrics(session_journal.get()),
            )
            repo = arguments.get("repo") or DEFAULT_REPO
            result = file_issue(repo, title, body)
            if result["ok"]:
                _reported_issues[fp] = result["url"]
                return [content_success({"message": "Issue filed", "url": result["url"], "repo": repo})]
            return [content_error(
                f"Could not file the issue automatically: {result['error']}",
                code="issue_filing_failed",
                raw_response={"repo": repo, "title": title, "prepared_body": result.get("body")},
                suggested_next_actions=["get_session"],
            )]

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

        elif name == "run_to_line":
            resp = gdb_client.run_to_line(arguments["location"])
            return [content_success(
                {"message": "Ran to location", "location": arguments["location"]},
                raw_response=resp,
            )]

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
            profile = gdb_client.profile_pc(
                count=arguments.get("count", 128),
                enable=arguments.get("enable", True),
            )
            top = profile["hotspots"][0]["function"] if profile["hotspots"] else None
            if profile["sampled"] == 0:
                # Nothing sampled: the core was halted or asleep the whole time, not running.
                msg = ("No PC samples hit running code — the core is halted, in WFI/sleep, or "
                       "stuck in a low-power state. Resume it (continue_execution) or check why "
                       "it is not running before profiling.")
                actions = ["continue_execution", "capture_state"]
            else:
                msg = (f"Profiled {profile['total_samples']} PC samples while running; "
                       f"hottest: {top} ({profile['hotspots'][0]['percent']}%).")
                actions = ["frame", "disassemble"]
            return [content_success({"message": msg, **profile},
                                    suggested_next_actions=actions)]

        elif name == "import_netlist":
            text = arguments.get("text")
            path = arguments.get("path")
            if not text and not path:
                return [content_error(
                    "import_netlist needs 'path' or 'text'.", code="missing_argument",
                    suggested_next_actions=["import_netlist(path='board.net')"])]
            fmt = arguments.get("format", "auto")
            try:
                parsed = (parse_netlist(text, fmt=fmt, source="<text>") if text
                          else load_netlist_file(path, fmt=fmt))
            except (ValueError, OSError) as e:
                return [content_error(
                    str(e), code="netlist_parse_error",
                    suggested_next_actions=["import_netlist with format=kicad"])]
            session_board["current"] = parsed
            return [content_success(
                summarize_board(parsed),
                suggested_next_actions=["describe_board (what=pins)", "describe_board (what=peripherals)"])]

        elif name == "describe_board":
            parsed = session_board.get("current")
            if not parsed:
                return [content_error(
                    "No board imported for this session. Run import_netlist first.", code="no_board",
                    suggested_next_actions=["import_netlist(path='board.net')"])]
            what = arguments.get("what", "summary")
            view = board_view(parsed, what)
            if view is None:
                return [content_error(
                    f"Unknown view '{what}'.", code="invalid_argument",
                    suggested_next_actions=["describe_board (what=summary|pins|nets|power|peripherals)"])]
            return [content_success(view, suggested_next_actions=["describe_board (what=pins)"])]

        elif name == "validate_board":
            parsed = session_board.get("current")
            if not parsed:
                return [content_error(
                    "No board imported for this session. Run import_netlist first.", code="no_board",
                    suggested_next_actions=["import_netlist(path='board.net')"])]
            capability_db = None
            db_path = arguments.get("db_path") or os.environ.get("STM32_GDB_MCP_PIN_DB")
            if db_path:
                try:
                    capability_db = load_capability_db(db_path)
                except (OSError, ValueError) as e:
                    return [content_error(
                        f"Failed to load pin-capability DB: {e}", code="db_load_error",
                        suggested_next_actions=["validate_board without db_path"])]
            report = validate_board(parsed, capability_db)
            actions = ["describe_board (what=pins)"] if not report["ok"] else ["describe_board (what=peripherals)"]
            if not report["af_checked"]:
                actions.append("validate_board(db_path=...) to also check alternate-function legality")
            return [content_success(report, suggested_next_actions=actions)]

        elif name == "load_acceptance":
            raw = arguments.get("spec")
            path = arguments.get("path")
            if raw is None and not path:
                return [content_error(
                    "Provide 'spec' (inline object) or 'path' (JSON file).", code="missing_argument",
                    suggested_next_actions=["load_acceptance(spec={'checks': [...]})"])]
            if raw is None:
                try:
                    with open(path, encoding="utf-8") as handle:
                        raw = json.load(handle)
                except (OSError, ValueError) as e:
                    return [content_error(
                        f"Failed to read acceptance spec: {e}", code="spec_load_error",
                        suggested_next_actions=["load_acceptance(spec={...})"])]
            try:
                normalized = validate_acceptance_spec(raw)
            except ValueError as e:
                return [content_error(
                    str(e), code="invalid_spec",
                    suggested_next_actions=["load_acceptance with a corrected spec"])]
            session_acceptance["current"] = normalized
            session_acceptance["last_result"] = None
            return [content_success(
                summarize_acceptance(normalized),
                suggested_next_actions=["run_acceptance", "describe_acceptance (what=checks)"])]

        elif name == "run_acceptance":
            spec = session_acceptance.get("current")
            if not spec:
                return [content_error(
                    "No acceptance spec loaded for this session. Run load_acceptance first.", code="no_spec",
                    suggested_next_actions=["load_acceptance(spec={...})"])]
            reader = GdbAcceptanceReader(gdb_client)
            report = evaluate_acceptance(spec, reader)
            session_acceptance["last_result"] = report
            if report["ok"]:
                actions = ["describe_acceptance (what=last_result)", "plan_framework"]
            else:
                actions = ["describe_acceptance (what=last_result)", "read_call_stack",
                           "reconstruct_fault_context"]
            return [content_success(report, suggested_next_actions=actions)]

        elif name == "describe_acceptance":
            spec = session_acceptance.get("current")
            if not spec:
                return [content_error(
                    "No acceptance spec loaded for this session. Run load_acceptance first.", code="no_spec",
                    suggested_next_actions=["load_acceptance(spec={...})"])]
            what = arguments.get("what") or "summary"
            if what == "summary":
                return [content_success(summarize_acceptance(spec),
                                        suggested_next_actions=["run_acceptance"])]
            if what == "checks":
                return [content_success({"checks": spec["checks"]},
                                        suggested_next_actions=["run_acceptance"])]
            if what == "last_result":
                result = session_acceptance.get("last_result")
                if result is None:
                    return [content_error(
                        "No verdict yet. Run run_acceptance first.", code="no_result",
                        suggested_next_actions=["run_acceptance"])]
                return [content_success(result)]
            return [content_error(
                "what must be summary|checks|last_result", code="invalid_argument",
                suggested_next_actions=["describe_acceptance (what=summary|checks|last_result)"])]

        elif name == "start_acceptance_loop":
            spec = session_acceptance.get("current")
            if not spec:
                return [content_error(
                    "No acceptance spec loaded for this session. Run load_acceptance first.", code="no_spec",
                    suggested_next_actions=["load_acceptance(spec={...})"])]
            max_iterations = arguments.get("max_iterations", 10)
            if not isinstance(max_iterations, int) or max_iterations < 1:
                return [content_error(
                    "max_iterations must be an integer >= 1.", code="invalid_argument",
                    suggested_next_actions=["start_acceptance_loop(max_iterations=10)"])]
            build_cfg = arguments.get("build")
            flash_cfg = arguments.get("flash")
            plan = {
                "max_iterations": max_iterations,
                "stall_patience": arguments.get("stall_patience", 3),
                "has_build": bool(build_cfg),
                "has_flash": bool(flash_cfg),
                "build": build_cfg,
                "flash": flash_cfg,
                "acceptance_name": spec.get("name"),
            }
            state = new_loop_state(plan)
            session_loop["current"] = state
            return [content_success(
                summarize_loop(state), suggested_next_actions=["run_acceptance_iteration"])]

        elif name == "run_acceptance_iteration":
            state = session_loop.get("current")
            if not state:
                return [content_error(
                    "No acceptance loop started for this session. Run start_acceptance_loop first.", code="no_loop",
                    suggested_next_actions=["start_acceptance_loop"])]
            spec = session_acceptance.get("current")
            if not spec:
                return [content_error(
                    "No acceptance spec loaded for this session. Run load_acceptance first.", code="no_spec",
                    suggested_next_actions=["load_acceptance(spec={...})"])]
            if state["status"] != "active" and not arguments.get("force"):
                decision = loop_decision(state)
                return [content_success(
                    {"iteration": None, "decision": decision, "summary": summarize_loop(state)},
                    suggested_next_actions=decision["next_actions"])]
            plan = state["plan"]
            steps = GdbLoopSteps(gdb_client, spec, build_cfg=plan.get("build"), flash_cfg=plan.get("flash"))
            outcome = run_iteration(state, steps)
            session_loop["current"] = state
            decision = outcome["decision"]
            return [content_success(
                {"iteration": outcome["iteration"], "decision": decision, "summary": summarize_loop(state)},
                suggested_next_actions=decision["next_actions"])]

        elif name == "acceptance_loop_status":
            state = session_loop.get("current")
            if not state:
                return [content_error(
                    "No acceptance loop started for this session. Run start_acceptance_loop first.", code="no_loop",
                    suggested_next_actions=["start_acceptance_loop"])]
            return [content_success({"summary": summarize_loop(state), "decision": loop_decision(state)})]

        elif name == "import_spec":
            spec = arguments.get("spec")
            if not isinstance(spec, dict) or not spec:
                return [content_error(
                    "import_spec needs a non-empty 'spec' object mapping peripheral -> intent config.",
                    code="missing_argument",
                    suggested_next_actions=["import_spec(spec={'USART1': {'baud': 115200, 'framing': '8N1'}})"])]
            board = session_board.get("current")
            result = build_design(spec, board=board)
            session_spec["current"] = result
            payload = dict(result)
            payload["cross_checked"] = board is not None
            if result["conflicts"]:
                actions = ["describe_board (what=peripherals)", "fix the spec, then import_spec again"]
            elif result["unresolved"]:
                actions = ["review unresolved, then design_framework(from_spec=true)"]
            else:
                actions = ["design_framework(from_spec=true)"]
            return [content_success(payload, suggested_next_actions=actions)]

        elif name == "design_framework":
            board = session_board.get("current")
            if not board:
                return [content_error(
                    "No board imported for this session. Run import_netlist first.", code="no_board",
                    suggested_next_actions=["import_netlist(path='board.net')"])]
            design = arguments.get("design")
            if design is not None and not isinstance(design, dict):
                return [content_error(
                    "design must be an object mapping peripheral name -> config.", code="invalid_argument",
                    suggested_next_actions=["design_framework(design={'USART1': {'baud': 115200}})"])]
            if arguments.get("from_spec"):
                stored = session_spec.get("current")
                if not stored:
                    return [content_error(
                        "from_spec set but no spec imported for this session. Run import_spec first.",
                        code="no_spec",
                        suggested_next_actions=["import_spec(spec={'USART1': {'baud': 115200}})"])]
                spec_design = stored.get("design") or {}
                if design:
                    merged = {p: dict(cfg) for p, cfg in spec_design.items()}
                    for p, cfg in design.items():
                        merged[p] = {**merged.get(p, {}), **(cfg or {})}
                    design = merged
                else:
                    design = {p: dict(cfg) for p, cfg in spec_design.items()}
            af_map = arguments.get("af_map")
            if af_map is not None and not isinstance(af_map, dict):
                return [content_error(
                    "af_map must be an object {line_or_family: {port_pin: {'PERIPH_SIG': af}}}.",
                    code="invalid_argument", suggested_next_actions=["design_framework"])]
            db_path = arguments.get("db_path") or os.environ.get("STM32_GDB_MCP_PIN_DB")
            if db_path:
                try:
                    af_map = merge_af_maps(load_capability_db(db_path).af_map(), af_map)
                except (OSError, ValueError) as exc:
                    return [content_error(
                        f"Could not load pin-capability DB '{db_path}': {exc}", code="invalid_db",
                        suggested_next_actions=["design_framework without db_path"])]
            plan = build_framework_plan(board, design=design, af_map=af_map)
            session_design["current"] = plan
            session_design["last_render"] = None
            return [content_success(
                summarize_framework(plan),
                suggested_next_actions=["describe_framework (what=unresolved)", "render_framework"])]

        elif name == "describe_framework":
            plan = session_design.get("current")
            if not plan:
                return [content_error(
                    "No framework plan for this session. Run design_framework first.", code="no_design",
                    suggested_next_actions=["design_framework"])]
            what = arguments.get("what", "summary")
            view = framework_view(plan, what)
            if view is None:
                return [content_error(
                    f"Unknown view '{what}'.", code="invalid_argument",
                    suggested_next_actions=["describe_framework (what=summary|clocks|gpio|peripherals|init_order|unresolved)"])]
            return [content_success(view)]

        elif name == "render_framework":
            plan = session_design.get("current")
            if not plan:
                return [content_error(
                    "No framework plan for this session. Run design_framework first.", code="no_design",
                    suggested_next_actions=["design_framework"])]
            rendered = render_framework(plan, style=arguments.get("style", "hal"))
            session_design["last_render"] = rendered
            return [content_success(
                rendered,
                suggested_next_actions=["synthesize_acceptance", "build_firmware"])]

        elif name == "synthesize_acceptance":
            plan = session_design.get("current")
            if not plan:
                return [content_error(
                    "No framework plan for this session. Run design_framework first.", code="no_design",
                    suggested_next_actions=["design_framework"])]
            register_map = arguments.get("register_map")
            if register_map is not None and not isinstance(register_map, dict):
                return [content_error(
                    "register_map must be an object {line_or_family: {clock: {address, bit}}}.",
                    code="invalid_argument", suggested_next_actions=["synthesize_acceptance"])]
            irq_map = arguments.get("irq_map")
            if irq_map is not None and not isinstance(irq_map, dict):
                return [content_error(
                    "irq_map must be an object {line_or_family: {irq_name: number}}.",
                    code="invalid_argument", suggested_next_actions=["synthesize_acceptance"])]
            gpio_map = arguments.get("gpio_map")
            if gpio_map is not None and not isinstance(gpio_map, dict):
                return [content_error(
                    "gpio_map must be an object {line_or_family: {port_letter: base_address}}.",
                    code="invalid_argument", suggested_next_actions=["synthesize_acceptance"])]
            mcu = plan.get("mcu") or {}
            line, family = mcu.get("line"), mcu.get("family")
            svd_loaded = getattr(svd_parser, "svd_root", None) is not None
            if register_map:
                clock_resolver, clock_source = dict_clock_resolver(register_map, line, family), "register_map"
            elif svd_loaded:
                clock_resolver, clock_source = svd_clock_resolver(svd_parser), "svd"
            else:
                clock_resolver, clock_source = None, "none"
            if irq_map:
                irq_resolver, irq_source = dict_irq_resolver(irq_map, line, family), "irq_map"
            elif svd_loaded:
                irq_resolver, irq_source = svd_irq_resolver(svd_parser), "svd"
            else:
                irq_resolver, irq_source = None, "none"
            if gpio_map:
                gpio_resolver, gpio_source = dict_gpio_resolver(gpio_map, line, family), "gpio_map"
            elif svd_loaded:
                gpio_resolver, gpio_source = svd_gpio_resolver(svd_parser), "svd"
            else:
                gpio_resolver, gpio_source = None, "none"
            options = {
                "include_no_fault": arguments.get("include_no_fault", True),
                "include_nvic": arguments.get("include_nvic", True),
                "include_gpio": arguments.get("include_gpio", True),
                "stopped_at": arguments.get("stopped_at"),
                "name": arguments.get("name"),
            }
            derived = derive_acceptance_spec(plan, clock_resolver=clock_resolver, options=options,
                                             irq_resolver=irq_resolver, gpio_resolver=gpio_resolver)
            try:
                validated = validate_acceptance_spec(derived["spec"])
            except ValueError as exc:
                return [content_error(
                    f"Derived acceptance spec is invalid: {exc}", code="invalid_spec",
                    suggested_next_actions=["synthesize_acceptance(include_no_fault=true)"])]
            # Provenance -> source (Pillar E): render the same plan, build its per-file source map,
            # and resolve each check's provenance to the exact init function + line it verifies. A
            # construct that was not emitted (TODO/unresolved) resolves to located=false, never a
            # fabricated line. The stored spec is self-contained -- run_acceptance and every loop
            # verdict then carry result.provenance.source with no further plan lookup.
            provenance_stats = annotate_spec_sources(validated, render_framework(plan).get("source_map"))
            loaded = arguments.get("load", True)
            if loaded:
                session_acceptance["current"] = validated
                session_acceptance["last_result"] = None
            next_actions = (["start_acceptance_loop", "describe_acceptance (what=checks)"] if loaded
                            else ["load_acceptance", "describe_acceptance"])
            return [content_success({
                "spec": summarize_acceptance(validated),
                "checks": validated["checks"],
                "unresolved": derived["unresolved"],
                "notes": derived["notes"],
                "stats": derived["stats"],
                "provenance": provenance_stats,
                "placement_source": clock_source,
                "resolver_sources": {"clock": clock_source, "nvic": irq_source, "gpio": gpio_source},
                "loaded": loaded,
            }, suggested_next_actions=next_actions)]

        elif name == "solve_clock_tree":
            plan = session_design.get("current")
            if not plan:
                return [content_error(
                    "No framework plan for this session. Run design_framework first.", code="no_design",
                    suggested_next_actions=["design_framework"])]
            profile_arg = arguments.get("profile")
            if profile_arg is not None and not isinstance(profile_arg, dict):
                return [content_error(
                    "profile must be an object (a device clock profile).", code="invalid_argument",
                    suggested_next_actions=["solve_clock_tree(sysclk_hz=...)"])]
            target = arguments.get("sysclk_hz") or arguments.get("target_sysclk_hz")
            if not target:
                return [content_error(
                    "Provide sysclk_hz (target SYSCLK in Hz).", code="missing_argument",
                    suggested_next_actions=["solve_clock_tree(sysclk_hz=80000000)"])]
            mcu = plan.get("mcu") or {}
            profile = profile_arg or resolve_profile(mcu.get("line"), mcu.get("family"))
            if not profile:
                return [content_success({
                    "feasible": False,
                    "unresolved": [{"type": "device_unmodelled", "line": mcu.get("line"),
                                    "family": mcu.get("family"),
                                    "detail": "No built-in clock profile for this device; pass an explicit "
                                              "profile with the datasheet PLL/bus limits."}],
                    "notes": [],
                }, suggested_next_actions=["solve_clock_tree(profile={...})"])]
            request = {
                "source": arguments.get("source"),
                "source_hz": arguments.get("source_hz"),
                "target_sysclk_hz": int(target),
                "need_48mhz": bool(arguments.get("need_48mhz")),
            }
            result = solve_clock_tree(profile, request)
            if not result["feasible"]:
                return [content_success({
                    "feasible": False,
                    "unresolved": result["unresolved"],
                    "notes": result["notes"],
                }, suggested_next_actions=["solve_clock_tree (adjust sysclk_hz / provide source_hz)"])]
            loaded = arguments.get("load", True)
            if loaded:
                plan["clock_config"] = result["solution"]  # persisted in the session plan
            return [content_success({
                "feasible": True,
                "clock": summarize_clock_solution(result),
                "solution": result["solution"],
                "notes": result["notes"],
                "loaded": loaded,
            }, suggested_next_actions=["render_framework", "synthesize_acceptance"])]

        elif name == "solve_timer":
            plan = session_design.get("current")
            if not plan:
                return [content_error(
                    "No framework plan for this session. Run design_framework first.", code="no_design",
                    suggested_next_actions=["design_framework"])]
            timer = arguments.get("timer")
            target_hz = arguments.get("target_hz")
            if target_hz is not None and not timer:
                return [content_error(
                    "target_hz requires a specific timer=; omit target_hz to use recorded design targets.",
                    code="invalid_argument", suggested_next_actions=["solve_timer(timer='TIM3', target_hz=1000)"])]
            bus = arguments.get("bus")
            if bus is not None and bus not in ("apb1", "apb2"):
                return [content_error(
                    "bus must be 'apb1' or 'apb2'.", code="invalid_argument",
                    suggested_next_actions=["solve_timer(timer='TIM3', bus='apb1')"])]
            arr_bits = arguments.get("arr_bits")
            if arr_bits is not None and arr_bits not in (16, 32):
                return [content_error(
                    "arr_bits must be 16 or 32.", code="invalid_argument",
                    suggested_next_actions=["solve_timer(arr_bits=32)"])]
            loaded = arguments.get("load", True)
            target_plan = plan if loaded else copy.deepcopy(plan)
            overrides = {timer: target_hz} if (timer and target_hz is not None) else None
            report = solve_timers_in_plan(
                target_plan, only=timer, target_overrides=overrides,
                timer_clock_hz=arguments.get("timer_clock_hz"), bus_override=bus, arr_bits_override=arr_bits)
            if not report["results"]:
                detail = (f"{timer} has no recorded target; pass target_hz=." if timer
                          else "No timer had a recorded target (design update_hz) to solve.")
                return [content_success({
                    "solved_count": 0, "timer_count": report["timer_count"], "results": [], "detail": detail,
                }, suggested_next_actions=["design_framework(design={'TIM3': {'update_hz': 1000}})"])]
            if loaded:
                session_design["current"] = target_plan
                session_design["last_render"] = None
            return [content_success({
                "solved_count": report["solved_count"],
                "timer_count": report["timer_count"],
                "results": report["results"],
                "loaded": loaded,
            }, suggested_next_actions=["render_framework", "describe_framework (what=unresolved)"])]

        elif name == "load_device_pack":
            pack_arg = arguments.get("pack")
            path = arguments.get("path")
            allow_override = bool(arguments.get("allow_override"))
            if pack_arg is None and not path:
                # No pack supplied -> report which families the deterministic solvers currently cover.
                return [content_success({
                    "action": "coverage",
                    "coverage": device_packs.coverage(),
                }, suggested_next_actions=[
                    "load_device_pack(path='pack.json')", "load_device_pack(pack={...})"])]
            if pack_arg is not None and not isinstance(pack_arg, dict):
                return [content_error(
                    "pack must be a device-pack object.", code="invalid_argument",
                    suggested_next_actions=["load_device_pack(path='pack.json')"])]
            if pack_arg is None:
                pack_arg, read_problems = device_packs.load_pack(path)
                if pack_arg is None:
                    return [content_error(
                        "Could not read device pack: " + "; ".join(read_problems), code="pack_unreadable",
                        raw_response={"problems": read_problems},
                        suggested_next_actions=["load_device_pack(path=<valid json file>)"])]
            problems = device_packs.register_pack(pack_arg, allow_override=allow_override)
            if problems:
                return [content_error(
                    "Device pack rejected: " + "; ".join(problems), code="invalid_pack",
                    raw_response={"problems": problems},
                    suggested_next_actions=["Fix the reported problems and retry"])]
            return [content_success({
                "action": "registered",
                "family": pack_arg.get("family"),
                "sections": sorted(k for k in ("clock", "dma", "nvic", "timer") if k in pack_arg),
                "coverage": device_packs.coverage(),
            }, suggested_next_actions=[
                "design_framework", "solve_clock_tree", "synthesize_acceptance"])]

        elif name == "run_pipeline":
            # Capstone: run the deterministic design DAG (Pillar G) by re-dispatching the
            # existing verified tools in order, aggregating every stage's honest gaps. It
            # never guesses and never hard-errors: a required stage failing (e.g. no board)
            # is reported honestly as pipeline_status=blocked at that stage.
            request = dict(arguments)
            outcomes: list = []
            skipped: list = []
            for stage in pipeline.STAGE_ORDER:
                plan = session_design.get("current")
                if not pipeline.wants_stage(stage, request, plan):
                    skipped.append({"stage": stage,
                                    "reason": pipeline.skip_reason(stage, request, plan)})
                    continue
                sub_args = dict(pipeline.stage_args(stage, request))
                sub_args["session"] = _sess.id  # keep every hop on this session
                env = json.loads(_dispatch_tool(stage, sub_args)[0].text)
                ok = bool(env.get("ok"))
                data = env.get("data") or {}
                error = env.get("error") or {}
                gaps = pipeline.extract_gaps(stage, data, session_design.get("current")) if ok else []
                outcomes.append({
                    "stage": stage, "ok": ok,
                    "code": error.get("code"), "message": error.get("message"),
                    "summary": _pipeline_stage_summary(stage, data),
                    "gaps": gaps,
                })
                if not ok and stage in pipeline.REQUIRED_STAGES:
                    break  # a required stage hard-failed -> blocked, stop here
            report = pipeline.consolidate(outcomes, skipped)
            # Enrich with the pipeline "products" the engineer actually wants in hand.
            plan = session_design.get("current")
            if plan:
                report["mcu"] = plan.get("mcu")
            render = session_design.get("last_render")
            if render and report["pipeline_status"] != "blocked":
                report["files"] = [{"path": f["path"], "content": f["content"]}
                                   for f in render.get("files", [])]
            accepted = session_acceptance.get("current")
            if accepted and any(o["stage"] == "synthesize_acceptance" and o["ok"] for o in outcomes):
                report["acceptance"] = summarize_acceptance(accepted)
            return [content_success(report, suggested_next_actions=_pipeline_next(report))]

        else:
            hint = _RENAMED_TOOLS.get(name)
            msg = f"Unknown tool: {name}." + (f" It was renamed — use {hint}." if hint
                                              else " Reach any tool via call(tool=..., args=...).")
            return [content_error(msg, code="unknown_tool",
                                  suggested_next_actions=["call"])]

    except KeyError as e:
        # A handler indexed a required argument that the caller omitted.
        return [content_error(
            f"Missing required argument: {e}. Provide it and retry.",
            code="missing_argument",
        )]
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
_JOURNAL_SKIP = {"get_session", "clear_session_journal"}


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict | None) -> CallToolResult:
    if arguments is None:
        arguments = {}

    if name == "run_scenario":
        return call_tool_result(await _run_scenario(arguments))

    if name == "batch":
        return call_tool_result(await _run_batch(arguments))

    if name == "call":
        inner = arguments.get("tool")
        if inner in (None, "call", "batch", "run_scenario"):
            return call_tool_result([content_error(
                "call needs a 'tool' name (not call/batch/run_scenario).", code="invalid_call")])
        return await handle_call_tool(inner, arguments.get("args", {}))

    sid = arguments.get("session") or "default"
    if sid != "default":
        session_manager.get(sid)  # create on the loop thread (race-free) before threading out
    lock = _lock_for_session(sid)
    start = time.monotonic()

    def _locked_dispatch():
        # Hold the per-session lock for the whole blocking dispatch so concurrent calls to the
        # same session can't interleave on its single GDB pipe. Waiting happens on this worker
        # thread, so the event loop keeps servicing other sessions and protocol messages.
        with lock:
            return _dispatch_tool(name, arguments)

    # GDB dispatch is synchronous and blocking (pipe IO, TCP port polling, multi-second waits
    # like run_and_wait/verify_flash); run it off the event loop so the loop stays responsive.
    result = await asyncio.to_thread(_locked_dispatch)
    duration_ms = round((time.monotonic() - start) * 1000, 1)

    # Prune the closed session's lock so _session_locks doesn't grow forever. Safe here:
    # this runs on the event-loop thread (the only thread that touches the dict) and only
    # after the dispatch above released the lock. The "default" lock is kept — that session
    # object is never removed, only stopped.
    if name == "close_session":
        closed_sid = arguments.get("session_id")
        if closed_sid and closed_sid != "default" and closed_sid not in session_manager.sessions:
            _session_locks.pop(closed_sid, None)

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

    return call_tool_result(result)


async def _run_batch(arguments: dict) -> list[TextContent]:
    """Execute several tool calls in one round trip, returning all full results."""
    steps = arguments.get("steps") or []
    stop_on_error = arguments.get("stop_on_error", False)
    results = []
    for step in steps:
        payload = json.loads((await handle_call_tool(step["tool"], step.get("args", {})))[0].text)
        ok = bool(payload.get("ok"))
        results.append({"tool": step["tool"], "ok": ok, "data": payload.get("data"), "error": payload.get("error")})
        if stop_on_error and not ok:
            break
    return [content_success({"results": results, "count": len(results), "total": len(steps)})]


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
