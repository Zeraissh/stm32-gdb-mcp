from __future__ import annotations

from copy import deepcopy

from mcp.types import Tool, ToolAnnotations

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
    "detect_probe", "suggest_server_args", "start_debug_session", "stop_debug_session", "recover_session",
    "self_check", "debug_profile", "load_symbols",
    "build_firmware", "flash_firmware", "flash_and_run",
    # halt_execution without a resume next to it taught agents to use
    # run_for_duration as a resume primitive, which is not what it is for (#33).
    "reset_target", "halt_execution", "continue_execution",
    "run_and_wait", "run_for_duration", "breakpoint",
    "debug_until", "capture_state",
    "read_memory", "write_memory", "read_variable", "read_call_stack",
    "reconstruct_fault_context", "analyze_stack",
    "logging", "read_peripheral_register",
    "batch", "call", "call_read", "run_scenario", "get_session", "report_issue",
    "list_sessions", "close_session", "tool_help",
}


# Action-dispatched families: {name: (discriminator, {choice: tool}, summary, arg_help)}.
#
# CONTRACT for arg_help, enforced by test_merged_family_arg_help_names_real_arguments:
# a "choice(...)" group lists that action's REAL argument names — required ones
# first, optional ones inside [brackets]. Prose belongs outside the parentheses.
# These strings are how the model learns to call a family, and when they named
# arguments that did not exist ("watch(expression)" when the schema wants
# location+access_type) every such call was rejected and had to be retried.
MERGED = {
    "logging": ("action",
        {"start": "start_logging", "stop": "stop_logging", "get": "get_logs", "clear": "clear_logs"},
        "Firmware log capture over a channel.",
        "action=start(channel[,port,baudrate,command,args,file,timeout]) | stop(channel) | "
        "get(channel[,limit,since_index,clear]) | clear(channel). channel=rtt|swo|uart; "
        "uart takes port/baudrate, rtt/swo take command/args or file."),
    "breakpoint": ("action",
        {"set": "set_breakpoint", "delete": "delete_breakpoint", "list": "list_breakpoints", "watch": "set_watchpoint"},
        "Breakpoint / watchpoint management.",
        "action=set(location[,condition,temporary,ignore_count]) | delete(breakpoint_id) | list | "
        "watch(location,access_type). access_type=r|w|a."),
    "expressions": ("action",
        {"assert": "assert_expressions", "capture": "capture_expressions", "compare": "compare_expressions_after_action"},
        "Evaluate C/GDB expressions.",
        "action=assert(assertions) | capture([expressions,table]) | compare(expressions,action). "
        "assertions=[{expression,expected[,operator]}]; table={index_range,columns}; "
        "compare runs 'action' between the two captures."),
    "coredump": ("action",
        {"capture": "capture_coredump", "load": "load_coredump"},
        "Core-dump capture / load.",
        "action=capture(path) | load(path)."),
    "timeouts": ("action",
        {"get": "get_timeouts", "set": "set_timeouts"},
        "GDB operation timeouts.",
        "action=get | set(overrides). overrides={connect,reset,memory,...} in seconds."),
    "debug_config": ("action",
        {"load": "load_debug_config", "save": "save_debug_config", "validate": "validate_debug_config"},
        "Debug-config file (.json) management.",
        "action=load(path) | save(path,config) | validate(config)."),
    "debug_profile": ("action",
        {"get": "get_debug_profile", "set": "set_debug_profile"},
        "Active debug profile (mcu/elf/svd/probe).",
        "action=get | set([mcu,elf_path,svd_path,board,probe,server_type,server_args,project_root,notes])."),
    "read_registers": ("what",
        {"core": "read_core_registers", "fault": "read_fault_registers", "cycle": "read_cycle_counter"},
        "Read CPU register groups.",
        "what=core([include_raw]) | fault | cycle([enable]). fault decodes CFSR/HFSR; "
        "cycle reads the DWT cycle counter."),
    "inspect_symbol": ("what",
        {"size": "sizeof", "type": "lookup_type", "address": "address_of",
         "resolve": "resolve_address", "functions": "list_functions", "variables": "list_variables"},
        "Symbol / type introspection.",
        "what=size(expr) | type(expr) | address(symbol) | resolve(expr) | functions([regex]) | "
        "variables([regex]). resolve maps an address back to source, returning "
        "{resolved,symbol,offset,section,file,line}; resolved=false means no symbol there."),
    "typed_memory": ("action",
        {"read": "read_typed_memory", "write": "write_typed_memory"},
        "Typed (struct-aware) memory access.",
        "action=read(address,width_bits,count) | write(address,value,width_bits). width_bits=8|16|32|64."),
    "write_guard": ("action",
        {"policy": "set_write_policy", "audit": "get_write_audit_log"},
        "Memory-write guardrail.",
        "action=policy([mode,add_allow,add_protected]) | audit([limit]). mode=enforce|dry_run."),
    "snapshot": ("scope",
        {"full": "capture_debug_snapshot", "rtos": "capture_rtos_snapshot"},
        "One-shot diagnostic snapshot.",
        "scope=full([include_project,include_rtos,include_logs,log_limit,project_root]) | rtos. "
        "full bundles registers+stack+faults; rtos captures task/queue state."),
    "frame": ("action",
        {"select": "select_frame", "source": "list_source", "variables": "read_frame_variables"},
        "Stack-frame navigation.",
        "action=select(level) | source([location,count]) | variables([level,include_raw]). "
        "level 0 is the innermost frame."),
    "session_diagnostics": ("what",
        {"health": "check_session_health", "events": "get_gdb_events", "server_logs": "get_gdb_server_logs"},
        "Session/transport diagnostics.",
        "what=health([reconnect]) | events | server_logs. events returns recent GDB/MI records; "
        "server_logs returns the GDB server's stderr."),
}

MERGED_AWAY = {old for _, mapping, *_ in MERGED.values() for old in mapping.values()}

SESSION_PROPERTY = {
    "type": "string",
    "description": "Optional debug-session name. Omit for the default target.",
}

READ_ONLY_TOOLS = {
    "tool_help", "inspect_project", "read_memory", "read_variable", "read_call_stack",
    "read_registers", "read_peripheral_register", "decode_peripheral_register",
    "inspect_symbol", "frame", "snapshot", "get_session",
    "list_sessions", "analyze_stack", "diagnose_fault", "detect_rtos", "read_freertos",
    "detect_probe", "describe_board", "validate_board", "describe_acceptance",
    "acceptance_loop_status", "describe_framework", "disassemble", "verify_flash",
    "sample_pc", "suggest_server_args", "capture_state", "wait_for_stop",
    "call_read",
}

# Read-only singles that no read-only family covers. Admission rule: the tool
# changes neither target nor host state, and carries no option that would (so
# get_logs, whose clear=true drops entries, and export_debug_report, which
# writes a file, stay out).
READ_ONLY_EXTRAS = {
    "get_debug_profile", "get_timeouts", "get_write_audit_log", "list_breakpoints",
    "get_gdb_events", "get_gdb_server_logs", "read_typed_memory",
}

HARDWARE_WRITE_TOOLS = {
    "flash_firmware", "flash_and_run", "flash_erase", "reset_target", "write_memory", "typed_memory",
    "set_adapter_speed", "setup_swo", "configure_debug_freeze",
}

GENERIC_DISPATCH_TOOLS = {"call", "batch", "run_scenario"}
OPEN_WORLD_TOOLS = {"report_issue", "build_firmware"}

# call_read forwards only to these, so it can be annotated read-only and skip the
# permission prompt that call's destructive+open-world annotation forces. Compact
# mode hides ~78 tools, roughly a third of them read-only; reaching those through
# call meant a prompt they would never have triggered as direct tools.
READ_ONLY_DISPATCH_EXCLUDED = {"call", "call_read", "batch", "run_scenario"}


def read_only_tool_names() -> set[str]:
    """Every tool call_read may forward to: the read-only set, the actions of a
    read-only family, and the standalone read-only tools listed above."""
    names = set(READ_ONLY_TOOLS) | set(READ_ONLY_EXTRAS)
    for family, (_discriminator, mapping, *_rest) in MERGED.items():
        if family in READ_ONLY_TOOLS:
            names.update(mapping.values())
    return names - READ_ONLY_DISPATCH_EXCLUDED


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
    if name in GENERIC_DISPATCH_TOOLS:
        return ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)
    if name in OPEN_WORLD_TOOLS:
        return ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=name == "build_firmware",
            idempotentHint=False,
            openWorldHint=True,
        )
    return ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)


def _decorate(tool: Tool) -> Tool:
    # No per-tool outputSchema: every tool shares the same envelope (see
    # tool_response.OUTPUT_SCHEMA), and advertising it on each tool cost ~460
    # chars x every tool for zero information gain.
    return tool.model_copy(
        deep=True,
        update={
            "inputSchema": _with_session(tool.inputSchema),
            "annotations": _annotations(tool.name),
        },
    )


def merged_tools(base: list[Tool]) -> list[Tool]:
    by_name = {tool.name: tool for tool in base}
    return [
        _merged_tool(name, discriminator, mapping, f"{summary} {arg_help}", by_name)
        for name, (discriminator, mapping, summary, arg_help) in MERGED.items()
    ]


def _merged_tool(name: str, discriminator: str, mapping: dict, description: str, by_name: dict) -> Tool:
    """Assemble one action-dispatched family from its per-action tools.

    Each action's arguments are hoisted into a single top-level ``properties``
    map, so a branch carries only its discriminator const and its own
    ``required`` list. Repeating every property (with descriptions, plus
    ``session``) inside all four branches made these the largest schemas on the
    surface — ``logging`` alone was 2.9k chars — while saying nothing the
    hoisted map and the per-branch ``required`` don't already say. Branch
    property names never collide today; ``_hoist`` keeps a colliding one local
    to its branch so a future clash degrades instead of silently merging.
    """
    branches = {
        choice: _with_session(_branch_schema(by_name.get(tool_name)))
        for choice, tool_name in mapping.items()
    }
    properties: dict = {
        discriminator: {
            "type": "string",
            "enum": list(mapping),
            "description": "Which operation to perform.",
        },
        "session": deepcopy(SESSION_PROPERTY),
    }
    local = {choice: _hoist(properties, schema["properties"]) for choice, schema in branches.items()}
    return Tool(
        name=name,
        description=description,
        inputSchema={
            "type": "object",
            "properties": properties,
            "required": [discriminator],
            "oneOf": [
                _merged_branch(discriminator, choice, schema, local[choice])
                for choice, schema in branches.items()
            ],
        },
    )


def _branch_schema(tool: Tool | None) -> dict:
    return tool.inputSchema if tool else {"type": "object", "properties": {}}


def _hoist(shared: dict, branch_properties: dict) -> dict:
    """Move a branch's properties into ``shared``; return the ones that must stay local.

    A property stays in its branch only when two actions genuinely mean different
    things by it — ``breakpoint.location`` is "where to break" for set and "what
    to watch" for watch, and collapsing those would lose real guidance. Differing
    only by one side omitting a description is not a real clash: the described
    copy wins and the bare duplicates are dropped.
    """
    kept_local = {}
    for prop, spec in branch_properties.items():
        existing = shared.get(prop)
        if existing is None:
            shared[prop] = deepcopy(spec)
        elif existing != spec:
            if _same_shape(existing, spec):
                if "description" not in existing and "description" in spec:
                    shared[prop] = deepcopy(spec)
            else:
                kept_local[prop] = deepcopy(spec)
    return kept_local


def _same_shape(left: dict, right: dict) -> bool:
    """True when two property specs differ only by a description one of them omits."""
    if {k: v for k, v in left.items() if k != "description"} != {
        k: v for k, v in right.items() if k != "description"
    }:
        return False
    return "description" not in left or "description" not in right


def _merged_branch(discriminator: str, choice: str, schema: dict, local_properties: dict) -> dict:
    return {
        "title": choice,
        "properties": {discriminator: {"const": choice}, **local_properties},
        "required": list(dict.fromkeys([discriminator, *schema.get("required", [])])),
    }


def advertised_tools(base: list[Tool]) -> list[Tool]:
    """Drop merged-away single tools and add the action-dispatched families."""
    visible = [tool for tool in base if tool.name not in MERGED_AWAY] + merged_tools(base)
    return [_decorate(tool) for tool in visible]
