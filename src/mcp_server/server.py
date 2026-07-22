import asyncio
import json
import logging
import os
import sys
import threading
import time
import types

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool

from . import pipeline
from .acceptance_model import summarize_acceptance

# Composites look statically unused once their handlers move to domain modules, but they
# must stay module globals: _make_context reads them via globals() on every dispatch so
# tests that monkeypatch mcp_server.server.<name> keep working (ctx.fns.<name>).
from .composites import capture_state, debug_until, flash_and_run, run_for_duration  # noqa: F401
from .debug_profile import DebugProfileStore
from .debug_session import SessionManager, teardown_debug_session
from .error_taxonomy import classify_error
from .freertos_inspector import FreeRTOSInspector
from .gdb_client import GdbClientManager
from .gdb_manager import GdbServerManager

# file_issue stays a module global for the same reason as the composites above.
from .issue_reporter import file_issue  # noqa: F401
from .log_reader import FileLogReader, ProcessLogReader, SerialLogReader
from .memory_guard import MemoryWriteGuard
from .openocd_config import detect_probe, find_openocd_scripts, suggest_server_args
from .reliability import retry_call
from .scenario import load_scenario, replay_scenario, step_summary
from .self_check import evaluate_self_check
from .server_metadata import SERVER_INSTRUCTIONS
from .session_journal import SessionJournal
from .svd_parser import SVDParser
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
from .tools._helpers import autoload_symbols as _autoload_symbols
from .tools._helpers import recover_current_session as _recover_current_session
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
        # (tool_help moved to tools/meta_tools.py)
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
        # (build_firmware .. flash_firmware moved to tools/firmware_tools.py)
        # (reset_target moved to tools/execution_tools.py)
        # --- Step 5: Core Debug Interaction ---
        # (set_breakpoint .. delete_breakpoint moved to tools/breakpoint_tools.py)
        # (continue_execution .. capture_state moved to tools/execution_tools.py)
        # (flash_and_run moved to tools/firmware_tools.py)
        # (run_for_duration .. step moved to tools/execution_tools.py)
        # (read_variable .. get_write_audit_log moved to tools/memory_tools.py)
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
        # (read_fault_registers .. capture_debug_snapshot moved to tools/fault_tools.py)
        # (inspect_project moved to tools/config_tools.py)
        # (detect_rtos .. capture_rtos_snapshot moved to tools/rtos_tools.py)
        # (start_logging .. clear_logs moved to tools/logging_tools.py)
        # (capture_expressions .. compare_expressions_after_action moved to tools/memory_tools.py)
        # (set_watchpoint moved to tools/breakpoint_tools.py)
        # (load_svd .. decode_peripheral_register moved to tools/peripheral_tools.py)
        # (read_typed_memory .. write_typed_memory moved to tools/memory_tools.py)
        # (set_debug_profile .. validate_debug_config moved to tools/config_tools.py)
        # --- Step 7: Tracing ---
        # (track_variable moved to tools/memory_tools.py)
        # --- Phase 2: determinism (journal + replayable scenarios) ---
        # (get_session .. clear_session_journal moved to tools/meta_tools.py)
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
        # (get_timeouts .. report_issue moved to tools/meta_tools.py)
        # (export_debug_report moved to tools/fault_tools.py)
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
        # (run_to_line moved to tools/execution_tools.py)
        # (disassemble .. address_of moved to tools/inspect_tools.py)
        # (capture_coredump .. load_coredump moved to tools/fault_tools.py)
        # (verify_flash moved to tools/firmware_tools.py)
        # (read_cycle_counter moved to tools/memory_tools.py)
        # (setup_swo moved to tools/logging_tools.py)
        # (sample_pc moved to tools/memory_tools.py)
        # (import_netlist .. validate_board moved to tools/board_tools.py)
        # (load_acceptance .. acceptance_loop_status moved to tools/acceptance_tools.py)
        # (import_spec .. solve_timer moved to tools/design_tools.py)
        # (load_device_pack moved to tools/board_tools.py)
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


def _adapter_speed_khz(args: list[str]) -> int | None:
    for token in args:
        parts = token.split() if isinstance(token, str) else []
        if len(parts) == 3 and parts[:2] == ["adapter", "speed"] and parts[2].isdigit():
            return int(parts[2])
    return None


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
    debug_profile = _sess.debug_profile
    _last_session = _sess.last_session
    session_acceptance = _sess.acceptance
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

        # Registry-backed tools (domain modules under mcp_server/tools/).
        _spec_entry = _TOOL_REGISTRY.get(name)
        if _spec_entry is not None:
            return _spec_entry.handler(_make_context(_sess), arguments)

        # (tool_help moved to tools/meta_tools.py)
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

        # (build_firmware .. flash_firmware moved to tools/firmware_tools.py)
        # (reset_target .. capture_state moved to tools/execution_tools.py)
        # (flash_and_run moved to tools/firmware_tools.py)
        # (run_for_duration .. step moved to tools/execution_tools.py)
        elif name == "get_gdb_events":
            resp = gdb_client.get_responses()
            return [content_success({"events": resp, "message": "GDB events read" if resp else "No new events"})]

        elif name == "get_gdb_server_logs":
            logs = gdb_manager.get_logs()
            return [content_success({"logs": logs, "message": "GDB server logs captured" if logs else "No GDB server logs captured"})]

        # (get_session .. clear_session_journal moved to tools/meta_tools.py)
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

        # (get_timeouts .. report_issue moved to tools/meta_tools.py)
        # (run_to_line moved to tools/execution_tools.py)
        # (verify_flash moved to tools/firmware_tools.py)
        # (import_netlist .. validate_board moved to tools/board_tools.py)
        # (load_acceptance .. acceptance_loop_status moved to tools/acceptance_tools.py)
        # (import_spec .. solve_timer moved to tools/design_tools.py)
        # (load_device_pack moved to tools/board_tools.py)
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
