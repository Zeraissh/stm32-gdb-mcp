"""Firmware tools: build, symbol load, flash, verify, and the flash_and_run composite."""

import os
import tempfile

from mcp.types import TextContent, Tool

from .. import build as build_mod
from ..gdb_decode import decode_console_text, decode_memory_bytes
from ..mi_guard import find_mi_error
from ..reset_strategy import resolve_reset_command
from ..tool_response import content_error, content_success
from .context import ToolContext
from .registry import register


@register(Tool(
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
))
def build_firmware(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    kind = arguments["kind"]
    log_path = None
    uv4_path = arguments.get("uv4_path")
    if kind == "keil":
        uv4_path = uv4_path or build_mod.find_uv4()
        log_path = os.path.join(tempfile.gettempdir(), f"uv4_build_{ctx.session_journal.run_id}.log")
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


@register(Tool(
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
))
def load_symbols(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    elf_path = arguments.get("elf_path") or ctx.debug_profile.get().get("elf_path")
    if not elf_path:
        return [content_error(
            "No elf_path given and none in the debug profile.",
            code="missing_elf",
            suggested_next_actions=["set_debug_profile"],
        )]
    resp = ctx.gdb_client.load_symbols(elf_path)
    return [content_success(
        {"message": "Symbols loaded", "elf_path": elf_path},
        raw_response=resp,
        suggested_next_actions=["set_breakpoint", "list_functions", "analyze_stack"],
    )]


@register(Tool(
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
))
def flash_firmware(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    resp = ctx.gdb_client.load_firmware(arguments["file_path"])
    data = {"message": "Firmware flashed", "file_path": arguments["file_path"], "reset_run": False}
    if arguments.get("reset_run", True):
        profile = ctx.debug_profile.get()
        reset_config = profile.get("reset", {})
        resolved = resolve_reset_command(
            ctx.gdb_manager.server_type or profile.get("server_type"),
            halt=False,
            strategy=reset_config.get("strategy"),
            command=reset_config.get("command"),
        )
        resp = (resp or []) + ctx.gdb_client.reset_run(command=resolved["command"])
        data["reset_run"] = True
        data["message"] = "Firmware flashed; target reset and running"
    return [content_success(data, raw_response=resp)]


@register(Tool(
    name="flash_erase",
    description="Erases a flash range on the target (OpenOCD 'flash erase_address pad'), "
                "rounding out to the driver's erase-sector boundaries. The recovery tool for "
                "a board that will not boot: clearing a corrupt OTA journal, a stale boot "
                "descriptor, or a half-written image. Requires an explicit address AND length "
                "— there is deliberately no mass-erase default.",
    inputSchema={
        "type": "object",
        "properties": {
            "address": {"type": "string", "description": "Start address, e.g. '0x08016000'. Rounded DOWN to the erase-sector boundary."},
            "length": {"type": "integer", "description": "Bytes to erase. Rounded UP to the erase-sector boundary."},
            "sector_size_bytes": {"type": "integer", "description": "Erase-sector size of this part (e.g. 4096 on STM32L1, up to 131072 on F4). Give it and the write guard checks the padded range that is really erased; omit it and the guard can only check the range you asked for."},
            "verify": {"type": "boolean", "description": "Read back the first 64 bytes and confirm they are 0xFF (default true)."}
        },
        "required": ["address", "length"]
    }
))
def flash_erase(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    address = arguments["address"]
    length = int(arguments["length"])
    if length < 1:
        return [content_error("flash_erase needs a positive 'length'.", code="invalid_argument")]

    start = int(str(address), 0)
    # OpenOCD pads out to its own sector boundaries, so the range it erases can
    # be strictly larger than the one asked for — on an F4 a 256-byte request can
    # take a 128 KiB sector with it. Guarding the unpadded range would check a
    # different operation than the one that runs, so when the caller knows the
    # sector size, guard what will really be erased.
    sector = int(arguments.get("sector_size_bytes") or 0)
    if sector > 0:
        guard_start = start - (start % sector)
        guard_end = start + length - 1
        guard_end += (sector - 1) - (guard_end % sector)
        guard_length = guard_end - guard_start + 1
    else:
        guard_start, guard_length = start, length

    decision = ctx.memory_guard.evaluate_range(guard_start, guard_length)
    ctx.memory_guard.audit("flash_erase", hex(guard_start), f"{guard_length} bytes", decision)
    if decision["action"] == "blocked":
        return [content_error(
            f"Erase of {guard_length} bytes at {hex(guard_start)} blocked: {decision['reason']}",
            code="memory_write_blocked",
            raw_response=decision,
            suggested_next_actions=["set_write_policy", "get_write_audit_log"],
        )]
    if decision["action"] == "simulated":
        return [content_success(
            {"message": "Flash erase simulated (dry_run)", "address": address,
             "length": length, "guard": decision},
        )]

    resp = ctx.gdb_client.flash_erase(start, length)
    data = {
        "message": "Flash erased",
        "address": address,
        "length": length,
        "guard": decision,
        "guard_scope": (
            {"address": hex(guard_start), "length": guard_length, "sector_size_bytes": sector}
            if sector > 0 else
            "requested range only — OpenOCD pads to its own erase sectors, which may be larger; "
            "pass sector_size_bytes for a guard check of what is actually erased"
        ),
        # OpenOCD reports the range it actually erased after padding; keep its
        # own words rather than reimplementing per-family sector arithmetic.
        "server_output": decode_console_text(resp) or None,
    }
    if arguments.get("verify", True):
        # An erase that "succeeded" without erasing is the same false ok this
        # server has been bitten by before, and it is cheap to check.
        checked = min(length, 64)
        readback = ctx.gdb_client.read_memory(hex(start), checked)
        contents = decode_memory_bytes(readback)
        # Erased STM32 flash reads back as 0xFF.
        erased = set(contents.lower()) <= {"f"} if contents else False
        data["verify"] = {"checked_bytes": checked, "bytes": contents, "erased": erased}
        if not contents:
            # A verification that could not run is not a verification that passed.
            # Falling through to ok:true here would be the same false ok the erase
            # check exists to prevent, with the evidence of its own failure buried
            # in the payload.
            gdb_error = find_mi_error(readback)
            return [content_error(
                f"Erase of {address} could not be verified: the read-back returned no bytes"
                + (f" ({gdb_error})" if gdb_error else "")
                + ". The erase command itself reported completion — re-read the range before "
                  "trusting it either way.",
                code="flash_erase_unverified",
                raw_response=data,
                suggested_next_actions=["read_memory", "self_check", "check_session_health"],
            )]
        if not erased:
            return [content_error(
                f"Flash at {address} still holds data after erase (first bytes: {contents[:32]}).",
                code="flash_erase_failed",
                raw_response=data,
                suggested_next_actions=["read_memory", "self_check", "reset_target"],
            )]
    return [content_success(data, raw_response=resp)]


@register(Tool(
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
))
def flash_and_run(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    profile = ctx.debug_profile.get()
    reset_config = profile.get("reset", {})
    reset = resolve_reset_command(
        ctx.gdb_manager.server_type or profile.get("server_type"),
        halt=True,
        strategy=reset_config.get("strategy"),
        command=reset_config.get("command"),
    )
    result = ctx.fns.flash_and_run(
        ctx.gdb_client,
        file_path=arguments["file_path"],
        run_to=arguments.get("run_to", "main"),
        timeout_sec=arguments.get("timeout_sec", 10.0),
        reset_command=reset["command"],
    )
    return [content_success(result, suggested_next_actions=["capture_state", "debug_until"])]


@register(Tool(
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
))
def verify_flash(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    resp = ctx.gdb_client.verify_flash(arguments["file_path"])
    return [content_success(
        {"message": "Flash verified", "file_path": arguments["file_path"]},
        raw_response=resp,
    )]
