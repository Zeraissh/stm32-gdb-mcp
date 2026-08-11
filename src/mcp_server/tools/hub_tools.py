"""Programmable USB hub tools: per-port power and USB2 data-line control.

These are the only tools in this server that can act on the target's power rail,
which makes them the answer to the failure this repo has documented five times
over: a wedged ST-Link USB endpoint that no software teardown can clear.

Every handler degrades cleanly when the ``[hub]`` extra is absent or no hub is
attached -- ``hub_unavailable`` with an install hint, never a traceback.
"""

from mcp.types import TextContent, Tool

from ..hub import HubBusyError, HubGuardError, HubUnavailableError
from ..tool_response import content_error, content_success
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
