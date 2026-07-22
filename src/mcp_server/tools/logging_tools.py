"""Background log capture tools (RTT / SWO / UART) plus one-call SWO/ITM setup."""

from mcp.types import TextContent, Tool

from .. import swo_config
from ..tool_response import content_error, content_success
from .context import ToolContext
from .registry import register


def _swo_reader(ctx: ToolContext):
    """The active SWO reader: the OpenOCD-file tailer if it's running, else the process one."""
    return ctx.swo_file_reader if ctx.swo_file_reader.is_running() else ctx.swo_log_reader


def _log_reader(ctx: ToolContext, channel: str):
    readers = {"rtt": ctx.rtt_log_reader, "swo": _swo_reader(ctx), "uart": ctx.uart_log_reader}
    if channel not in readers:
        raise ValueError(f"Unknown log channel '{channel}'. Use rtt, swo, or uart.")
    return readers[channel]


@register(Tool(
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
))
def start_logging(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    channel = arguments["channel"]
    config = {
        **ctx.debug_profile.get().get(channel, {}),
        **arguments,
    }
    if channel == "uart":
        if not config.get("port"):
            return [content_error(
                "uart logging requires port, either explicitly or in the active debug profile.",
                code="missing_argument",
            )]
        reader = ctx.uart_log_reader
        reader.start(
            port=config["port"],
            baudrate=config.get("baudrate", 115200),
            timeout=config.get("timeout", 0.1),
        )
    elif channel == "swo" and config.get("file"):
        # Out-of-the-box SWO printf: tail OpenOCD's internal ITM decode file,
        # no external decoder needed. Pair with setup_swo + '-output <file>'.
        reader = ctx.swo_file_reader
        reader.start(config["file"])
    else:  # rtt / swo: a process whose stdout is captured (e.g. JLinkRTTClient)
        reader = ctx.rtt_log_reader if channel == "rtt" else ctx.swo_log_reader
        default_cmd = "JLinkRTTClient" if channel == "rtt" else None
        command = [config.get("command", default_cmd)]
        if command[0] is None:
            return [content_error(
                "swo logging needs either file=<OpenOCD ITM output file> (use setup_swo "
                "first) or command=<external decoder>.", code="missing_command")]
        command.extend(config.get("args", []))
        reader.start(command)
    return [content_success({"channel": channel, **reader.status()})]


@register(Tool(
    name="stop_logging",
    description="Stops background log capture on a channel (rtt/swo/uart).",
    inputSchema={
        "type": "object",
        "properties": {"channel": {"type": "string", "enum": ["rtt", "swo", "uart"]}},
        "required": ["channel"]
    }
))
def stop_logging(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    reader = _log_reader(ctx, arguments["channel"])
    reader.stop()
    return [content_success({"channel": arguments["channel"], **reader.status()})]


@register(Tool(
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
))
def get_logs(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    reader = _log_reader(ctx, arguments["channel"])
    return [content_success({
        "channel": arguments["channel"],
        "status": reader.status(),
        "entries": reader.get_logs(
            limit=arguments.get("limit"),
            since_index=arguments.get("since_index"),
            clear=arguments.get("clear", False),
        ),
    })]


@register(Tool(
    name="clear_logs",
    description="Clears the buffered log lines for a channel (rtt/swo/uart) without stopping capture.",
    inputSchema={
        "type": "object",
        "properties": {"channel": {"type": "string", "enum": ["rtt", "swo", "uart"]}},
        "required": ["channel"]
    }
))
def clear_logs(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    reader = _log_reader(ctx, arguments["channel"])
    reader.clear()
    return [content_success({"message": f"{arguments['channel']} log buffer cleared", "channel": arguments["channel"]})]


@register(Tool(
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
))
def setup_swo(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    hclk_hz = int(arguments["hclk_hz"])
    swo_hz = int(arguments.get("swo_hz", 2_000_000))
    port = int(arguments.get("port", 0))
    output = arguments.get("output", "swo_itm.log")
    prescaler, achieved, exact = swo_config.swo_prescaler(hclk_hz, swo_hz)

    # Configure TPIU+ITM straight from the debugger (version-independent, no fw init).
    writes = swo_config.itm_tpiu_setup_writes(hclk_hz, swo_hz, port)
    for address, value in writes:
        ctx.gdb_client.write_typed_memory(hex(address), hex(value), width_bits=32)

    openocd_cmds = swo_config.openocd_swo_capture_commands(
        hclk_hz, swo_hz, output, port,
        tpiu_name=arguments.get("tpiu_name", "$_CHIPNAME.tpiu"))
    applied = []
    if arguments.get("apply_openocd"):
        for cmd in openocd_cmds:
            try:
                ctx.gdb_client.execute_cli_command(f"monitor {cmd}", timeout_sec=3.0)
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
