"""Session tools: server/client lifecycle, probe selection, health checks, and session admin."""

from mcp.types import TextContent, Tool

from ..debug_session import teardown_debug_session
from ..error_taxonomy import classify_error, refine_target_unreachable
from ..reliability import retry_call
from ..self_check import evaluate_self_check
from ..server_metadata import server_info
from ..tool_response import content_error, content_success
from ._helpers import autoload_symbols, core_state, hub_binding, recover_current_session
from .context import ToolContext
from .registry import register


def _usb_location_for(ctx: ToolContext, arguments: dict, profile: dict) -> str | None:
    """The ``adapter usb location`` value for this session's board, or None.

    Three sources, most explicit first, because a wrong value here attaches to the
    wrong board:

    1. ``usb_location`` passed to this call.
    2. ``hub.map[<channel>].usb_location`` in the profile, selected by hub_channel,
       by the profile's hub.channel, or by the label that matches this session id --
       the same precedence hub channel selection already uses.
    3. Nothing. There is deliberately NO guess: the last port number happens to be
       the hub channel on this bench, but the prefix ("1-1.") is the hub's own
       position on the bus and cannot be derived from a channel number. Inventing
       one would either fail to open or, worse, open a different probe.
    """
    explicit = arguments.get("usb_location")
    if explicit:
        return str(explicit)

    hub_spec = profile.get("hub") or {}
    entries = hub_spec.get("map") or {}
    channel = arguments.get("hub_channel") or hub_spec.get("channel")
    if channel is None:
        for key, entry in entries.items():
            if isinstance(entry, dict) and entry.get("label") == ctx.session_id:
                channel = key
                break
    if channel is None:
        return None
    for key, entry in entries.items():
        try:
            same = int(key) == int(channel)
        except (TypeError, ValueError):
            same = str(key) == str(channel)
        if same and isinstance(entry, dict) and entry.get("usb_location"):
            return str(entry["usb_location"])
    return None


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
            "speed_khz": {"type": "integer", "description": "OpenOCD adapter clock used when server_args are inferred (default 4000)."},
            "usb_location": {"type": "string", "description":
                "Select the probe by physical USB position, e.g. '1-1.4'. This is how to "
                "pick one of several probes that report NO serial number (most clone "
                "ST-Links), and unlike hub isolation it disconnects nobody -- two sessions "
                "can debug two boards at once. Added as 'adapter usb location <value>'. "
                "Usually you want hub_channel instead, which looks this up."},
            "hub_channel": {"type": "integer", "description":
                "Take the probe on this hub channel, using the usb_location recorded for it "
                "in the profile's hub.map. Falls back to hub.channel, then to the map entry "
                "whose label matches this session's name."}
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
                # Naming only "select one" sent agents off to read the skill docs to
                # work out HOW -- and on a hub bench the practical answer is to
                # isolate, which this message never mentioned. Spell out both routes
                # so the next step is a tool call, not a documentation round trip.
                count = detection.get("count", 0)
                serials = [p.get("serial") for p in detection.get("probes", []) if p.get("serial")]
                by_serial = (f' Serials seen: {serials}; set one with '
                             f'debug_profile(action=set, serial="...").' if serials else
                             " None of them reports a serial number, so they can only be told apart "
                             "by USB port -- isolation is the practical route here.")
                return [content_error(
                    f"{count} debug probes are connected and none was chosen automatically."
                    f"{by_serial} Best route: pin the one you want by USB position, which needs "
                    "no serial and disconnects nobody -- pass server_args containing "
                    '-c "adapter usb location <bus>-<port>", e.g. 1-1.4 for hub channel 4 '
                    "(the last port number is the hub channel). Two sessions can then debug two "
                    "boards at once. Failing that, isolate: "
                    'hub(action=data, state="on", channel=N, exclusive=true, confirm=true) leaves '
                    "one probe enumerated -- but it takes every OTHER board off the USB bus, and "
                    'putting them back needs hub(action=data, state="on", channel=M, confirm=true) '
                    "(confirm is required: data_on is a mutating action under the default guard).",
                    code="multiple_probes",
                    raw_response=detection,
                    suggested_next_actions=[
                        'start_debug_session(server_args=[..., "-c", "adapter usb location 1-1.N"])',
                        'hub(action=data, state="on", channel=N, exclusive=true, confirm=true)',
                        "debug_profile(action=set, serial=...)",
                    ],
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
        # The serial-less case, which is most clone ST-Links: OpenOCD can select a
        # probe by physical USB position instead, and that needs nobody
        # disconnected. Verified on this bench -- two instances pinned to different
        # locations coexist, and the last port number is the hub channel
        # (CH1->1-1.1, CH3->1-1.3, CH4->1-1.4, correlated by cutting each channel's
        # data line and seeing which location vanished).
        #
        # Isolation (hub exclusive=true) was only ever a workaround for this server
        # not telling OpenOCD which probe to take: it drops every OTHER board off
        # the USB bus, bypasses the hub guard and the audit trail, and has to be
        # undone afterwards.
        location = _usb_location_for(ctx, arguments, profile)
        if location and "adapter usb location" not in _argstr:
            args += ["-c", f"adapter usb location {location}"]
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
    # self_check is the mandated first call after connecting, so this is the one
    # place the running build's identity reaches the transcript unprompted --
    # before anyone thinks to ask which server they are actually talking to.
    result["server"] = server_info()
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
                "probe_unavailable or connection_lost error. When a programmable USB hub is "
                "configured (debug_profile hub.channel), a retry that cannot fix the probe "
                "escalates to toggling its USB data lines and then power-cycling the port -- "
                "the only in-band cure for a wedged endpoint. Any hub action taken is reported "
                "in recovery_steps.",
    inputSchema={
        "type": "object",
        "properties": {
            "escalate": {
                "type": "boolean",
                "description": "Allow hub data-line/power escalation when a plain retry fails (default true).",
            },
            "deep": {
                "type": "boolean",
                "description": "Add a long (2 s) power-off rung to drain bulk capacitance.",
            },
        },
    }
))
def recover_session(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    if not ctx.last_session.get("server_type"):
        return [content_error(
            "No prior session to recover; call start_debug_session first.",
            code="no_session",
            suggested_next_actions=["start_debug_session"],
        )]
    binding = hub_binding(ctx) if arguments.get("escalate", True) else None
    hub, channel = binding if binding else (None, None)
    recovered = recover_current_session(
        ctx.gdb_client, ctx.gdb_manager, ctx.last_session, ctx.sess,
        hub=hub, channel=channel,
        escalate=arguments.get("escalate", True),
        deep=bool(arguments.get("deep")),
    )
    data = {
        "message": recovered["message"],
        "server_type": recovered["server_type"],
        "port": recovered["port"],
        "symbols_loaded": recovered["symbols_loaded"],
    }
    if "recovery_steps" in recovered:
        data["recovery_steps"] = recovered["recovery_steps"]
    return [content_success(
        data,
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
    state = core_state(ctx.gdb_client)
    responsive, identity = _target_evidence(ctx.gdb_client, state)
    health = {
        "gdb_alive": ctx.gdb_client.is_alive(),
        "server_alive": ctx.gdb_manager.is_alive(),
        "target_responsive": responsive,
        "server_type": ctx.gdb_manager.server_type,
        "port": ctx.gdb_manager.port,
        "reconnected": reconnected,
    }
    if identity:
        health["identity"] = identity
    hub_state = _hub_health(ctx)
    if hub_state:
        health["hub"] = hub_state

    if responsive:
        next_actions = []
    elif state == "running":
        # A running core is not a broken link. Sending an agent to recover_session
        # here would tear down a perfectly good session.
        next_actions = ["halt_execution", "self_check"]
    else:
        # recover_session first: with a live server, a re-enumerated probe is the
        # usual cause and restarting the whole session is the heavier remedy.
        next_actions = ["self_check", "recover_session", "start_debug_session",
                        "get_gdb_server_logs"]
    return [content_success(health, suggested_next_actions=next_actions, core_state=state)]


def _target_evidence(gdb_client, state: str | None) -> tuple[bool, dict | None]:
    """Is the target really answering? Judge by evidence, not by "no exception".

    ``probe_target`` only asked whether GDB answered at all, which it does from
    its own state even when the link underneath is dead. Measured on hardware
    after a hub power cycle dropped the probe: it reported responsive=true while
    every memory read returned 0x00000000 and self_check reported
    cpuid=0x00000000. A health check that says "healthy" about a dead link is
    worse than one that says nothing.

    So read CPUID and apply the same signature test self_check uses: a real
    Cortex-M has implementer 0x41 and the architecture constant 0xF. All-zero,
    all-ones, or byte-swapped reads all fail it.

    A RUNNING core also reads back zeros here (measured), which is not evidence
    of a dead link -- but it is not evidence of a live one either. That case is
    reported as unverified with the fix being ``halt_execution``, not
    ``recover_session``: two very different failures should not look the same.
    """
    try:
        cpuid = gdb_client.read_word(0xE000ED00)
    except Exception as exc:  # noqa: BLE001 - an unanswered read IS the answer
        return False, {"cpuid": None, "verified": False,
                       "reason": f"CPUID read failed: {exc}"[:200]}

    implementer = (cpuid >> 24) & 0xFF
    constant = (cpuid >> 16) & 0xF
    ok = implementer == 0x41 and constant == 0xF
    evidence: dict = {"cpuid": f"0x{cpuid:08x}", "verified": ok}
    if ok:
        return True, evidence

    if state == "running":
        evidence["reason"] = (
            f"Identity could not be verified because the core is RUNNING: reads returned "
            f"0x{cpuid:08x}. This is expected while running and is NOT evidence of a dead link. "
            f"Halt the core to check the link for real."
        )
    else:
        evidence["reason"] = (
            f"CPUID 0x{cpuid:08x} is not a Cortex-M signature (implementer 0x{implementer:02x}, "
            f"expected 0x41). The link answered but the answer is not the target -- the probe "
            f"has most likely re-enumerated, and every read is returning meaningless data."
        )
    return False, evidence


def _hub_health(ctx: ToolContext) -> dict | None:
    """One ~13 ms hub snapshot for this session's port. Costs no target traffic.

    An unresponsive target and a port reading 0 mV are the same story told twice;
    seeing both at once is what turns "the link died" into "the board lost power".
    """
    binding = hub_binding(ctx)
    if binding is None:
        return None
    manager, channel = binding
    try:
        described = manager.describe()
    except Exception:  # noqa: BLE001 - health must never fail because of the hub
        return None
    for port in described.get("ports", []):
        if port.get("channel") == channel:
            return dict(port)
    return {"channel": channel}


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
