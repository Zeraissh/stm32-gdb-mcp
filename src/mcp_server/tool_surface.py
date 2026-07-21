from __future__ import annotations

from copy import deepcopy

from mcp.types import Tool, ToolAnnotations

from .tool_response import OUTPUT_SCHEMA

RENAMED_TOOLS = {
    **{
        f"{action}_{channel}_log{suffix}": f'{action}_log{suffix}(channel="{channel}")'
        for channel in ("rtt", "swo", "uart")
        for action, suffix in (("start", "ging"), ("stop", "ging"), ("get", "s"), ("clear", "s"))
    },
    "step_over": 'step(kind="over")',
    "step_into": 'step(kind="into")',
    "step_out": 'step(kind="out")',
    "step_instruction": 'step(kind="instruction")',
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


CORE_TOOLS = {
    "suggest_server_args", "start_debug_session", "stop_debug_session", "recover_session",
    "self_check", "debug_profile", "load_symbols",
    "build_firmware", "flash_firmware", "flash_and_run",
    "reset_target", "halt_execution", "run_and_wait", "run_for_duration", "breakpoint",
    "debug_until", "capture_state",
    "read_memory", "write_memory", "read_variable", "read_call_stack",
    "reconstruct_fault_context", "analyze_stack",
    "logging", "read_peripheral_register",
    "batch", "call", "run_scenario", "get_session", "report_issue",
    "list_sessions", "close_session", "tool_help",
}


MERGED = {
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
        "action=assert(expressions) | capture(expressions or table={index_range,columns}) | compare(expressions, action_to_run_between)."),
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


MERGED_AWAY = {old for _, mapping, *_ in MERGED.values() for old in mapping.values()}

SESSION_PROPERTY = {
    "type": "string",
    "description": "Optional debug-session name. Omit for the default target.",
}

READ_ONLY_TOOLS = {
    "tool_help", "inspect_project", "read_memory", "read_variable", "read_call_stack",
    "read_registers", "read_peripheral_register", "decode_peripheral_register",
    "inspect_symbol", "frame", "snapshot", "session_diagnostics", "get_session",
    "list_sessions", "analyze_stack", "diagnose_fault", "detect_rtos", "read_freertos",
}

HARDWARE_WRITE_TOOLS = {
    "flash_firmware", "flash_and_run", "reset_target", "write_memory", "typed_memory",
    "set_adapter_speed", "setup_swo", "configure_debug_freeze",
}


def _with_session(schema: dict) -> dict:
    updated = deepcopy(schema)
    updated.setdefault("type", "object")
    updated.setdefault("properties", {}).setdefault("session", deepcopy(SESSION_PROPERTY))
    return updated


def _annotations(name: str) -> ToolAnnotations | None:
    if name in READ_ONLY_TOOLS:
        return ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
    if name in HARDWARE_WRITE_TOOLS:
        return ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)
    if name == "report_issue":
        return ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)
    return None


def _decorate(tool: Tool) -> Tool:
    return tool.model_copy(
        deep=True,
        update={
            "inputSchema": _with_session(tool.inputSchema),
            "outputSchema": deepcopy(OUTPUT_SCHEMA),
            "annotations": _annotations(tool.name),
        },
    )


def merged_tools(base: list[Tool]) -> list[Tool]:
    by_name = {tool.name: tool for tool in base}
    return [
        Tool(
            name=name,
            description=f"{summary} {arg_help}",
            inputSchema={
                "type": "object",
                "properties": {
                    discriminator: {
                        "type": "string",
                        "enum": list(mapping),
                        "description": "Which operation to perform.",
                    },
                    "session": deepcopy(SESSION_PROPERTY),
                },
                "required": [discriminator],
                "oneOf": [
                    _merged_branch(discriminator, choice, by_name.get(tool_name))
                    for choice, tool_name in mapping.items()
                ],
            },
        )
        for name, (discriminator, mapping, summary, arg_help) in MERGED.items()
    ]


def _merged_branch(discriminator: str, choice: str, tool: Tool | None) -> dict:
    schema = _with_session(tool.inputSchema if tool else {"type": "object", "properties": {}})
    properties = {
        discriminator: {"type": "string", "const": choice},
        **schema["properties"],
    }
    required = list(dict.fromkeys([discriminator, *schema.get("required", [])]))
    return {
        "title": choice,
        "type": "object",
        "properties": properties,
        "required": required,
    }


def advertised_tools(base: list[Tool]) -> list[Tool]:
    """Drop merged-away single tools and add the action-dispatched families."""
    visible = [tool for tool in base if tool.name not in MERGED_AWAY] + merged_tools(base)
    return [_decorate(tool) for tool in visible]
