"""Tool registry: each domain module co-locates a Tool schema with its handler.

``@register(Tool(...))`` puts a ToolSpec in REGISTRY; server.py's dispatch shim
looks handlers up here, and handle_list_tools assembles the advertised base list
by walking TOOL_ORDER so extraction order never changes the advertised order.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mcp.types import TextContent, Tool

if TYPE_CHECKING:
    from .context import ToolContext

Handler = Callable[["ToolContext", dict], list[TextContent]]


@dataclass(frozen=True)
class ToolSpec:
    tool: Tool
    handler: Handler


REGISTRY: dict[str, ToolSpec] = {}


def register(tool: Tool) -> Callable[[Handler], Handler]:
    def decorate(fn: Handler) -> Handler:
        if tool.name in REGISTRY:
            raise ValueError(f"duplicate tool registration: {tool.name}")
        REGISTRY[tool.name] = ToolSpec(tool=tool, handler=fn)
        return fn

    return decorate


# The exact advertised base-tool order (pre tool_surface post-processing), captured
# from handle_list_tools before the decomposition. The golden surface snapshot test
# holds this constant; append new tools at the position they should advertise.
TOOL_ORDER: list[str] = [
    "start_debug_session", "stop_debug_session", "self_check", "detect_probe", "suggest_server_args",
    "set_adapter_speed", "batch", "call", "call_read", "tool_help", "recover_session", "check_session_health",
    "describe_hub", "set_hub_power", "set_hub_data", "power_cycle_target",
    "build_firmware", "load_symbols", "flash_firmware", "reset_target", "set_breakpoint",
    "list_breakpoints", "delete_breakpoint", "continue_execution", "halt_execution", "run_and_wait",
    "debug_until", "capture_state", "flash_and_run", "run_for_duration", "wait_for_stop", "step",
    "read_variable", "read_memory", "write_memory", "set_write_policy", "get_write_audit_log",
    "get_gdb_events", "get_gdb_server_logs", "read_call_stack", "read_core_registers", "select_frame",
    "read_frame_variables", "list_source", "resolve_address", "read_fault_registers", "diagnose_fault",
    "configure_debug_freeze", "analyze_stack", "reconstruct_fault_context", "capture_debug_snapshot",
    "inspect_project", "detect_rtos", "read_freertos", "capture_rtos_snapshot", "start_logging",
    "stop_logging", "get_logs", "clear_logs", "capture_expressions", "assert_expressions",
    "compare_expressions_after_action", "set_watchpoint", "load_svd", "read_peripheral_register",
    "decode_peripheral_register", "read_typed_memory", "write_typed_memory", "set_debug_profile",
    "get_debug_profile", "load_debug_config", "save_debug_config", "validate_debug_config",
    "track_variable", "get_session", "clear_session_journal", "list_sessions", "close_session",
    "get_timeouts", "set_timeouts", "report_issue", "export_debug_report", "run_scenario",
    "run_to_line", "disassemble", "list_functions", "list_variables", "lookup_type", "sizeof",
    "address_of", "capture_coredump", "load_coredump", "verify_flash", "flash_erase",
    "read_cycle_counter",
    "setup_swo", "sample_pc", "import_netlist", "describe_board", "validate_board", "load_acceptance",
    "run_acceptance", "describe_acceptance", "start_acceptance_loop", "run_acceptance_iteration",
    "acceptance_loop_status", "import_spec", "design_framework", "describe_framework",
    "render_framework", "synthesize_acceptance", "solve_clock_tree", "solve_timer",
    "load_device_pack", "run_pipeline",
]
