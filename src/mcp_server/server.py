import asyncio
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
import types

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from . import build as build_mod
from . import swo_config
from .acceptance_eval import GdbAcceptanceReader, evaluate_acceptance
from .acceptance_model import summarize_acceptance, validate_acceptance_spec
from .acceptance_synth import derive_acceptance_spec, dict_clock_resolver, svd_clock_resolver
from .board_model import board_view, summarize_board
from .board_validation import load_capability_db, validate_board
from .clock_solver import resolve_profile, solve_clock_tree, summarize_clock_solution
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
from .debug_session import SessionManager
from .debug_snapshot import collect_debug_snapshot
from .error_taxonomy import classify_error
from .exception_frame import build_fault_context
from .fault_analysis import diagnose_fault_registers
from .framework_render import render_framework
from .framework_solver import build_framework_plan, framework_view, summarize_framework
from .freertos_inspector import FreeRTOSInspector
from .gdb_client import GdbClientManager
from .gdb_decode import registers_summary
from .gdb_manager import GdbServerManager
from .issue_reporter import DEFAULT_REPO, build_issue_body, file_issue, issue_fingerprint
from .log_reader import FileLogReader, ProcessLogReader, SerialLogReader
from .loop_control import loop_decision, new_loop_state, summarize_loop
from .loop_orchestrator import GdbLoopSteps, run_iteration
from .memory_guard import MemoryWriteGuard
from .metrics import compute_metrics
from .netlist_parser import load_netlist_file, parse_netlist
from .openocd_config import find_openocd_scripts, suggest_server_args
from .project_inspector import inspect_project
from .reliability import retry_call
from .reset_strategy import resolve_reset_command
from .scenario import load_scenario, replay_scenario, step_summary
from .self_check import evaluate_self_check
from .session_journal import SessionJournal
from .stack_analysis import stack_report
from .svd_parser import SVDParser
from .tool_response import content_error, content_success
from .tracker import VariableTracker

SERVER_INSTRUCTIONS = """\
STM32 on-chip debugging over GDB + OpenOCD/ST-Link/J-Link. Drive it as a loop:
observe -> orient (symbolize) -> hypothesize -> act safely -> verify.

Tool not in your list? Some clients cap how many tools they expose, so a tool you need
(e.g. start_debug_session) may be hidden. Reach ANY tool via call(tool="<name>", args={...}),
or run several with batch — these always work even when the tool isn't directly listed.

The surface is lean: related ops are action-dispatched families — pass the discriminator.
breakpoint(action=set|delete|list|watch), logging(action=start|stop|get|clear, channel=…),
expressions(action=assert|capture|compare), debug_profile(action=get|set),
debug_config(action=load|save|validate), read_registers(what=core|fault|cycle),
inspect_symbol(what=size|type|address|resolve|functions|variables), frame(action=select|source|
variables), write_guard(action=policy|audit), coredump, timeouts, typed_memory, snapshot,
session_diagnostics. (Old standalone names still work if you call them.)

Core workflow:
0. Need OpenOCD server_args? Call suggest_server_args(mcu, probe) — it returns the
   right -f interface/target cfgs (validated against OpenOCD's bundled scripts).
   NEVER search the disk for .cfg files; OpenOCD resolves them from its scripts dir.
1. start_debug_session, then ALWAYS run self_check first — it validates byte order,
   the Cortex-M core, and the device family, catching link/config faults early.
2. Set debug_profile(action=set, mcu, elf_path, svd_path) — symbols then auto-load on every
   connect/recover_session (symbols are per-session). To load symbols mid-session without
   flashing, call load_symbols. Without symbols, breakpoints on function names won't resolve.
3. Reproduce with the fewest calls: prefer the composites over manual sequences —
   flash_and_run (ELF -> halted at entry), debug_until (conditional breakpoint + run +
   decoded backtrace/locals in one call), capture_state ("where am I" in one call).
4. Diagnose a crash with reconstruct_fault_context: it unwinds the stacked exception
   frame and resolves the true faulting PC to source file:line.
5. Verify a fix with expressions(action=compare) / expressions(action=assert).

Key rules (the target must cooperate):
- Reads (registers/memory/frames) require a HALTED core. If a read fails with
  target_unresponsive, the core is running — call halt_execution first.
- run_and_wait returns a structured stop event; on timeout it leaves the core RUNNING.
- A breakpoint TIMEOUT means the code path was NOT reached — do NOT just retry run_and_wait.
  The location is usually gated by a flag/state/stimulus. Instead: halt_execution, then
  capture_state + breakpoint(action=list) (hit_count=0 confirms it was never reached); read the
  gating flag; then either set a breakpoint EARLIER on the path (or where the flag is set),
  drive the precondition (write the flag/variable, send the UART/input stimulus), or use a
  conditional breakpoint. Forcing a flag via write_memory changes behavior — note it.
- Memory writes are guarded: option bytes, IWDG, and WWDG are blocked by default;
  use write_guard(action=policy) to allow, or dry_run to simulate. Every write is audited.
- If halting causes mysterious resets, configure_debug_freeze (freeze IWDG/WWDG/timers).
- On probe_unavailable / connection_lost, call recover_session; tune flaky probes with
  timeouts(action=set).
- The ST-Link SWD/debug interface is EXCLUSIVE: while this MCP session is active, never
  start a second OpenOCD/GDB on the same probe (e.g. a verify script's own --reset will
  fail with "ST-Link in use"). Reset via reset_target instead. The ST-Link virtual COM
  port (e.g. COM3) is a SEPARATE USB endpoint and DOES coexist with debugging — read it
  with logging(action=start, channel="uart"), or let an external serial script use it without resetting.

Observability: to find where firmware spends time or what loop it is stuck in, use
sample_pc — a non-intrusive statistical profiler that runs over SWD (no SWO pin / firmware
change) and returns a symbolized hot-spot histogram. For printf-over-SWO, call
setup_swo(hclk_hz, swo_hz) once (it configures TPIU+ITM from the debugger), then capture with
logging(action=start, channel="swo", file=<output>) — no external decoder needed.

Determinism & sharing: every call is journaled — review it with get_session(view=journal|
timeline|metrics). Replay a repro with run_scenario; bundle a full, shareable report
with export_debug_report. Most results carry suggested_next_actions — follow them.

Self-reporting: if a tool behaves wrongly, gives a confusing result, or you get stuck on
what looks like an MCP bug (not a target bug), call report_issue(title, description) — it
files a structured GitHub issue auto-bundling this session's journal so the MCP can be fixed.

Multiple boards: pass session="<name>" to ANY tool to target an isolated debug session
(its own connection, profile, breakpoints, logs). Omit it for the single 'default' session.
list_sessions / close_session manage them. For truly concurrent OpenOCD instances, give each
session distinct server_args (gdb_port and adapter serial).
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
swo_file_reader = FileLogReader("swo")
uart_log_reader = SerialLogReader()
memory_guard = MemoryWriteGuard()
session_journal = SessionJournal()
_last_session = {"server_type": None, "server_args": []}
_board = {"current": None}  # imported BoardDescription (netlist -> BSP model) for the default session
_acceptance = {"current": None, "last_result": None}  # loaded AcceptanceSpec + last verdict (default session)
_loop = {"current": None}  # bounded acceptance-loop state (Pillar C) for the default session
_design = {"current": None, "last_render": None}  # FrameworkPlan + last render (Pillar D) for default session
_reported_issues = {}  # fingerprint -> issue url (in-session dedup)

# Phase 3: named per-target sessions for multi-board / CI. The "default" session reuses
# the module globals above (single-target back-compat + existing tests); named sessions get
# fully isolated objects from the SessionManager.
session_manager = SessionManager()
_SESSION_ATTRS = ("gdb_manager", "gdb_client", "svd_parser", "variable_tracker",
                  "debug_profile", "freertos_inspector", "rtt_log_reader", "swo_log_reader",
                  "swo_file_reader", "uart_log_reader", "memory_guard", "last_session", "board",
                  "acceptance", "loop", "design")
# Session attrs whose "default" backing global is named differently from the attribute.
_DEFAULT_SESSION_GLOBALS = {"last_session": "_last_session", "board": "_board",
                            "acceptance": "_acceptance", "loop": "_loop", "design": "_design"}


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


def _mcp_version() -> str:
    try:
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        out = subprocess.run(
            ["git", "-C", root, "rev-parse", "--short", "HEAD"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return "unknown"

# Structured logging to stderr (stdout is the MCP transport), correlated by run-id.
logger = logging.getLogger("stm32-gdb-mcp")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)

@server.list_tools()
async def handle_list_tools() -> list[Tool]:
    _tools = [
        # --- Step 4: Basic Control and Flashing ---
        Tool(
            name="start_debug_session",
            description="Starts the specified GDB Server (openocd, stlink, jlink) and connects the GDB Client to it. "
                        "openocd REQUIRES server_args naming the probe and target, e.g. "
                        "['-f','interface/stlink.cfg','-f','target/stm32l4x.cfg'] — without them OpenOCD cannot "
                        "find a config or adapter. Or call load_debug_config first to supply them.",
            inputSchema={
                "type": "object",
                "properties": {
                    "server_type": {"type": "string", "enum": ["openocd", "stlink", "jlink"], "description": "Type of debug server backend."},
                    "server_args": {"type": "array", "items": {"type": "string"}, "description": "Optional args for the server e.g. ['-f', 'interface/stlink.cfg', '-f', 'target/stm32f4x.cfg']"},
                    "serial": {"type": "string", "description": "Probe/ST-Link serial to select a specific board (for concurrent multi-target). Auto-added as 'adapter serial <serial>'."}
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
            name="suggest_server_args",
            description="Returns the correct OpenOCD server_args for an MCU family + probe (e.g. "
                        "STM32L431 + stlink -> ['-f','interface/stlink.cfg','-f','target/stm32l4x.cfg']) "
                        "and validates the config files exist in OpenOCD's bundled scripts dir. Call "
                        "this to get start_debug_session args — do NOT search the disk for .cfg files.",
            inputSchema={
                "type": "object",
                "properties": {
                    "mcu": {"type": "string", "description": "MCU or family, e.g. 'STM32L431' or 'STM32F4'."},
                    "probe": {"type": "string", "description": "Debug probe: stlink, jlink, or cmsis-dap."}
                },
                "required": ["mcu", "probe"]
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
                    "target": {"type": "string", "description": "cmake/make: build target."},
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
            name="list_breakpoints",
            description="Lists breakpoints with their HIT COUNTS. A hit_count of 0 means the code "
                        "path was never reached — so a run_and_wait timeout means the precondition "
                        "(a flag/state) to get there was not satisfied. Use this instead of retrying.",
            inputSchema={"type": "object", "properties": {}}
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
            name="analyze_stack",
            description="Reports stack used/free bytes and a clear overflow verdict for the halted "
                        "core. stack_top defaults to the initial MSP (first word of the vector table "
                        "at vector_table_addr). Give stack_size or stack_limit for the overflow check "
                        "(else only usage is reported). The key tool for diagnosing stack overflows.",
            inputSchema={
                "type": "object",
                "properties": {
                    "stack_top": {"type": "string", "description": "Top-of-stack address (hex). Default: initial MSP from the vector table."},
                    "stack_limit": {"type": "string", "description": "Lowest valid stack address (hex)."},
                    "stack_size": {"type": "string", "description": "Stack size in bytes (used as stack_top - stack_size if stack_limit omitted)."},
                    "vector_table_addr": {"type": "string", "description": "Vector table base for the initial MSP (default '0x08000000')."}
                }
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
            name="read_freertos",
            description="Reads FreeRTOS state by 'what': 'current_task' (pxCurrentTCB), 'tasks' (ready "
                        "list: names/priorities/TCB/stack), 'task_lists' (ready/delayed/suspended/"
                        "deleted), 'queue' or 'mutex' (needs handle = a Queue_t expression), or 'heap' "
                        "(heap_4/5 usage).",
            inputSchema={
                "type": "object",
                "properties": {
                    "what": {"type": "string", "enum": ["current_task", "tasks", "task_lists", "queue", "mutex", "heap"]},
                    "handle": {"type": "string", "description": "For 'queue'/'mutex': GDB expression for the Queue_t pointer/handle."},
                    "max_priorities": {"type": "integer", "description": "tasks/task_lists: priority count override."},
                    "max_tasks": {"type": "integer", "description": "tasks/task_lists: max tasks to return."}
                },
                "required": ["what"]
            }
        ),
        Tool(
            name="capture_rtos_snapshot",
            description="Captures FreeRTOS detection, current task, ready tasks, and task-list snapshot.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="start_logging",
            description="Starts background log capture on a channel: 'rtt' (SEGGER RTT, default cmd "
                        "JLinkRTTClient), 'swo' (ITM printf — run setup_swo then pass file=<output>; "
                        "or command=<external decoder>), or 'uart' (serial, port required). "
                        "One tool for all three channels.",
            inputSchema={
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "enum": ["rtt", "swo", "uart"], "description": "Log channel."},
                    "command": {"type": "string", "description": "rtt/swo: executable to launch (rtt defaults to JLinkRTTClient)."},
                    "args": {"type": "array", "items": {"type": "string"}, "description": "rtt/swo: command arguments."},
                    "file": {"type": "string", "description": "swo: tail OpenOCD's ITM decode file (run setup_swo first) — no external decoder needed."},
                    "port": {"type": "string", "description": "uart: serial port, e.g. COM3 or /dev/ttyUSB0."},
                    "baudrate": {"type": "integer", "description": "uart: baudrate (default 115200)."},
                    "timeout": {"type": "number", "description": "uart: read timeout seconds (default 0.1)."}
                },
                "required": ["channel"]
            }
        ),
        Tool(
            name="stop_logging",
            description="Stops background log capture on a channel (rtt/swo/uart).",
            inputSchema={
                "type": "object",
                "properties": {"channel": {"type": "string", "enum": ["rtt", "swo", "uart"]}},
                "required": ["channel"]
            }
        ),
        Tool(
            name="get_logs",
            description="Returns captured log lines for a channel (rtt/swo/uart).",
            inputSchema={
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "enum": ["rtt", "swo", "uart"]},
                    "limit": {"type": "integer", "description": "Max recent entries to return."},
                    "since_index": {"type": "integer", "description": "Only entries with index greater than this."},
                    "clear": {"type": "boolean", "description": "Clear returned entries after reading."}
                },
                "required": ["channel"]
            }
        ),
        Tool(
            name="clear_logs",
            description="Clears the buffered log lines for a channel (rtt/swo/uart) without stopping capture.",
            inputSchema={
                "type": "object",
                "properties": {"channel": {"type": "string", "enum": ["rtt", "swo", "uart"]}},
                "required": ["channel"]
            }
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
            name="setup_swo",
            description="One-call SWO/ITM printf setup. Configures the target's TPIU+ITM from the "
                        "debugger (no firmware ITM-init needed) for the given core clock (hclk_hz) and "
                        "SWO baud, and returns the OpenOCD capture commands + the firmware printf "
                        "retarget snippet. Then capture with logging(action=start, channel='swo', "
                        "file=<output>). Requires the SWO pin (e.g. PB3) wired to the probe.",
            inputSchema={
                "type": "object",
                "properties": {
                    "hclk_hz": {"type": "integer", "description": "Core clock (HCLK) in Hz, e.g. 80000000 for an L4 at 80 MHz."},
                    "swo_hz": {"type": "integer", "description": "Desired SWO baud (default 2000000; ST-Link V2 max ~2 MHz)."},
                    "port": {"type": "integer", "description": "ITM stimulus port for printf (default 0)."},
                    "output": {"type": "string", "description": "OpenOCD ITM decode output file to tail (default 'swo_itm.log')."},
                    "tpiu_name": {"type": "string", "description": "OpenOCD tpiu object name if not the default '$_CHIPNAME.tpiu'."},
                    "apply_openocd": {"type": "boolean", "description": "Also run the OpenOCD capture commands via monitor now (default false)."}
                },
                "required": ["hclk_hz"]
            }
        ),
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
                        "unreadable target is reported as 'error', never a silent pass. Run "
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
            name="design_framework",
            description="Synthesize a deterministic FrameworkPlan (Pillar D) from the session's imported "
                        "board: which clocks to enable, how each pin must be muxed, and which peripheral "
                        "init blocks to emit, in dependency order. Supply per-peripheral HAL .Init "
                        "parameters via design={'USART1': {'baud': 115200, ...}} and optional AF numbers "
                        "via af_map. Anything not supplied is surfaced as unresolved, never guessed. "
                        "Import a netlist first (import_netlist).",
            inputSchema={
                "type": "object",
                "properties": {
                    "design": {"type": "object", "description": "Per-peripheral config, e.g. {'USART1': {'baud': 115200, 'word_length': 'UART_WORDLENGTH_8B'}}."},
                    "af_map": {"type": "object", "description": "Optional alternate-function numbers: {line_or_family: {port_pin: {'USART1_TX': 7}}}."},
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
                        "HardFault); adds a memory_u32 bits_set check per clock the plan enables, using RCC "
                        "enable-bit placements resolved from the session's loaded SVD (or an explicit "
                        "register_map). Unresolvable clocks are surfaced, never guessed. Run "
                        "design_framework first; load an SVD (start_debug_session/set svd) for clock checks.",
            inputSchema={
                "type": "object",
                "properties": {
                    "register_map": {"type": "object", "description": "Optional explicit RCC placements {line_or_family: {clock: {address, bit}}}; overrides the SVD."},
                    "stopped_at": {"type": "string", "description": "Optional symbol to also assert the PC reached after init (e.g. 'main')."},
                    "include_no_fault": {"type": "boolean", "description": "Emit the no_fault check (default true)."},
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
        )
    ]
    # Compact mode (STM32_GDB_MCP_COMPACT=1): expose only a small core so nothing gets
    # truncated under tight client tool-count caps. Every other tool is still reachable
    # via call(tool, args).
    advertised = _advertised_tools(_tools)
    if os.environ.get("STM32_GDB_MCP_COMPACT"):
        return [t for t in advertised if t.name in _CORE_TOOLS]
    return advertised


# Tools consolidated in the slim-down: map old name -> new call form for clear errors.
_RENAMED_TOOLS = {
    **{f"{a}_{c}_log{s}": f'{a}_log{s}(channel="{c}")'
       for c in ("rtt", "swo", "uart")
       for a, s in (("start", "ging"), ("stop", "ging"), ("get", "s"), ("clear", "s"))},
    "step_over": 'step(kind="over")', "step_into": 'step(kind="into")',
    "step_out": 'step(kind="out")', "step_instruction": 'step(kind="instruction")',
    "read_current_task": 'read_freertos(what="current_task")',
    "read_freertos_tasks": 'read_freertos(what="tasks")',
    "read_freertos_task_lists": 'read_freertos(what="task_lists")',
    "read_freertos_queue": 'read_freertos(what="queue", handle=...)',
    "read_freertos_mutex": 'read_freertos(what="mutex", handle=...)',
    "read_freertos_heap": 'read_freertos(what="heap")',
    "start_variable_tracking": 'track_variable(action="start")',
    "stop_variable_tracking": 'track_variable(action="stop")',
    "get_tracked_data": 'track_variable(action="get")',
    "get_session_journal": 'get_session(view="journal")',
    "get_session_timeline": 'get_session(view="timeline")',
    "get_session_metrics": 'get_session(view="metrics")',
}


# Core tools kept visible in compact mode; everything else is reached via `call`.
_CORE_TOOLS = {
    "suggest_server_args", "start_debug_session", "stop_debug_session", "recover_session",
    "self_check", "debug_profile", "load_symbols",
    "build_firmware", "flash_firmware", "flash_and_run",
    "reset_target", "halt_execution", "run_and_wait", "breakpoint",
    "debug_until", "capture_state",
    "read_memory", "write_memory", "read_variable", "read_call_stack",
    "reconstruct_fault_context", "analyze_stack",
    "logging", "read_peripheral_register",
    "batch", "call", "run_scenario", "get_session", "report_issue",
    "list_sessions", "close_session",
}


# Tool-surface consolidation: a dozen single-purpose tools collapse into action-dispatched
# families (superpowers-style lean surface). Each merged tool routes to the existing,
# already-tested handler, so every old name still works when called via `call` or directly.
# Spec: merged_name -> (discriminator, {value: underlying_tool}, summary, arg_help).
_MERGED = {
    "logging": ("action",
        {"start": "start_logging", "stop": "stop_logging", "get": "get_logs", "clear": "clear_logs"},
        "Firmware log capture over a channel.",
        "action=start|stop|get|clear; channel=rtt|swo|uart (start also takes the channel's config args)."),
    "breakpoint": ("action",
        {"set": "set_breakpoint", "delete": "delete_breakpoint", "list": "list_breakpoints", "watch": "set_watchpoint"},
        "Breakpoint / watchpoint management.",
        "action=set(location[,condition,temporary,commands]) | delete(number) | list | watch(expression)."),
    "expressions": ("action",
        {"assert": "assert_expressions", "capture": "capture_expressions", "compare": "compare_expressions_after_action"},
        "Evaluate C/GDB expressions.",
        "action=assert(expressions) | capture(expressions) | compare(expressions, action_to_run_between)."),
    "coredump": ("action",
        {"capture": "capture_coredump", "load": "load_coredump"},
        "Core-dump capture / load.",
        "action=capture(path) | load(path)."),
    "timeouts": ("action",
        {"get": "get_timeouts", "set": "set_timeouts"},
        "GDB operation timeouts.",
        "action=get | set(connect,reset,memory,...)."),
    "debug_config": ("action",
        {"load": "load_debug_config", "save": "save_debug_config", "validate": "validate_debug_config"},
        "Debug-config file (.json) management.",
        "action=load(path) | save(path) | validate(path)."),
    "debug_profile": ("action",
        {"get": "get_debug_profile", "set": "set_debug_profile"},
        "Active debug profile (mcu/elf/svd/probe).",
        "action=get | set(mcu,elf_path,svd_path,...)."),
    "read_registers": ("what",
        {"core": "read_core_registers", "fault": "read_fault_registers", "cycle": "read_cycle_counter"},
        "Read CPU register groups.",
        "what=core | fault(CFSR/HFSR decode) | cycle(DWT cycle counter)."),
    "inspect_symbol": ("what",
        {"size": "sizeof", "type": "lookup_type", "address": "address_of",
         "resolve": "resolve_address", "functions": "list_functions", "variables": "list_variables"},
        "Symbol / type introspection.",
        "what=size(type) | type(name) | address(symbol) | resolve(address) | functions(regex) | variables."),
    "typed_memory": ("action",
        {"read": "read_typed_memory", "write": "write_typed_memory"},
        "Typed (struct-aware) memory access.",
        "action=read(address,type) | write(address,type,value)."),
    "write_guard": ("action",
        {"policy": "set_write_policy", "audit": "get_write_audit_log"},
        "Memory-write guardrail.",
        "action=policy(mode,allow) | audit."),
    "snapshot": ("scope",
        {"full": "capture_debug_snapshot", "rtos": "capture_rtos_snapshot"},
        "One-shot diagnostic snapshot.",
        "scope=full(regs+stack+faults) | rtos(task/queue state)."),
    "frame": ("action",
        {"select": "select_frame", "source": "list_source", "variables": "read_frame_variables"},
        "Stack-frame navigation.",
        "action=select(number) | source(around a frame) | variables(of selected frame)."),
    "session_diagnostics": ("what",
        {"health": "check_session_health", "events": "get_gdb_events", "server_logs": "get_gdb_server_logs"},
        "Session/transport diagnostics.",
        "what=health | events(recent GDB/MI) | server_logs(GDB-server stderr)."),
}
_MERGED_AWAY = {old for _, mapping, *_ in _MERGED.values() for old in mapping.values()}
_MERGED_TOOLS = [
    Tool(
        name=mname,
        description=f"{summary} {arg_help}",
        inputSchema={
            "type": "object",
            "properties": {disc: {"type": "string", "enum": list(mapping),
                                  "description": "Which operation to perform."}},
            "required": [disc],
            "additionalProperties": True,
        },
    )
    for mname, (disc, mapping, summary, arg_help) in _MERGED.items()
]


def _advertised_tools(base: list) -> list:
    """The lean public surface: drop merged-away singles, add the action-dispatched families."""
    return [t for t in base if t.name not in _MERGED_AWAY] + _MERGED_TOOLS


def _swo_reader(sess):
    """The active SWO reader: the OpenOCD-file tailer if it's running, else the process one."""
    return sess.swo_file_reader if sess.swo_file_reader.is_running() else sess.swo_log_reader


def _log_reader(sess, channel: str):
    readers = {"rtt": sess.rtt_log_reader, "swo": _swo_reader(sess), "uart": sess.uart_log_reader}
    if channel not in readers:
        raise ValueError(f"Unknown log channel '{channel}'. Use rtt, swo, or uart.")
    return readers[channel]


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
    swo_file_reader = _sess.swo_file_reader
    uart_log_reader = _sess.uart_log_reader
    _last_session = _sess.last_session
    session_board = _sess.board
    session_acceptance = _sess.acceptance
    session_loop = _sess.loop
    session_design = _sess.design

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

        if name == "start_debug_session":
            server_type = arguments["server_type"]
            args = list(arguments.get("server_args", []))
            if server_type == "openocd" and not args:
                return [content_error(
                    "openocd requires server_args naming the probe interface and target, e.g. "
                    "['-f','interface/stlink.cfg','-f','target/stm32l4x.cfg']. Pass server_args, or "
                    "load a debug config (load_debug_config) that defines them.",
                    code="invalid_server_args",
                    suggested_next_actions=["load_debug_config", "inspect_project"],
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
                serial = arguments.get("serial") or getattr(_sess, "serial", None)
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
            port = retry_call(lambda: gdb_manager.start(server_type, args), attempts=3, backoff_base=0.8)
            gdb_client.start_gdb()
            resp = gdb_client.connect("localhost", port)
            _last_session["server_type"] = server_type
            _last_session["server_args"] = args
            symbols = _autoload_symbols(_sess)
            return [content_success(
                {"message": "Debug session started", "server_type": server_type, "port": port, "symbols_loaded": symbols},
                raw_response=resp,
            )]

        elif name == "suggest_server_args":
            scripts_dir = find_openocd_scripts()
            result = suggest_server_args(arguments["mcu"], arguments["probe"], scripts_dir=scripts_dir)
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
            symbols = _autoload_symbols(_sess)
            return [content_success(
                {"message": "Session recovered", "server_type": _last_session["server_type"], "port": port, "symbols_loaded": symbols},
                raw_response=resp,
                suggested_next_actions=["self_check", "check_session_health"],
            )]

        elif name == "stop_debug_session":
            gdb_client.stop_gdb()
            gdb_manager.stop()
            variable_tracker.stop()
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
            payload = {
                "kind": kind,
                "command": cmd,
                "returncode": result["returncode"],
                "success": success,
                "log_tail": result["output"][-4000:],
            }
            if success:
                return [content_success(payload, suggested_next_actions=["flash_firmware", "flash_and_run"])]
            return [content_error(
                f"Build failed (exit {result['returncode']})",
                code="build_failed",
                raw_response=payload,
                suggested_next_actions=["get_session"],
            )]

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

        elif name == "list_breakpoints":
            bps = gdb_client.list_breakpoints_decoded()
            never_hit = [b["number"] for b in bps if b.get("hit_count") == 0]
            summary = (f"{len(bps)} breakpoints; never reached (hit_count=0): {never_hit}"
                       if never_hit else f"{len(bps)} breakpoints, all reached at least once")
            return [content_success({"breakpoints": bps, "summary": summary})]

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

        elif name == "analyze_stack":
            sp = gdb_client.read_register_value("$sp")
            if arguments.get("stack_top") is not None:
                stack_top = int(str(arguments["stack_top"]), 0)
            else:
                vt = int(str(arguments.get("vector_table_addr", "0x08000000")), 0)
                stack_top = gdb_client.read_word(vt)  # initial MSP = first vector
            stack_limit = None
            if arguments.get("stack_limit") is not None:
                stack_limit = int(str(arguments["stack_limit"]), 0)
            elif arguments.get("stack_size") is not None:
                stack_limit = stack_top - int(str(arguments["stack_size"]), 0)
            report = stack_report(sp, stack_top, stack_limit)
            return [content_success(
                report,
                suggested_next_actions=["read_call_stack", "reconstruct_fault_context", "read_freertos_tasks"],
            )]

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

        elif name == "read_freertos":
            what = arguments["what"]
            if what == "current_task":
                result = freertos_inspector.read_current_task()
            elif what == "tasks":
                result = freertos_inspector.read_tasks(
                    max_priorities=arguments.get("max_priorities"),
                    max_tasks=arguments.get("max_tasks", 64),
                )
            elif what == "task_lists":
                result = freertos_inspector.read_task_lists(
                    max_priorities=arguments.get("max_priorities"),
                    max_tasks=arguments.get("max_tasks", 128),
                )
            elif what == "queue":
                result = freertos_inspector.read_queue(arguments["handle"])
            elif what == "mutex":
                result = freertos_inspector.read_mutex(arguments["handle"])
            elif what == "heap":
                result = freertos_inspector.read_heap()
            else:
                raise ValueError(f"Unknown read_freertos what '{what}'.")
            return [content_success({"what": what, **result} if isinstance(result, dict) else {"what": what, "result": result})]

        elif name == "capture_rtos_snapshot":
            result = freertos_inspector.capture_snapshot()
            return [content_success(result)]

        elif name == "start_logging":
            channel = arguments["channel"]
            if channel == "uart":
                reader = uart_log_reader
                reader.start(
                    port=arguments["port"],
                    baudrate=arguments.get("baudrate", 115200),
                    timeout=arguments.get("timeout", 0.1),
                )
            elif channel == "swo" and arguments.get("file"):
                # Out-of-the-box SWO printf: tail OpenOCD's internal ITM decode file,
                # no external decoder needed. Pair with setup_swo + '-output <file>'.
                reader = swo_file_reader
                reader.start(arguments["file"])
            else:  # rtt / swo: a process whose stdout is captured (e.g. JLinkRTTClient)
                reader = rtt_log_reader if channel == "rtt" else swo_log_reader
                default_cmd = "JLinkRTTClient" if channel == "rtt" else None
                command = [arguments.get("command", default_cmd)]
                if command[0] is None:
                    return [content_error(
                        "swo logging needs either file=<OpenOCD ITM output file> (use setup_swo "
                        "first) or command=<external decoder>.", code="missing_command")]
                command.extend(arguments.get("args", []))
                reader.start(command)
            return [content_success({"channel": channel, **reader.status()})]

        elif name == "stop_logging":
            reader = _log_reader(_sess, arguments["channel"])
            reader.stop()
            return [content_success({"channel": arguments["channel"], **reader.status()})]

        elif name == "get_logs":
            reader = _log_reader(_sess, arguments["channel"])
            return [content_success({
                "channel": arguments["channel"],
                "status": reader.status(),
                "entries": reader.get_logs(
                    limit=arguments.get("limit"),
                    since_index=arguments.get("since_index"),
                    clear=arguments.get("clear", False),
                ),
            })]

        elif name == "clear_logs":
            reader = _log_reader(_sess, arguments["channel"])
            reader.clear()
            return [content_success({"message": f"{arguments['channel']} log buffer cleared", "channel": arguments["channel"]})]

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

        elif name == "setup_swo":
            hclk_hz = int(arguments["hclk_hz"])
            swo_hz = int(arguments.get("swo_hz", 2_000_000))
            port = int(arguments.get("port", 0))
            output = arguments.get("output", "swo_itm.log")
            prescaler, achieved, exact = swo_config.swo_prescaler(hclk_hz, swo_hz)

            # Configure TPIU+ITM straight from the debugger (version-independent, no fw init).
            writes = swo_config.itm_tpiu_setup_writes(hclk_hz, swo_hz, port)
            for address, value in writes:
                gdb_client.write_typed_memory(hex(address), hex(value), width_bits=32)

            openocd_cmds = swo_config.openocd_swo_capture_commands(
                hclk_hz, swo_hz, output, port,
                tpiu_name=arguments.get("tpiu_name", "$_CHIPNAME.tpiu"))
            applied = []
            if arguments.get("apply_openocd"):
                for cmd in openocd_cmds:
                    try:
                        gdb_client.execute_cli_command(f"monitor {cmd}", timeout_sec=3.0)
                        applied.append({"command": cmd, "ok": True})
                    except Exception as exc:  # version/target-name variance — report, don't fail
                        applied.append({"command": cmd, "ok": False, "error": str(exc)})

            return [content_success({
                "message": f"ITM/TPIU configured for SWO printf on port {port}; "
                           f"SWO baud {achieved} Hz" + ("" if exact else f" (requested {swo_hz}, nearest divisor)"),
                "hclk_hz": hclk_hz, "swo_hz": achieved, "prescaler": prescaler, "exact_baud": exact,
                "target_writes": [{"address": f"0x{a:08x}", "value": f"0x{v:08x}"} for a, v in writes],
                "openocd_commands": openocd_cmds,
                "openocd_applied": applied,
                "output_file": output,
                "firmware_retarget": swo_config.printf_retarget_snippet(),
                "note": "SWO needs the trace pin (e.g. PB3) wired to the probe's SWO. printf "
                        "still requires the firmware to send chars to the ITM stimulus port.",
            }, suggested_next_actions=[f"logging (action=start, channel=swo, file={output})", "get_logs"])]

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
            af_map = arguments.get("af_map")
            if af_map is not None and not isinstance(af_map, dict):
                return [content_error(
                    "af_map must be an object {line_or_family: {port_pin: {'PERIPH_SIG': af}}}.",
                    code="invalid_argument", suggested_next_actions=["design_framework"])]
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
            mcu = plan.get("mcu") or {}
            if register_map:
                resolver = dict_clock_resolver(register_map, mcu.get("line"), mcu.get("family"))
                source = "register_map"
            elif getattr(svd_parser, "svd_root", None) is not None:
                resolver = svd_clock_resolver(svd_parser)
                source = "svd"
            else:
                resolver = None
                source = "none"
            options = {
                "include_no_fault": arguments.get("include_no_fault", True),
                "stopped_at": arguments.get("stopped_at"),
                "name": arguments.get("name"),
            }
            derived = derive_acceptance_spec(plan, clock_resolver=resolver, options=options)
            try:
                validated = validate_acceptance_spec(derived["spec"])
            except ValueError as exc:
                return [content_error(
                    f"Derived acceptance spec is invalid: {exc}", code="invalid_spec",
                    suggested_next_actions=["synthesize_acceptance(include_no_fault=true)"])]
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
                "placement_source": source,
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
async def handle_call_tool(name: str, arguments: dict | None) -> list[TextContent]:
    if arguments is None:
        arguments = {}

    if name == "run_scenario":
        return await _run_scenario(arguments)

    if name == "batch":
        return await _run_batch(arguments)

    if name == "call":
        inner = arguments.get("tool")
        if inner in (None, "call", "batch", "run_scenario"):
            return [content_error(
                "call needs a 'tool' name (not call/batch/run_scenario).", code="invalid_call")]
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
