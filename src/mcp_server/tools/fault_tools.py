"""Fault diagnosis and postmortem tools (fault registers, crash reconstruction,
debug freeze, snapshots, coredumps, and the exportable debug report)."""

from mcp.types import TextContent, Tool

from ..debug_freeze import plan_freeze_writes, resolve_freeze_targets, supported_families
from ..debug_report import build_report, write_report
from ..debug_snapshot import collect_debug_snapshot
from ..exception_frame import build_fault_context
from ..fault_analysis import diagnose_fault_registers
from ..project_inspector import inspect_project
from ..tool_response import content_error, content_success
from .context import ToolContext
from .registry import register


@register(Tool(
    name="read_fault_registers",
    description="Reads Cortex-M SCB fault status registers (CFSR, HFSR, DFSR, MMFAR, BFAR, AFSR).",
    inputSchema={"type": "object", "properties": {}}
))
def read_fault_registers(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    resp = ctx.gdb_client.read_fault_registers()
    hex_resp = {key: f"0x{value & 0xFFFFFFFF:08x}" for key, value in resp.items()}
    return [content_success(hex_resp, raw_response=resp)]


@register(Tool(
    name="diagnose_fault",
    description="Reads and decodes Cortex-M fault registers into likely fault causes.",
    inputSchema={"type": "object", "properties": {}}
))
def diagnose_fault(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    resp = ctx.gdb_client.read_fault_registers()
    diagnosis = diagnose_fault_registers(resp)
    return [content_success(diagnosis, raw_response=resp)]


@register(Tool(
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
))
def configure_debug_freeze(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    family = arguments.get("family") or ctx.debug_profile.get().get("mcu")
    if not family:
        return [content_error(
            "No STM32 family given and no MCU in the debug profile.",
            code="missing_family",
            suggested_next_actions=["set_debug_profile"],
        )]
    targets = resolve_freeze_targets(family, arguments["peripherals"])
    plans = plan_freeze_writes(targets, ctx.gdb_client.read_word)
    applied = arguments.get("apply", True)
    if applied:
        for plan in plans:
            ctx.gdb_client.write_memory(hex(plan["address"]), hex(plan["new_value"]))
    return [content_success({
        "message": "Debug freeze applied" if applied else "Debug freeze planned (not applied)",
        "family": family,
        "applied": applied,
        "plans": plans,
    })]


@register(Tool(
    name="reconstruct_fault_context",
    description="Reconstructs the full crash site after a HardFault: decodes fault "
                "registers, unwinds the auto-stacked exception frame from MSP/PSP via "
                "EXC_RETURN to recover the true faulting PC, and resolves it to source "
                "file:line. Run this when halted in a fault handler.",
    inputSchema={"type": "object", "properties": {}}
))
def reconstruct_fault_context(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    context = build_fault_context(ctx.gdb_client)
    return [content_success(
        context,
        suggested_next_actions=["list_source", "read_frame_variables", "read_call_stack"],
    )]


@register(Tool(
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
))
def capture_debug_snapshot(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    profile = ctx.debug_profile.get()
    project_context = None
    rtos_context = None
    log_context = None
    if arguments.get("include_project"):
        project_context = inspect_project(arguments.get("project_root") or profile.get("project_root"), profile)
    if arguments.get("include_rtos"):
        rtos_context = ctx.freertos_inspector.capture_snapshot()
    if arguments.get("include_logs"):
        log_context = {
            "rtt": {
                "status": ctx.rtt_log_reader.status(),
                "entries": ctx.rtt_log_reader.get_logs(limit=arguments.get("log_limit", 200)),
            },
            "uart": {
                "status": ctx.uart_log_reader.status(),
                "entries": ctx.uart_log_reader.get_logs(limit=arguments.get("log_limit", 200)),
            },
            "swo": {
                "status": ctx.swo_log_reader.status(),
                "entries": ctx.swo_log_reader.get_logs(limit=arguments.get("log_limit", 200)),
            },
        }
    snapshot = collect_debug_snapshot(
        ctx.gdb_client,
        ctx.gdb_manager,
        project_context=project_context,
        rtos_context=rtos_context,
        log_context=log_context,
    )
    return [content_success(snapshot)]


@register(Tool(
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
))
def capture_coredump(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    resp = ctx.gdb_client.capture_coredump(arguments["path"])
    return [content_success(
        {"message": "Core dump captured", "path": arguments["path"]},
        raw_response=resp,
        suggested_next_actions=["load_coredump"],
    )]


@register(Tool(
    name="load_coredump",
    description="Loads a previously captured core file for offline analysis.",
    inputSchema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the core file."}
        },
        "required": ["path"]
    }
))
def load_coredump(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    resp = ctx.gdb_client.load_coredump(arguments["path"])
    return [content_success({"message": "Core dump loaded", "path": arguments["path"]}, raw_response=resp)]


@register(Tool(
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
))
def export_debug_report(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    snapshot = None
    if arguments.get("include_snapshot"):
        snapshot = collect_debug_snapshot(ctx.gdb_client, ctx.gdb_manager)
    coredump_path = arguments.get("coredump_path")
    if coredump_path:
        ctx.gdb_client.capture_coredump(coredump_path)
    report = build_report(
        run_id=ctx.session_journal.run_id,
        journal_entries=ctx.session_journal.get(),
        profile=ctx.debug_profile.get(),
        snapshot=snapshot,
        coredump_path=coredump_path,
    )
    written = write_report(arguments["path"], report)
    return [content_success({
        "message": "Debug report exported",
        "path": written,
        "run_id": ctx.session_journal.run_id,
        "entries": len(report["journal"]),
        "included_snapshot": snapshot is not None,
        "coredump": coredump_path,
    })]
