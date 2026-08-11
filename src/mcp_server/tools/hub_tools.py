"""Programmable USB hub tools: per-port power and USB2 data-line control.

These are the only tools in this server that can act on the target's power rail,
which makes them the answer to the failure this repo has documented five times
over: a wedged ST-Link USB endpoint that no software teardown can clear.

Every handler degrades cleanly when the ``[hub]`` extra is absent or no hub is
attached -- ``hub_unavailable`` with an install hint, never a traceback.
"""

import time

from mcp.types import TextContent, Tool

from ..hub import HubBusyError, HubGuardError, HubUnavailableError
from ..hub_topology import discover_by_toggle
from ..tool_response import content_error, content_success
from ._helpers import core_state
from .context import ToolContext
from .registry import register

_STATE = {"type": "string", "enum": ["on", "off"], "description": "Target state for the port."}
_CHANNEL = {
    "type": "integer",
    "description": "1-based hub channel. Defaults to the session's mapped hub channel.",
}
_CONFIRM = {
    "type": "boolean",
    "description": "Required to cut power/data when the guard is in confirm mode or a session is live.",
}


def _manager(ctx: ToolContext):
    """The process-wide hub manager, configured from this session's profile.

    Read through ctx.fns so tests can monkeypatch mcp_server.server.hub_manager,
    matching how every other hardware dependency is injected here.
    """
    manager = ctx.fns.hub_manager
    profile = ctx.debug_profile.get()
    spec = profile.get("hub")
    if spec:
        manager.configure(spec)
    return manager


def _resolve(ctx: ToolContext, arguments: dict):
    manager = _manager(ctx)
    channel, source = manager.channel_for(
        explicit=arguments.get("channel"),
        profile=ctx.debug_profile.get(),
        session_id=ctx.session_id,
        probe_serial=getattr(ctx.sess, "serial", None) or ctx.debug_profile.get().get("serial"),
    )
    return manager, channel, source


def _live_session(ctx: ToolContext, channel: int) -> str | None:
    """This session's id when its own GDB server is live on ``channel``.

    Cutting power out from under a running server is how a flash becomes a brick,
    so a hit here forces confirmation regardless of guard mode. Only the calling
    session is checked; sweeping every named session (and taking that session's
    dispatch lock) arrives with the rack work.
    """
    try:
        mapped, _source = _manager(ctx).channel_for(
            profile=ctx.debug_profile.get(),
            session_id=ctx.session_id,
            probe_serial=getattr(ctx.sess, "serial", None) or ctx.debug_profile.get().get("serial"),
        )
    except Exception:  # noqa: BLE001 - unmapped session cannot be the one at risk
        return None
    if mapped != channel:
        return None
    try:
        if ctx.gdb_manager is not None and ctx.gdb_manager.is_alive():
            return ctx.session_id
    except Exception:  # noqa: BLE001 - liveness is advisory
        return None
    return None


def _error(exc: Exception) -> list[TextContent]:
    if isinstance(exc, HubGuardError):
        return [content_error(str(exc), code="hub_guard_blocked",
                              suggested_next_actions=["hub(action=describe)"])]
    if isinstance(exc, HubBusyError):
        return [content_error(str(exc), code="hub_busy",
                              suggested_next_actions=["hub(action=describe)"])]
    return [content_error(str(exc), code="hub_unavailable",
                          suggested_next_actions=["hub(action=describe)",
                                                  "debug_profile(action=set, hub=...)"])]


@register(Tool(
    name="describe_hub",
    description=(
        "Reports the attached programmable USB hub: identity, channel list, per-port power and "
        "USB2 data-line state, and per-port voltage/current on models with an ADC. Read-only. "
        "Returns hub_unavailable when the [hub] extra is not installed or no hub is attached."
    ),
    inputSchema={"type": "object", "properties": {}}
))
def describe_hub(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    try:
        return [content_success(_manager(ctx).describe())]
    except (HubUnavailableError, HubBusyError, HubGuardError) as exc:
        return _error(exc)


@register(Tool(
    name="set_hub_power",
    description=(
        "Switches a hub port's power on or off. Powering off removes VBUS from whatever is on that "
        "port, so it cold-boots the board and un-enumerates its probe. Requires confirm=true while "
        "the guard is in confirm mode (the default) or a session has a live GDB server on the port."
    ),
    inputSchema={
        "type": "object",
        "properties": {"state": _STATE, "channel": _CHANNEL, "confirm": _CONFIRM},
        "required": ["state"],
    }
))
def set_hub_power(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    try:
        manager, channel, source = _resolve(ctx, arguments)
        result = manager.power(channel, arguments["state"],
                               confirm=bool(arguments.get("confirm")),
                               live_session=_live_session(ctx, channel))
        result["channel_source"] = source
        return [content_success(result)]
    except (HubUnavailableError, HubBusyError, HubGuardError) as exc:
        return _error(exc)
    except ValueError as exc:
        return [content_error(str(exc), code="invalid_argument")]


@register(Tool(
    name="set_hub_data",
    description=(
        "Connects or disconnects a hub port's USB2 data lines, leaving power untouched. This "
        "un-enumerates a probe without rebooting the board it is attached to. With exclusive=true "
        "every other port's data line is disconnected, so exactly one probe stays visible -- which "
        "keeps start_debug_session's single-probe auto-select valid on a multi-board rack."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "state": _STATE,
            "channel": _CHANNEL,
            "exclusive": {
                "type": "boolean",
                "description": "With state=on, disconnect every other port's data line.",
            },
            "confirm": _CONFIRM,
        },
        "required": ["state"],
    }
))
def set_hub_data(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    try:
        manager, channel, source = _resolve(ctx, arguments)
        result = manager.data(channel, arguments["state"],
                              confirm=bool(arguments.get("confirm")),
                              live_session=_live_session(ctx, channel),
                              exclusive=bool(arguments.get("exclusive")))
        result["channel_source"] = source
        return [content_success(result)]
    except (HubUnavailableError, HubBusyError, HubGuardError) as exc:
        return _error(exc)
    except ValueError as exc:
        return [content_error(str(exc), code="invalid_argument")]


@register(Tool(
    name="discover_hub_map",
    description=(
        "Works out which hub channel each debug probe is plugged into, by disconnecting one "
        "channel's USB data lines at a time and seeing which probe disappears from detect_probe. "
        "Data lines, not power, so a neighbouring board mid-experiment is not rebooted. SLOW and "
        "opt-in: it runs detect_probe once per channel plus a baseline, and on Windows each of "
        "those walks the whole USB device tree (~8.5 s). Refuses while any GDB server is alive "
        "unless force=true. With apply=true the result is written into the active debug profile."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "apply": {"type": "boolean", "description": "Write the discovered map into the debug profile."},
            "force": {"type": "boolean", "description": "Run even though a GDB server is alive on this session."},
            "settle_sec": {"type": "number", "description": "Seconds to wait for USB re-enumeration per toggle (default 1.0)."},
        },
    }
))
def discover_hub_map(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    try:
        manager = _manager(ctx)
        if not arguments.get("force") and _any_session_live(ctx):
            return [content_error(
                "a GDB server is alive on this session; discovery un-enumerates probes one at a "
                "time and would drop it. Stop the session, or pass force=true.",
                code="hub_busy_sessions",
                suggested_next_actions=["stop_debug_session", "hub(action=discover, force=true)"],
            )]

        channels = manager.channels()
        result = discover_by_toggle(
            _GuardBypass(manager), ctx.fns.detect_probe, channels,
            sleep=time.sleep, settle_sec=float(arguments.get("settle_sec", 1.0)),
        )

        # Probes with no serial number are common (ST-Link/V2 clones all share
        # VID:PID 0483:3748 and report none), and for those `key` -- derived from
        # the Windows instance id, i.e. the physical port -- is the ONLY identity
        # there is. Persisting only serial/label would write an empty entry and
        # silently throw it away.
        unserialised = sorted(ch for ch, entry in result["map"].items() if not entry.get("serial"))
        if unserialised:
            result["note"] = (
                f"channels {unserialised} hold probes with no serial number, so their map entries "
                f"are keyed by USB port instead. A port key identifies the channel but cannot "
                f"select it: give those channels a `label` (and select with it), or set "
                f"hub.channel per board."
            )

        if arguments.get("apply") and result["map"]:
            spec = dict(ctx.debug_profile.get().get("hub") or {})
            merged = {str(k): dict(v) for k, v in (spec.get("map") or {}).items()}
            for channel, entry in result["map"].items():
                existing = merged.get(str(channel), {})
                persisted = {k: v for k, v in entry.items() if k in ("serial", "key")}
                # Never drop a label a human chose; discovery only fills in identity.
                if existing.get("label"):
                    persisted["label"] = existing["label"]
                merged[str(channel)] = persisted
            spec["map"] = merged
            ctx.debug_profile.update({"hub": spec})
            manager.configure(spec)
            result["applied"] = True

        # JSON has no integer keys, so stringify explicitly rather than leave it to
        # the serializer -- callers should see the same shape they can write back.
        result["map"] = {str(channel): entry for channel, entry in result["map"].items()}

        next_actions = ["debug_profile(action=get)"] if result.get("applied") else \
            ["hub(action=discover, apply=true)"]
        if result["ambiguous"]:
            next_actions.insert(0, "hub(action=describe)")
        return [content_success(result, suggested_next_actions=next_actions)]
    except (HubUnavailableError, HubBusyError, HubGuardError) as exc:
        return _error(exc)
    except (TypeError, ValueError) as exc:
        return [content_error(str(exc), code="invalid_argument")]


def _any_session_live(ctx: ToolContext) -> bool:
    try:
        return bool(ctx.gdb_manager is not None and ctx.gdb_manager.is_alive())
    except Exception:  # noqa: BLE001 - liveness is advisory
        return False


class _GuardBypass:
    """Data-line toggles issued by discovery, which is itself the explicit request.

    Discovery only ever restores what it disconnected, and it already refused to
    run while a session is live, so re-asking for confirmation per channel would
    make the tool unusable without adding safety.
    """

    def __init__(self, manager):
        self._manager = manager

    def data(self, channel, state, confirm=False):
        return self._manager.data(channel, state, confirm=True)


@register(Tool(
    name="measure_hub_channel",
    description=(
        "Samples a hub port's voltage and current over a window and returns the series plus "
        "min/max/mean per channel. Read-only, and costs no target traffic -- so it works while "
        "the core is running, which is the point: it is the only instrument here that sees "
        "actual power draw, and an MCU that entered Stop and one that only thinks it did look "
        "identical over SWD. Requires a model with an ADC."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "channel": _CHANNEL,
            "duration_sec": {"type": "number", "description": "Sampling window; 0 (default) takes one snapshot."},
            "interval_sec": {"type": "number", "description": "Delay between samples (default 0.05)."},
            "all_channels": {"type": "boolean", "description": "Sample every channel instead of just one."},
        },
    }
))
def measure_hub_channel(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    try:
        manager = _manager(ctx)
        channels = None
        source = "all_channels"
        if not arguments.get("all_channels"):
            _manager_unused, channel, source = _resolve(ctx, arguments)
            channels = (channel,)
        result = manager.measure(
            channels,
            duration_sec=float(arguments.get("duration_sec", 0.0)),
            interval_sec=float(arguments.get("interval_sec", 0.05)),
        )
        result["channel_source"] = source
        return [content_success(result, core_state=core_state(ctx.gdb_client))]
    except (HubUnavailableError, HubBusyError, HubGuardError) as exc:
        return _error(exc)
    except (TypeError, ValueError) as exc:
        return [content_error(str(exc), code="invalid_argument")]


@register(Tool(
    name="power_cycle_target",
    description=(
        "Removes power from a hub port and restores it: a true cold boot of whatever is on that "
        "port, and the only in-band way to clear a wedged probe USB endpoint. On models with an "
        "ADC the voltage during the off window is measured and reported, so a rail that never "
        "actually collapsed (bulk capacitance, a second supply, a coin cell) is flagged instead "
        "of being reported as a cold boot that did not happen. Requires confirm=true while the "
        "guard is in confirm mode or a session has a live GDB server on the port."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "channel": _CHANNEL,
            "off_ms": {"type": "integer", "description": "Milliseconds to hold power off (default 400)."},
            "settle_ms": {"type": "integer", "description": "Milliseconds to wait after restoring power (default 1500)."},
            "confirm": _CONFIRM,
        },
    }
))
def power_cycle_target(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    try:
        manager, channel, source = _resolve(ctx, arguments)
        profile_cycle = (ctx.debug_profile.get().get("hub") or {}).get("power_cycle") or {}
        result = manager.power_cycle(
            channel,
            off_ms=int(arguments.get("off_ms", profile_cycle.get("off_ms", 400))),
            settle_ms=int(arguments.get("settle_ms", profile_cycle.get("settle_ms", 1500))),
            confirm=bool(arguments.get("confirm")),
            live_session=_live_session(ctx, channel),
        )
        result["channel_source"] = source
        next_actions = ["recover_session", "self_check"]
        if result.get("browned_out") is False:
            next_actions = ["hub(action=cycle, off_ms=2000)", "hub(action=describe)"]
        return [content_success(result, suggested_next_actions=next_actions)]
    except (HubUnavailableError, HubBusyError, HubGuardError) as exc:
        return _error(exc)
    except ValueError as exc:
        return [content_error(str(exc), code="invalid_argument")]
