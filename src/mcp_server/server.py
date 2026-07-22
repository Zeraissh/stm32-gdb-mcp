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

# Composites look statically unused once their handlers move to domain modules, but they
# must stay module globals: _make_context reads them via globals() on every dispatch so
# tests that monkeypatch mcp_server.server.<name> keep working (ctx.fns.<name>).
from .composites import capture_state, debug_until, flash_and_run, run_for_duration  # noqa: F401
from .debug_profile import DebugProfileStore
from .debug_session import SessionManager
from .error_taxonomy import classify_error
from .freertos_inspector import FreeRTOSInspector
from .gdb_client import GdbClientManager
from .gdb_manager import GdbServerManager

# file_issue stays a module global for the same reason as the composites above.
from .issue_reporter import file_issue  # noqa: F401
from .log_reader import FileLogReader, ProcessLogReader, SerialLogReader
from .memory_guard import MemoryWriteGuard

# detect_probe/suggest_server_args/find_openocd_scripts stay module globals for the same
# reason as the composites above: _make_context reads them via globals() on every dispatch
# so tests that monkeypatch mcp_server.server.<name> keep working (ctx.fns.<name>).
from .openocd_config import detect_probe, find_openocd_scripts, suggest_server_args  # noqa: F401
from .scenario import load_scenario, replay_scenario, step_summary
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


# (_probe_selection moved to tools/session_tools.py)

@server.list_tools()
async def handle_list_tools() -> list[Tool]:
    global _tool_catalog
    _tools = [
        # --- Step 4: Basic Control and Flashing ---
        # (start_debug_session .. set_adapter_speed moved to tools/session_tools.py)
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
        # (recover_session .. check_session_health moved to tools/session_tools.py)
        # (build_firmware .. flash_firmware moved to tools/firmware_tools.py)
        # (reset_target moved to tools/execution_tools.py)
        # --- Step 5: Core Debug Interaction ---
        # (set_breakpoint .. delete_breakpoint moved to tools/breakpoint_tools.py)
        # (continue_execution .. capture_state moved to tools/execution_tools.py)
        # (flash_and_run moved to tools/firmware_tools.py)
        # (run_for_duration .. step moved to tools/execution_tools.py)
        # (read_variable .. get_write_audit_log moved to tools/memory_tools.py)
        # (get_gdb_events .. get_gdb_server_logs moved to tools/session_tools.py)
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
        # (list_sessions .. close_session moved to tools/session_tools.py)
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
        # (run_pipeline moved to tools/pipeline_tools.py)
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


# (_adapter_speed_khz moved to tools/session_tools.py)
# (_pipeline_stage_summary / _pipeline_next moved to tools/pipeline_tools.py)


def _dispatch_tool(name: str, arguments: dict | None) -> list[TextContent]:
    if arguments is None:
        arguments = {}

    # Resolve the per-target session; registered handlers get its objects via ToolContext.
    # The "default" session reads the module globals (back-compat).
    _sess = _resolve_session(arguments)
    if "session" in arguments:
        arguments = {k: v for k, v in arguments.items() if k != "session"}  # handlers don't see the selector

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
        # (start_debug_session .. check_session_health moved to tools/session_tools.py)
        # (build_firmware .. flash_firmware moved to tools/firmware_tools.py)
        # (reset_target .. capture_state moved to tools/execution_tools.py)
        # (flash_and_run moved to tools/firmware_tools.py)
        # (run_for_duration .. step moved to tools/execution_tools.py)
        # (get_gdb_events .. get_gdb_server_logs moved to tools/session_tools.py)
        # (get_session .. clear_session_journal moved to tools/meta_tools.py)
        # (list_sessions .. close_session moved to tools/session_tools.py)
        # (get_timeouts .. report_issue moved to tools/meta_tools.py)
        # (run_to_line moved to tools/execution_tools.py)
        # (verify_flash moved to tools/firmware_tools.py)
        # (import_netlist .. validate_board moved to tools/board_tools.py)
        # (load_acceptance .. acceptance_loop_status moved to tools/acceptance_tools.py)
        # (import_spec .. solve_timer moved to tools/design_tools.py)
        # (load_device_pack moved to tools/board_tools.py)
        # (run_pipeline moved to tools/pipeline_tools.py)
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
