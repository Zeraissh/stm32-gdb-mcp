"""Helpers shared between server.py's inline handlers and domain tool modules.

Never import server.py from here — callers pass session objects in explicitly.
"""

import logging
import time
from typing import Any

from ..hub_recovery import HubRecoveryError
from ..hub_recovery import escalate as hub_escalate
from ..reliability import retry_call

_logger = logging.getLogger(__name__)


def core_state(gdb_client: Any) -> str | None:
    """"running"/"halted" from the state GDB already reported, or None if unknown.

    Costs no target traffic: GdbClientManager tracks this from GDB's own
    *running/*stopped async records. Reads that carry it let an agent notice a
    stopped core immediately, instead of inferring it hours later from some
    unrelated instrument going quiet (issue #33).
    """
    getter = getattr(gdb_client, "is_running", None)
    if getter is None:
        return None
    try:
        running = getter()
    except Exception:
        return None
    if running is None:
        return None
    return "running" if running else "halted"


def autoload_symbols(sess: Any) -> bool:
    """Load symbols from the profile's elf_path after a connect, if configured.

    After loading, runs a non-throwing compare-sections to check whether the ELF
    matches the target flash. A mismatch is logged at WARNING level so that
    downstream agents can see the discrepancy even when there is no MCP response
    carrier (this path is called from session setup, not from a tool handler).
    """
    elf_path = sess.debug_profile.get().get("elf_path")
    if not elf_path:
        return False
    try:
        sess.gdb_client.load_symbols(elf_path)
    except Exception:
        return False
    # Non-throwing consistency check: log a warning if symbols don't match flash.
    # Read-only sections only -- writable ones hold the ELF's initial values and
    # differ on any target that has run, which would make this fire on almost
    # every connect and teach readers to ignore it.
    try:
        report = sess.gdb_client.compare_sections_report(read_only=True)
        if report["checked"]:
            if report["mismatched"]:
                _logger.warning(
                    "Auto-loaded symbols from %s do NOT match target flash — %d read-only section(s) "
                    "mismatched: %s. Values read through these symbols will be meaningless. "
                    "Re-flash or use the matching ELF.",
                    elf_path,
                    len(report["mismatched"]),
                    "; ".join(report["mismatched"]),
                )
        else:
            _logger.info(
                "Could not verify symbol-flash match for %s: %s", elf_path, report.get("reason", "unknown")
            )
    except Exception:
        # compare_sections_report is designed not to raise, but guard anyway.
        _logger.debug("compare_sections_report failed unexpectedly", exc_info=True)
    return True


def hub_binding(ctx: Any) -> tuple[Any, int] | None:
    """The (manager, channel) this session's board sits on, or None.

    Returns None for every "no hub here" reason -- extra not installed, no hub
    block in the profile, no channel mapped, hub offline. A missing hub is never
    an error, just no escalation available, so this must not raise.
    """
    return resolve_hub_binding(ctx)[0]


def resolve_hub_binding(ctx: Any) -> tuple[tuple[Any, int] | None, str | None]:
    """``hub_binding`` plus WHY it came back empty.

    The reason exists because collapsing every cause into None made one caller
    lie: reset_target's cold path reported "this session has a hub block but no
    channel resolved from it" whenever the binding was missing -- including when
    the channel resolved perfectly and the hub was simply unreachable, or the
    optional extra was not installed. Telling someone to fix their map when the
    map is fine sends them the wrong way.

    Callers that only care whether a hub is available keep using ``hub_binding``.
    """
    try:
        manager = ctx.fns.hub_manager
        profile = ctx.debug_profile.get()
        spec = profile.get("hub")
        if not spec:
            return None, "this session's debug profile has no hub block"
        manager.configure(spec)
        channel, _source = manager.channel_for(
            profile=profile,
            session_id=ctx.session_id,
            probe_serial=getattr(ctx.sess, "serial", None) or profile.get("serial"),
        )
    except Exception as exc:  # noqa: BLE001 - no hub is a normal state, not a failure
        return None, str(exc) or type(exc).__name__
    return (manager, channel), None


def recover_current_session(gdb_client: Any, gdb_manager: Any, last_session: dict, sess: Any,
                            hub: Any = None, channel: int | None = None,
                            escalate: bool = True, deep: bool = False,
                            sleep=None) -> dict:
    """Tear down client+server and bring the session back up.

    With ``hub`` unset -- no extra installed, no hub configured, or a caller that
    opted out -- this is exactly the pre-hub code path: one ``retry_call`` with
    the same attempts and backoff. The hub only ever adds rungs ABOVE that, and
    only for failures that re-enumeration can actually cure.
    """
    # Resolved at call time, not bound as a default, so tests can replace
    # _helpers.time with a fake clock and keep the suite at unit speed.
    sleep = sleep or time.sleep
    if not last_session.get("server_type"):
        raise RuntimeError("No prior session to recover; call start_debug_session first.")
    for teardown in (gdb_client.stop_gdb, gdb_manager.stop):
        try:
            teardown()
        except Exception:
            pass

    def start():
        return gdb_manager.start(last_session["server_type"], last_session["server_args"])

    steps: list[dict] = []
    if hub is not None and escalate:
        # Honour the profile's power_cycle timings, same as reset_target(cold).
        # A board that needs 800 ms to brown out needs it on every path, and
        # having the ladder quietly use 400 ms instead is the kind of split
        # behaviour that makes a flaky rig look like a flaky tool.
        cycle: dict = {}
        try:
            cycle = (sess.debug_profile.get().get("hub") or {}).get("power_cycle") or {}
        except Exception:  # noqa: BLE001 - timings are an optimization, not a requirement
            cycle = {}
        try:
            port, steps = hub_escalate(start, hub, channel, deep=deep, sleep=sleep,
                                       off_ms=cycle.get("off_ms"),
                                       settle_ms=cycle.get("settle_ms"))
        except HubRecoveryError as exc:
            # Surface the original failure, not a wrapper: callers classify on it.
            raise exc.cause if exc.cause is not None else exc from None
    else:
        port = retry_call(start, attempts=3, backoff_base=0.8, sleep=sleep)

    gdb_client.start_gdb()
    resp = gdb_client.connect("localhost", port)
    symbols = autoload_symbols(sess)
    recovered = {
        "message": "Session recovered",
        "server_type": last_session["server_type"],
        "port": port,
        "symbols_loaded": symbols,
        "raw_response": resp,
    }
    # Only reported when the hub was actually involved: a plain soft recovery must
    # look exactly like it always did.
    if any(step.get("rung") != "soft" for step in steps):
        recovered["recovery_steps"] = steps
    return recovered
