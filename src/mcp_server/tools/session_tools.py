"""Session tools: server/client lifecycle, probe selection, health checks, and session admin."""

from mcp.types import TextContent, Tool

from ..debug_session import teardown_debug_session
from ..error_taxonomy import classify_error, refine_target_unreachable
from ..reliability import retry_call
from ..self_check import evaluate_self_check
from ..tool_response import content_error, content_success
from ._helpers import autoload_symbols, recover_current_session
from .context import ToolContext
from .registry import register


def _probe_selection(ctx: ToolContext, arguments: dict, profile: dict) -> tuple[str | None, str | None, dict | None, dict | None]:
    probe = arguments.get("probe")
    if probe:
        return probe, "argument", None, None
    probe = profile.get("probe")
    if probe:
        return probe, "profile", None, None

    detection = ctx.fns.detect_probe()
    probes = detection.get("probes") or []
    if len(probes) == 1:
        return probes[0].get("type"), "detected", probes[0], detection
    return None, None, None, detection


def _adapter_speed_khz(args: list[str]) -> int | None:
    for token in args:
        parts = token.split() if isinstance(token, str) else []
        if len(parts) == 3 and parts[:2] == ["adapter", "speed"] and parts[2].isdigit():
            return int(parts[2])
    return None


@register(Tool(
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
))
def start_debug_session(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    profile = ctx.debug_profile.get()
    server_type = arguments.get("server_type") or profile.get("server_type")
    if not server_type:
        # "the profile does not define it" reads as "you set the wrong field" when
        # the truth is usually "there is no profile": it is in-memory only, so a
        # restarted MCP server starts empty. Saying which one it is saves a
        # teardown-and-retry cycle (issue #39).
        detail = (
            "the active debug profile is EMPTY — it lives in memory only and is lost when the "
            "MCP server process restarts, so re-apply it"
            if not profile else
            f"the active debug profile does not define it (it holds: {sorted(profile)})"
        )
        return [content_error(
            f"server_type is required and {detail}.",
            code="missing_argument",
            suggested_next_actions=["load_debug_config", "set_debug_profile"],
        )]
    server_args_source: str | None
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
        probe, probe_source, detected_probe, detection = _probe_selection(ctx, arguments, profile)
        if mcu and probe:
            try:
                inferred = ctx.fns.suggest_server_args(
                    mcu,
                    probe,
                    scripts_dir=ctx.fns.find_openocd_scripts(),
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
        if ctx.sess.id != "default" and "gdb_port" not in _argstr:
            args += ["-c", f"gdb_port {ctx.sess.gdb_port}"]
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
            or getattr(ctx.sess, "serial", None)
        )
        if serial and "adapter serial" not in _argstr:
            args += ["-c", f"adapter serial {serial}"]
            ctx.sess.serial = serial
    # A previous session may not have fully released the probe/port yet, so the
    # restart can transiently fail with "open failed". retry_call backs off and
    # retries those, so stop -> start (and CI loops) work without a manual restart.
    if ctx.gdb_manager.is_alive():
        try:
            ctx.gdb_client.stop_gdb()
        except Exception:
            pass
        ctx.gdb_manager.stop()
    try:
        port = retry_call(lambda: ctx.gdb_manager.start(server_type, args), attempts=3, backoff_base=0.8)
        ctx.gdb_client.start_gdb()
        resp = ctx.gdb_client.connect("localhost", port)
    except Exception as exc:
        server_log = ctx.gdb_manager.get_logs() if hasattr(ctx.gdb_manager, "get_logs") else ""
        classification = classify_error(f"{exc}\n{server_log}")
        # The probe's measured VTREF is right there in the log — use it instead of
        # advising a power check the same payload disproves (issue #35).
        classification = refine_target_unreachable(classification, str(server_log))
        for teardown in (ctx.gdb_client.stop_gdb, ctx.gdb_manager.stop):
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
    ctx.last_session["server_type"] = server_type
    ctx.last_session["server_args"] = args
    symbols = autoload_symbols(ctx.sess)
    adopted = bool(getattr(ctx.gdb_manager, "adopted", False))
    data = {
        "message": "Debug session started",
        "server_type": server_type,
        "port": port,
        "symbols_loaded": symbols,
        "server_args_source": server_args_source,
        "detected_probe": detected_probe,
        "adopted_server": adopted,
    }
    if adopted:
        # Never let this pass silently: the server we attached to was configured by
        # someone else and may be pointed at a different board, so the requested
        # server_args had no effect. self_check is what proves which device is
        # actually on the other end.
        data["message"] = (
            f"Attached to a GDB server already listening on port {port}; it was not started "
            "by this session, so the requested server_args did not apply and it will not be "
            "stopped by stop_debug_session. Run self_check to confirm which device it serves."
        )
    return [content_success(
        data,
        raw_response=resp,
        suggested_next_actions=["self_check"] if adopted else None,
    )]


@register(Tool(
    name="stop_debug_session",
    description="Stops the GDB client/server, variable tracking, and all active RTT/SWO/UART readers.",
    inputSchema={"type": "object", "properties": {}}
))
def stop_debug_session(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    teardown_debug_session(ctx.sess)
    return [content_success({"message": "Debug session stopped"})]


@register(Tool(
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
))
def self_check(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    # Identity registers can't be read reliably while the core is running, so halt
    # first (a self_check is a deliberate diagnostic). Pass halt=false to skip.
    halted = False
    if arguments.get("halt", True):
        try:
            ctx.gdb_client.halt_execution()
            halted = True
        except Exception:
            pass
    cpuid = ctx.gdb_client.read_word(0xE000ED00)
    dbgmcu_idcode = ctx.gdb_client.read_word(0xE0042000)
    expected = arguments.get("expected_family") or ctx.debug_profile.get().get("mcu")
    result = evaluate_self_check(cpuid, dbgmcu_idcode, expected_family=expected)
    result["halted_for_check"] = halted
    next_actions = [] if result["ok"] else ["check_session_health", "start_debug_session"]
    return [content_success(result, suggested_next_actions=next_actions)]


@register(Tool(
    name="detect_probe",
    description="Lists physical ST-Link, J-Link, and CMSIS-DAP probes currently connected over USB. "
                "Preserves serial numbers so multiple identical probes are never silently collapsed.",
    inputSchema={"type": "object", "properties": {}}
))
def detect_probe(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    detected = ctx.fns.detect_probe()
    if detected.get("error"):
        return [content_error(
            f"Could not enumerate host USB debug probes: {detected['error']}",
            code="probe_detection_failed",
            raw_response=detected,
            suggested_next_actions=["set_debug_profile", "suggest_server_args"],
        )]
    actions = ["suggest_server_args", "start_debug_session"] if detected.get("count") == 1 else []
    return [content_success(detected, suggested_next_actions=actions)]


@register(Tool(
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
))
def suggest_server_args(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    scripts_dir = ctx.fns.find_openocd_scripts()
    profile = ctx.debug_profile.get()
    probe, probe_source, detected_probe, detection = _probe_selection(ctx, arguments, profile)
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
    result = ctx.fns.suggest_server_args(
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


@register(Tool(
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
))
def set_adapter_speed(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    resp = ctx.gdb_client.set_adapter_speed(arguments["khz"])
    return [content_success({"message": "Adapter speed set", "khz": arguments["khz"]}, raw_response=resp)]


@register(Tool(
    name="recover_session",
    description="Recovers a dropped or wedged session: cleanly tears down the GDB client and "
                "server, then restarts the server (with retry/backoff for a busy probe) using "
                "the last start_debug_session arguments and reconnects. Use after a "
                "probe_unavailable or connection_lost error.",
    inputSchema={"type": "object", "properties": {}}
))
def recover_session(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    if not ctx.last_session.get("server_type"):
        return [content_error(
            "No prior session to recover; call start_debug_session first.",
            code="no_session",
            suggested_next_actions=["start_debug_session"],
        )]
    recovered = recover_current_session(ctx.gdb_client, ctx.gdb_manager, ctx.last_session, ctx.sess)
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


@register(Tool(
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
))
def check_session_health(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    reconnected = False
    if arguments.get("reconnect") and ctx.gdb_manager.is_alive():
        ctx.gdb_client.start_gdb()
        ctx.gdb_client.connect("localhost", ctx.gdb_manager.port)
        reconnected = True
    health = {
        "gdb_alive": ctx.gdb_client.is_alive(),
        "server_alive": ctx.gdb_manager.is_alive(),
        "target_responsive": ctx.gdb_client.probe_target(),
        "server_type": ctx.gdb_manager.server_type,
        "port": ctx.gdb_manager.port,
        "reconnected": reconnected,
    }
    next_actions = [] if health["target_responsive"] else ["start_debug_session", "get_gdb_server_logs"]
    return [content_success(health, suggested_next_actions=next_actions)]


@register(Tool(
    name="get_gdb_events",
    description="Polls GDB for any asynchronous events (like hitting a breakpoint) or stdout messages.",
    inputSchema={"type": "object", "properties": {}}
))
def get_gdb_events(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    resp = ctx.gdb_client.get_responses()
    return [content_success({"events": resp, "message": "GDB events read" if resp else "No new events"})]


@register(Tool(
    name="get_gdb_server_logs",
    description="Returns recent logs captured from the active GDB server process.",
    inputSchema={"type": "object", "properties": {}}
))
def get_gdb_server_logs(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    logs = ctx.gdb_manager.get_logs()
    return [content_success({"logs": logs, "message": "GDB server logs captured" if logs else "No GDB server logs captured"})]


@register(Tool(
    name="list_sessions",
    description="Lists all debug target sessions (the 'default' one plus any named) with "
                "their liveness and port. To debug MULTIPLE boards at once, pass a 'session' "
                "argument (any string) to any tool — e.g. start_debug_session(session='rackA', "
                "...) — and each gets fully isolated state.",
    inputSchema={"type": "object", "properties": {}}
))
def list_sessions(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    default = ctx.default_session()
    default_row = {
        "session": "default",
        "server_alive": default.gdb_manager.is_alive(),
        "gdb_alive": default.gdb_client.is_alive(),
        "server_type": default.gdb_manager.server_type,
        "port": default.gdb_manager.port,
    }
    return [content_success({"sessions": [default_row] + ctx.session_manager.list()})]


@register(Tool(
    name="close_session",
    description="Closes a named debug session, tearing down its GDB client and server.",
    inputSchema={
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "Name of the session to close, e.g. 'rackA'."}
        },
        "required": ["session_id"]
    }
))
def close_session(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    sid = arguments["session_id"]
    if sid == "default":
        default = ctx.default_session()
        for stop in (default.gdb_client.stop_gdb, default.gdb_manager.stop):
            try:
                stop()
            except Exception:
                pass
        return [content_success({"message": "Default session stopped", "session_id": "default"})]
    closed = ctx.session_manager.close(sid)
    return [content_success({
        "message": "Session closed" if closed else "No such session",
        "closed": closed, "session_id": sid,
    })]
