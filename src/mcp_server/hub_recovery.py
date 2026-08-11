"""Escalating recovery for a probe that software teardown cannot revive.

``retry_call`` answers one question: will waiting help? That covers a busy probe
and a momentary link drop, and it is the whole of today's recovery. It cannot
cover a wedged ST-Link USB endpoint, which this repo documents in five places as
needing a physical replug -- waiting does nothing, because the device is in a
state only re-enumeration clears.

This module adds the rungs above waiting, in increasing order of violence:

    soft              -> exactly today's retry_call, unchanged
    data_toggle       -> drop the USB data lines; re-enumerates the probe without
                         rebooting the board it is attached to
    power_cycle       -> cut VBUS; cold-boots the board too
    power_cycle_long  -> same, long enough to drain bulk capacitance (deep only)

Nothing here is reachable without a configured hub: ``escalate`` with ``hub=None``
runs the soft rung and nothing else, which is byte-for-byte today's behaviour.
"""

from __future__ import annotations

import time
from typing import Any

from .error_taxonomy import classify_error
from .reliability import retry_call

# Failures where physically re-enumerating the probe is a plausible cure.
#
# Deliberately NOT the taxonomy's ``retryable`` flag. That flag answers "will
# waiting help?"; this set answers "will re-enumeration help?". They are
# different questions, so they get different tables and neither module has to
# know about the other.
#
# probe_locked is excluded ON PURPOSE: the lock file is held by a live PID, and
# a power cycle cannot delete it, so the next rung would fail identically -- you
# would have browned out a board for nothing. See ``force_steal`` on
# recover_session for the one case where stealing it is the caller's decision.
HUB_RECOVERABLE_CODES = frozenset({
    "probe_busy",
    "probe_unavailable",
    "target_unresponsive",
    "connection_lost",
    "target_unreachable",
})

RUNGS = ("soft", "data_toggle", "power_cycle", "power_cycle_long")

DEFAULT_DATA_TOGGLE_MS = 300
DEFAULT_OFF_MS = 400
DEFAULT_LONG_OFF_MS = 2000
DEFAULT_SETTLE_MS = 1500


class HubRecoveryError(RuntimeError):
    """Every rung was tried and the session still would not come back."""

    def __init__(self, message: str, steps: list[dict], cause: Exception | None = None):
        super().__init__(message)
        self.steps = steps
        self.cause = cause


def _classified(exc: Exception, classify) -> str:
    try:
        return str(classify(str(exc)).get("code") or "")
    except Exception:  # noqa: BLE001 - classification must never mask the real error
        return ""


def _attempt(start_fn, attempts: int, backoff_base: float, sleep) -> Any:
    return retry_call(start_fn, attempts=attempts, backoff_base=backoff_base, sleep=sleep)


def escalate(start_fn, hub=None, channel: int | None = None, *, deep: bool = False,
             classify=classify_error, sleep=time.sleep, monotonic=time.monotonic,
             off_ms: int | None = None, settle_ms: int | None = None) -> tuple[Any, list[dict]]:
    """Bring a GDB server back up, escalating to the hub only when it can help.

    ``start_fn`` is the caller's server-start thunk; its return value (a port) is
    passed straight back. Returns ``(result, steps)`` where ``steps`` records what
    was actually done to the hardware -- a tool that physically power-cycled a
    board must say so, not just report success.

    Raises ``HubRecoveryError`` when every applicable rung failed, carrying both
    the steps and the original exception.
    """
    steps: list[dict] = []
    off_ms = DEFAULT_OFF_MS if off_ms is None else off_ms
    settle_ms = DEFAULT_SETTLE_MS if settle_ms is None else settle_ms

    # Rung 0 -- identical to the pre-hub code path.
    started = monotonic()
    try:
        result = _attempt(start_fn, 3, 0.8, sleep)
        steps.append({"rung": "soft", "ok": True, "elapsed_ms": _ms(monotonic() - started)})
        return result, steps
    except Exception as exc:  # noqa: BLE001 - re-raised below if no rung helps
        code = _classified(exc, classify)
        steps.append({"rung": "soft", "ok": False, "code": code, "error": str(exc),
                      "elapsed_ms": _ms(monotonic() - started)})
        last_exc: Exception = exc

    if hub is None or channel is None:
        raise HubRecoveryError(str(last_exc), steps, last_exc)

    if code not in HUB_RECOVERABLE_CODES:
        steps.append({"rung": "escalation_skipped", "ok": False, "code": code,
                      "reason": f"'{code}' is not cured by re-enumerating the probe"})
        raise HubRecoveryError(str(last_exc), steps, last_exc)

    rungs: list[tuple[str, dict]] = [
        ("data_toggle", {"off_ms": DEFAULT_DATA_TOGGLE_MS}),
        ("power_cycle", {"off_ms": off_ms, "settle_ms": settle_ms}),
    ]
    if deep:
        rungs.append(("power_cycle_long", {"off_ms": DEFAULT_LONG_OFF_MS, "settle_ms": settle_ms}))

    for name, params in rungs:
        step: dict = {"rung": name, "channel": channel, **params}
        started = monotonic()
        try:
            if name == "data_toggle":
                hub.data(channel, "off", confirm=True)
                sleep(params["off_ms"] / 1000.0)
                hub.data(channel, "on", confirm=True)
            else:
                cycle = hub.power_cycle(channel, off_ms=params["off_ms"],
                                        settle_ms=params["settle_ms"], confirm=True,
                                        sleep=sleep, monotonic=monotonic)
                for key in ("measured_off_voltage_mv", "browned_out"):
                    if key in cycle:
                        step[key] = cycle[key]
        except Exception as exc:  # noqa: BLE001 - a rung that cannot run is not fatal
            step.update({"ok": False, "error": str(exc), "elapsed_ms": _ms(monotonic() - started)})
            steps.append(step)
            continue

        try:
            result = _attempt(start_fn, 2, 0.8 if name == "data_toggle" else 1.2, sleep)
        except Exception as exc:  # noqa: BLE001 - try the next rung
            last_exc = exc
            step.update({"ok": False, "code": _classified(exc, classify), "error": str(exc),
                         "elapsed_ms": _ms(monotonic() - started)})
            steps.append(step)
            continue

        step.update({"ok": True, "attach_latency_ms": _ms(monotonic() - started)})
        steps.append(step)
        return result, steps

    raise HubRecoveryError(str(last_exc), steps, last_exc)


def _ms(seconds: float) -> int:
    return int(round(seconds * 1000))
