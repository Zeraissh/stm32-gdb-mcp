"""Programmable USB hub control: per-port power, USB2 data lines, and V/I sampling.

Five places in this repo document the same unfixable failure: hard-killing a GDB
server wedges the ST-Link USB endpoint "until the probe is physically unplugged"
(``process_guard``, ``gdb_manager.stop``, ``server._shutdown_all_sessions``,
README, and the single-target-excellence plan). Every existing mitigation --
the job object, PDEATHSIG, the probe lock, ``retry_call`` -- is software guarding
against a hardware failure mode, so none of them can cover the case the repo
itself names as uncoverable. A hub that can cut VBUS is the missing hand.

The vendor package (``smartusbhub``) is an OPTIONAL extra. Nothing here may be
imported at module scope: ``_import_backend`` is the single seam, and every
public entry point degrades to a clean "hub unavailable" error when the extra is
absent. Core debugging must behave byte-for-byte identically without it.

Channels are 1-based throughout (``get_channels() -> (1, 2, 3, 4)``), matching
the vendor API and the labels silkscreened on the hardware.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Protocol

# Below this, the port rail is considered collapsed and the board really did
# cold-boot. Calibrated against measurements on a 4CH hub, off-window readings:
#   94-166 mV  ST-Link + STM32L151 board drawing ~80 mA
#   345 mV     ST-Link + STM32L151 board drawing ~60 mA (more bulk capacitance)
# 345 mV sat uncomfortably close to the original 500 mV, so the threshold has
# headroom now. It stays far below any level that could keep a board alive: this
# measures the 5 V port rail, and an STM32's brown-out reset trips with its 3.3 V
# regulator input long gone. Raising it trades a false "did not cold-boot"
# warning (annoying, safe) against a false confirmation (a wrong answer) -- 800 mV
# is still nowhere near able to run a board.
BROWNOUT_MV = 800

# Minimum pre-cut draw for the brown-out verdict to mean anything. Below this the
# port has no measurable load, and a switched-off port with nothing on it floats
# rather than reading zero (3112 mV measured on an empty channel of a real 4CH
# hub, versus 167 mV on the same hub with an ST-Link attached).
LOAD_CURRENT_MA = 1

GUARD_MODES = ("allow", "confirm", "dry_run")
DEFAULT_GUARD_MODE = "confirm"

# Actions that remove power or data from a port. Reads never go through the guard.
MUTATING_ACTIONS = ("power_off", "power_on", "data_off", "data_on", "power_cycle")

INSTALL_HINT = "pip install 'stm32-gdb-mcp[hub]'"

# Seconds of no hub traffic after which the control port is handed back. Long
# enough that an ordinary sequence of hub calls never pays a reconnect, short
# enough that a session which merely looked at the hub is not still holding it
# minutes later. Override per-manager for tests; 0 disables.
IDLE_RELEASE_SEC = 30.0


class HubUnavailableError(RuntimeError):
    """The hub cannot be reached: extra not installed, no device, or link down.

    ``next_actions`` lets a raise site that knows WHICH cause applies carry the
    remedy for THAT cause out to the tool layer. Without it every hub failure got
    the same generic pair of suggestions, and for an unmapped channel the generic
    pair pointed at the wrong fix.
    """

    def __init__(self, *args: Any, next_actions: list[str] | None = None) -> None:
        super().__init__(*args)
        self.next_actions: list[str] = list(next_actions or [])


class HubBusyError(RuntimeError):
    """The hub's control port is held by another process or instance."""


class HubGuardError(RuntimeError):
    """The guard refused the action (confirmation required, or interlock mode)."""


class HubBackend(Protocol):
    """The vendor surface this module actually uses.

    Everything is typed against this protocol rather than ``SmartUSBHub`` so that
    mypy passes on a machine where the extra is not installed, and so the fake in
    the test suite is a first-class implementation rather than a duck-typed mock.
    """

    def get_channels(self) -> tuple[int, ...]: ...
    def get_device_info(self) -> dict: ...
    def get_operate_mode(self) -> int | None: ...
    def get_serial_no(self) -> str | None: ...
    def get_product_name(self) -> str | None: ...
    def get_channel_name(self, channel: int) -> str | None: ...
    def set_channel_power(self, *channels: int, state: int) -> bool: ...
    def get_channel_power_status(self, *channels: int) -> Any: ...
    def set_channel_usb2_dataline(self, *channels: int, state: int) -> bool: ...
    def get_channel_usb2_dataline_status(self, *channels: int) -> Any: ...
    def get_channel_measurements(self, *channels: int) -> Any: ...
    def register_disconnect_callback(self, callback: Any) -> None: ...
    def is_connected(self) -> bool: ...
    def disconnect(self) -> None: ...


def _import_backend():
    """The single import seam for the optional vendor package.

    Kept as a function so tests can force absence by patching this one name, and
    so importing ``mcp_server.hub`` never costs anything when no hub is in play.
    """
    from smartusbhub import SmartUSBHub  # noqa: PLC0415 - optional extra, imported lazily

    return SmartUSBHub


def _vendor_errors() -> tuple[type[BaseException], ...]:
    """Vendor exception classes, or an empty tuple when the extra is absent."""
    try:
        import smartusbhub  # noqa: PLC0415 - optional extra
    except ImportError:
        return ()
    return (smartusbhub.SmartUSBHubError,)


def _port_busy_error() -> tuple[type[BaseException], ...]:
    try:
        import smartusbhub  # noqa: PLC0415 - optional extra
    except ImportError:
        return ()
    return (smartusbhub.PortBusyError,)


class HubPowerGuard:
    """Confirmation policy + audit trail for actions that cut power or data.

    Deliberately NOT ``MemoryWriteGuard``: that guard decides by address range,
    and "hub channel 2" has no honest address range. It is also a per-session
    object while the hub is process-wide. The method names mirror it so the two
    read the same, but the decision logic is its own.
    """

    def __init__(self, mode: str = DEFAULT_GUARD_MODE) -> None:
        self.mode = mode
        self.audit_log: list[dict] = []

    def set_policy(self, mode: str | None = None) -> dict:
        if mode is not None:
            if mode not in GUARD_MODES:
                raise ValueError(f"mode must be one of: {', '.join(GUARD_MODES)}")
            self.mode = mode
        return self.policy()

    def policy(self) -> dict:
        return {"mode": self.mode}

    def evaluate(self, action: str, channel: int, confirm: bool = False,
                 live_session: str | None = None) -> dict:
        """Decide whether a mutating hub action may proceed.

        ``live_session`` is the id of a session whose GDB server is currently
        alive on this channel. When set, confirmation is required regardless of
        mode: an "allow" policy set for scripted CI must not silently brown out a
        board in the middle of a flash.

        ``confirm`` asserts intent and does not have to arrive as a caller's
        argument. Three entry points pass it themselves, because naming one of them
        IS the confirmation, and their decisions are audited exactly like an
        argument-confirmed one. They do NOT all protect the session the same way,
        and the difference matters:

        - ``reset_target(strategy="cold")`` and ``recover_session``'s escalation
          ladder stop the debug session before the cut and re-establish it after.
        - ``hub(action=discover)`` refuses outright while a session is live --
          unless ``force=true``, which bypasses this guard entirely
          (``_GuardBypass`` passes ``confirm=True`` and never passes
          ``live_session``). On that path the caller has overridden the interlock
          knowingly; nothing downstream re-checks it.
        """
        if self.mode == "dry_run":
            return {"action": "simulated", "reason": "dry_run mode is active",
                    "channel": channel, "live_session": live_session}

        if live_session is not None and not confirm:
            return {
                "action": "blocked",
                "reason": (f"session '{live_session}' has a live GDB server on channel {channel}; "
                           f"pass confirm=true to {action} anyway"),
                "channel": channel,
                "live_session": live_session,
            }

        if self.mode == "confirm" and not confirm:
            return {
                "action": "blocked",
                "reason": f"guard mode is 'confirm'; pass confirm=true to {action} channel {channel}",
                "channel": channel,
                "live_session": live_session,
            }

        return {"action": "apply", "reason": f"permitted by guard mode '{self.mode}'",
                "channel": channel, "live_session": live_session}

    def audit(self, action: str, channel: int, decision: dict, detail: dict | None = None) -> dict:
        entry = {
            "action": action,
            "channel": channel,
            "decision": decision["action"],
            "reason": decision["reason"],
        }
        if detail:
            entry.update(detail)
        self.audit_log.append(entry)
        return entry

    def get_audit_log(self, limit: int | None = None) -> list[dict]:
        if limit is None:
            return list(self.audit_log)
        return self.audit_log[-limit:]


class HubManager:
    """Process-wide owner of the one hub connection.

    A single USB-CDC serial port cannot be opened twice, so a per-session hub is
    not merely wasteful but physically impossible. The only per-session datum is
    which channel a board sits on, and that already has a home: the session's
    debug profile. Shape follows the existing precedent, ``probe_lock_manager``.

    The lock is an RLock because ``power_cycle`` re-enters ``power``. Contention
    is irrelevant at ~2.5 ms per vendor round-trip, but it is load-bearing: two
    NAMED sessions dispatch concurrently (``server._session_locks`` only
    serializes within a session), so two threads can reach the hub at once.
    """

    def __init__(self, backend_factory=None) -> None:
        self._lock = threading.RLock()
        self._backend_factory = backend_factory
        self._hub: HubBackend | None = None
        self._spec: dict = {}
        self._info: dict = {}
        self._channels: tuple[int, ...] = ()
        self._interlock = False
        self._link_lost = False
        # Channels this process powered off and has not powered back on. Restored
        # on shutdown so a killed MCP server never leaves a bench board dark.
        self._we_turned_off: set[int] = set()
        # Channels this process took OFF the USB bus and has not put back. The
        # data-line twin of _we_turned_off; see close().
        self._we_disconnected: set[int] = set()
        # Hand the control port back after this long with no hub traffic, so one
        # session cannot lock every other one out of the bench. 0 disables it.
        self._idle_release_sec: float = IDLE_RELEASE_SEC
        self._idle_timer: threading.Timer | None = None
        self._touched_at: float = 0.0
        self.guard = HubPowerGuard()

    # ---------------------------------------------------------------- config

    def configure(self, spec: dict | None) -> dict:
        """Apply a profile ``hub`` block. Does NOT connect -- connection is lazy.

        Reconfiguring a different port drops any existing connection, so a
        session that switches rigs does not keep talking to the old hub.
        """
        spec = dict(spec or {})
        with self._lock:
            if self._hub is not None and spec.get("port") and spec.get("port") != self._spec.get("port"):
                self._disconnect_locked()
            self._spec = spec
            guard_mode = spec.get("guard")
            if guard_mode is not None:
                self.guard.set_policy(guard_mode)
        return dict(self._spec)

    @property
    def spec(self) -> dict:
        return dict(self._spec)

    def is_available(self) -> bool:
        """True when a hub could be reached. Never raises -- absence is not an error."""
        try:
            self._ensure()
        except Exception:  # noqa: BLE001 - availability is a question, not an assertion
            return False
        return True

    # ------------------------------------------------------------ connection

    def _ensure(self) -> HubBackend:
        with self._lock:
            self._touched_at = time.monotonic()
            self._arm_idle_release_locked()
            if self._hub is not None and not self._link_lost:
                if self._hub.is_connected():
                    return self._hub
                self._link_lost = True
            if self._link_lost:
                self._disconnect_locked()
            return self._connect_locked()

    # ------------------------------------------------------- idle release
    #
    # The vendor library holds a CROSS-PROCESS lock on the hub's USB-CDC control
    # port, and this connection is lazy and then cached forever -- so one MCP
    # session that merely ran hub(action=describe) locked every other session out
    # of the hub until its process exited, with no way to hand it back. Measured:
    # a second process got "PortBusyError: Port COM7 is already in use by another
    # process".
    #
    # Releasing on idle fixes that without anyone having to remember: the next
    # call reconnects transparently, because connection was always lazy.

    def _arm_idle_release_locked(self, delay: float | None = None) -> None:
        if not self._idle_release_sec:
            return
        if self._idle_timer is not None:
            self._idle_timer.cancel()
        self._idle_timer = threading.Timer(
            self._idle_release_sec if delay is None else max(delay, 0.001),
            self._release_if_idle)
        self._idle_timer.daemon = True
        self._idle_timer.start()

    def _release_if_idle(self) -> None:
        with self._lock:
            if self._hub is None:
                return
            remaining = self._idle_release_sec - (time.monotonic() - self._touched_at)
            if remaining > 0:
                # Re-arm for what is left; do NOT just return. threading.Timer waits
                # on an Event, whose clock is not guaranteed to agree with
                # time.monotonic(), so a timer can fire a hair early -- and a plain
                # return would then leave nothing scheduled and hold the port
                # forever, which is the exact failure this feature exists to
                # prevent. Windows CI caught it; this machine's timing did not.
                self._arm_idle_release_locked(remaining)
                return
            if self._we_turned_off or self._we_disconnected:
                # NEVER release while this process owes a channel its power OR its
                # data line back. Holding the handle is exactly what lets shutdown
                # restore them, and close() only restores while connected --
                # dropping the link here would leave a board dark, or silently off
                # the USB bus, with nothing able to notice.
                self._arm_idle_release_locked()
                return
            # Power is deliberately NOT touched: an idle disconnect is not a
            # shutdown. Re-powering here would silently undo a deliberate
            # hub(action=power, state="off") just because the agent paused.
            self._disconnect_locked()

    def _default_factory(self, exclude_ports=None):
        """Open the vendor device: an explicit port when configured, else a scan.

        ``scan_and_connect`` filters on the hub's own VID/PID (0x1A86/0xFE0C)
        before opening anything, so it will not probe a target's UART. The
        ``exclude_ports`` list is still passed for the case where a serial log
        reader already owns a look-alike port.
        """
        try:
            backend_cls = _import_backend()
        except ImportError as exc:
            raise HubUnavailableError(
                f"hub unavailable: smartusbhub is not installed; {INSTALL_HINT}"
            ) from exc

        port = self._spec.get("port")
        if port:
            return backend_cls(port)
        return backend_cls.scan_and_connect(exclude_ports=exclude_ports)

    def _connect_locked(self) -> HubBackend:
        factory = self._backend_factory or self._default_factory
        try:
            hub = factory(exclude_ports=self._spec.get("exclude_ports"))
        except _port_busy_error() as exc:
            # The vendor raises PortBusyError for "could not open port" too, which
            # covers a port that does not exist at all -- so name both causes
            # rather than sending someone hunting for a process that isn't there.
            raise HubBusyError(
                f"hub port busy: {exc} Either another process holds the hub's control port, or "
                f"the configured port does not exist. Omit hub.port to auto-scan."
            ) from exc
        except _vendor_errors() as exc:
            raise HubUnavailableError(f"hub unavailable: {exc}") from exc
        except OSError as exc:
            raise HubUnavailableError(f"hub unavailable: {exc}") from exc

        if hub is None:
            port_note = f" on port {self._spec['port']}" if self._spec.get("port") else ""
            raise HubUnavailableError(
                f"hub unavailable: no SmartUSBHub found{port_note}. Check the hub's own USB cable, "
                f"or set hub.port in the debug profile."
            )

        self._hub = hub
        self._link_lost = False
        try:
            hub.register_disconnect_callback(self._on_disconnect)
        except Exception:  # noqa: BLE001 - callback registration is best effort
            pass

        try:
            self._channels = tuple(hub.get_channels())
        except Exception:  # noqa: BLE001 - fall back to the info block below
            self._channels = ()
        try:
            self._info = dict(hub.get_device_info() or {})
        except Exception:  # noqa: BLE001 - identity is informational
            self._info = {}
        if not self._channels:
            max_channels = self._info.get("max_channels")
            if isinstance(max_channels, int) and 0 < max_channels < 64:
                self._channels = tuple(range(1, max_channels + 1))

        self._interlock = self._info.get("operate_mode") == "interlock"
        return hub

    def _on_disconnect(self) -> None:
        with self._lock:
            self._link_lost = True

    def _disconnect_locked(self) -> None:
        # Cancel first: a timer left running past a disconnect is a thread that
        # wakes up holding nothing, and on shutdown it would keep the process alive
        # for its remaining delay.
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None
        hub, self._hub = self._hub, None
        self._channels = ()
        self._info = {}
        self._link_lost = False
        if hub is not None:
            try:
                hub.disconnect()
            except Exception:  # noqa: BLE001 - teardown must not raise
                pass

    def close(self, restore_power: bool = True, restore_data: bool = True) -> dict:
        """Disconnect, putting back whatever this process changed.

        Wired into ``server._shutdown_all_sessions``: a client that kills the MCP
        server must not leave a bench rack dark -- or, just as invisibly, off the
        USB bus.

        Data lines matter as much as power here and had no restore at all until
        now. ``hub(action=data, exclusive=true)`` switches off every OTHER
        channel's data line, which is the documented way to make
        start_debug_session's single-probe auto-select work on a rack -- so the
        common case leaves several boards disconnected, and nothing remembered to
        put them back. A dark board is at least visible; a board whose probe
        silently vanished from the bus looks like a broken cable.
        """
        restored: list[int] = []
        reconnected: list[int] = []
        with self._lock:
            if self._hub is not None and restore_power and self._we_turned_off:
                for channel in sorted(self._we_turned_off):
                    try:
                        if self._hub.set_channel_power(channel, state=1):
                            restored.append(channel)
                    except Exception:  # noqa: BLE001 - shutdown must not raise
                        pass
            if self._hub is not None and restore_data and self._we_disconnected:
                for channel in sorted(self._we_disconnected):
                    try:
                        if self._hub.set_channel_usb2_dataline(channel, state=1):
                            reconnected.append(channel)
                    except Exception:  # noqa: BLE001 - shutdown must not raise
                        pass
            self._we_turned_off.clear()
            self._we_disconnected.clear()
            self._disconnect_locked()
        return {"restored_channels": restored, "reconnected_channels": reconnected}

    # --------------------------------------------------------------- channels

    def channels(self) -> tuple[int, ...]:
        self._ensure()
        return self._channels

    def channel_for(self, explicit: int | None = None, profile: dict | None = None,
                    session_id: str | None = None, probe_serial: str | None = None) -> tuple[int, str]:
        """Resolve which hub channel a request targets.

        Precedence deliberately mirrors ``session_tools._probe_selection``:
        argument, then profile, then a configured map, then refuse. Guessing when
        the rig is ambiguous is how you power-cycle the wrong board.
        """
        if explicit is not None:
            return self._validated(explicit), "argument"

        # Fall back to the manager's own spec ONLY when the caller passed no
        # profile at all (a script driving HubManager directly). A session that
        # HAS a profile but no hub block must not inherit another session's
        # channel: this singleton is shared across sessions, and on a rack that
        # would resolve to a neighbouring board and power-cycle the wrong one.
        spec = dict((profile or {}).get("hub") or {}) if profile is not None else dict(self._spec)
        channel = spec.get("channel")
        if channel is not None:
            return self._validated(channel), "profile"

        entries = _normalize_map(spec.get("map"))
        if probe_serial:
            for ch, entry in sorted(entries.items()):
                if entry.get("serial") and entry["serial"] == probe_serial:
                    return self._validated(ch), "map_serial"
        if session_id:
            for ch, entry in sorted(entries.items()):
                if entry.get("label") == session_id:
                    return self._validated(ch), "map_label"

        raise self._unmapped(profile, spec, entries, session_id, probe_serial)

    def _unmapped(self, profile: dict | None, spec: dict, entries: dict[int, dict],
                  session_id: str | None, probe_serial: str | None) -> HubUnavailableError:
        """Name the ONE cause that applies, with the remedy that fixes THAT cause.

        The message used to list all three causes at once and offer a single
        remedy -- pin hub.channel -- which is the wrong answer for two of them.
        Observed on a rack: the config was loaded into the DEFAULT session only,
        hub(session="TC") then failed, and pinning a channel per session would
        have bypassed the map instead of loading it where it was missing.

        Everything below is decided from what this call actually knows: whether a
        profile was passed at all, whether it is empty, whether it has a hub
        block, and what the map can select on.
        """
        who = f'session "{session_id}"' if session_id else "this session"
        load = (f'debug_config(action=load, path=..., session="{session_id}")'
                if session_id else "debug_config(action=load, path=...)")
        pin = 'debug_profile(action=set, hub={"channel": N})'

        if not spec:
            if profile is None:
                return HubUnavailableError(
                    "hub channel unmapped: this HubManager has no hub spec -- configure() was "
                    "never given a channel or a map. This is the direct-caller path (a script "
                    'driving HubManager); pass explicit=N, or configure({"channel": N}) first.',
                    next_actions=["hub(action=describe)"],
                )
            shared = ""
            if self._spec:
                shared = (" A hub block IS configured elsewhere in this process, but it belongs "
                          "to another session and is deliberately never inherited -- on a rack, "
                          "inheriting it would power-cycle a neighbouring board.")
            if not profile:
                return HubUnavailableError(
                    f"hub channel unmapped: the debug profile for {who} is EMPTY, so no hub config "
                    f"was ever loaded into it. The profile is per-session and in-memory only: "
                    f"loading a rack config into one session leaves every other session empty, and "
                    f"a server restart clears them all.{shared} Load it into this session: {load}.",
                    next_actions=[load, "debug_profile(action=get)"],
                )
            return HubUnavailableError(
                f'hub channel unmapped: the debug profile for {who} has no "hub" block, so it '
                f"names neither a channel nor a hub.map.{shared} Load the rack config into this "
                f"session ({load}), or give this session its own hub block.",
                next_actions=[load, pin],
            )

        if not entries:
            return HubUnavailableError(
                f'hub channel unmapped: the hub block for {who} has no "channel" and no "map", so '
                f"nothing says which port this board is on. Fill the map in with "
                f"hub(action=discover, apply=true), or pin this port with {pin}.",
                next_actions=["hub(action=discover, apply=true)", pin],
            )

        labels = sorted(str(entry["label"]) for entry in entries.values() if entry.get("label"))
        serials = sorted(str(entry["serial"]) for entry in entries.values() if entry.get("serial"))

        if labels:
            tried = f'"{session_id}"' if session_id else "no session name at all"
            also = (f' The probe serial "{probe_serial}" matched no mapped serial either.'
                    if probe_serial and serials else "")
            return HubUnavailableError(
                f"hub channel unmapped: hub.map defines the labels {labels}, and a label is "
                f"compared against the debug session name EXACTLY -- this call ran under {tried}, "
                f"which is none of them.{also} Start this board's session under ITS OWN label, or "
                f"change that channel's label to the session name you are using. Which of those "
                f"labels is yours is not something this code can know -- it cannot see which "
                f"channel your board is on, and naming the wrong one would select, and power-cycle, "
                f"a different board.",
                next_actions=["hub(action=describe)", "debug_profile(action=get)"],
            )

        if serials:
            if probe_serial:
                return HubUnavailableError(
                    f'hub channel unmapped: hub.map selects by serial ({serials}), and the probe '
                    f'serial for {who}, "{probe_serial}", is not among them. No entry carries a '
                    f'"label", so there is no second way to select a channel. Refresh the serials '
                    f"with hub(action=discover, apply=true), or add a label equal to the session "
                    f"name.",
                    next_actions=["hub(action=discover, apply=true)", "hub(action=describe)"],
                )
            return HubUnavailableError(
                f"hub channel unmapped: hub.map selects by serial ({serials}), but {who} has no "
                f'probe serial to match -- no debug session is running and the profile has no '
                f'"serial". Start the session first, or add a "label" equal to the session name.',
                next_actions=["start_debug_session", "hub(action=describe)"],
            )

        return HubUnavailableError(
            f"hub channel unmapped: hub.map has entries for channels {sorted(entries)}, but none "
            f'carries a "label" or a "serial", so none of them can be selected. Discovery keys '
            f"serial-less probes by USB port, which identifies a channel but cannot select one: "
            f"give those channels a label equal to the session name, or pin one with {pin}.",
            next_actions=["hub(action=describe)", pin],
        )

    def _validated(self, channel: Any) -> int:
        """Range-check a channel against the connected hub, without forcing a connect.

        ``channel_for`` runs before any hardware is touched, so when the hub is
        not connected yet this only rejects obvious nonsense; ``_switch`` calls it
        again after ``_ensure`` when the real channel list is known.
        """
        if isinstance(channel, bool) or not isinstance(channel, int):
            raise HubUnavailableError(f"hub channel must be an integer, got {channel!r}")
        if self._channels:
            if channel not in self._channels:
                raise HubUnavailableError(
                    f"hub channel {channel} does not exist; this hub has channels {list(self._channels)}"
                )
        elif channel < 1:
            raise HubUnavailableError(f"hub channel must be >= 1 (channels are 1-based), got {channel}")
        return channel

    # ------------------------------------------------------------------ state

    def _status(self, getter, channels: tuple[int, ...]) -> dict[int, int | None]:
        """Normalize the vendor's single-vs-multi channel return shapes.

        ``get_channel_power_status`` returns a bare int for one channel and a dict
        for several; the data-line getter returns a dict either way; both return
        None on timeout and raise IndexError when called with no channels.
        """
        if not channels:
            return {}
        try:
            raw = getter(*channels)
        except Exception:  # noqa: BLE001 - a status read must never break describe
            return dict.fromkeys(channels)
        if raw is None:
            return dict.fromkeys(channels)
        if isinstance(raw, dict):
            return {ch: raw.get(ch) for ch in channels}
        return {channels[0]: raw}

    def _measurements(self, hub: HubBackend, channels: tuple[int, ...]) -> dict[int, dict] | None:
        """Per-channel voltage/current, dropping samples the device disowns.

        The vendor tags every sample ``valid``/``fresh``/``stale``. Passing an
        invalid one through as data would be the same false-success shape this
        server keeps fixing, so a sample the device does not stand behind is
        reported as None rather than as a number.
        """
        if not channels:
            return None
        try:
            raw = hub.get_channel_measurements(*channels)
        except Exception:  # noqa: BLE001 - models without an ADC raise FeatureNotSupportedError
            return None
        if not isinstance(raw, dict):
            return None

        samples = {}
        for channel in channels:
            sample = raw.get(channel)
            if not isinstance(sample, dict):
                continue
            if sample.get("valid") is False:
                samples[channel] = {"voltage": None, "current": None, "valid": False}
                continue
            samples[channel] = {
                "voltage": sample.get("voltage"),
                "current": sample.get("current"),
                "valid": True,
                "stale": bool(sample.get("stale")),
            }
        return samples

    def describe(self) -> dict:
        """Identity + per-channel power/data/measurement snapshot. Read-only."""
        hub = self._ensure()
        with self._lock:
            channels = self._channels
            power = self._status(hub.get_channel_power_status, channels)
            data = self._status(hub.get_channel_usb2_dataline_status, channels)
            measurements = self._measurements(hub, channels)
            names = {}
            for ch in channels:
                try:
                    name = hub.get_channel_name(ch)
                except Exception:  # noqa: BLE001 - naming is optional firmware
                    name = None
                if name:
                    names[ch] = name

            entries = _normalize_map(self._spec.get("map"))
            ports = []
            for ch in channels:
                entry: dict = {
                    "channel": ch,
                    "power": _on_off(power.get(ch)),
                    "data": _on_off(data.get(ch)),
                }
                if ch in names:
                    entry["name"] = names[ch]
                mapped = entries.get(ch)
                if mapped:
                    entry["mapped"] = mapped
                if measurements and ch in measurements:
                    sample = measurements[ch]
                    entry["voltage_mv"] = sample.get("voltage")
                    entry["current_ma"] = sample.get("current")
                ports.append(entry)

            return {
                "available": True,
                "device": dict(self._info),
                "channels": list(channels),
                "adc": measurements is not None,
                "interlock": self._interlock,
                "guard": self.guard.policy(),
                "ports": ports,
                "configured_channel": self._spec.get("channel"),
                "recent_actions": self.guard.get_audit_log(limit=10),
            }

    # --------------------------------------------------------------- mutation

    def _refuse_interlock(self, action: str) -> None:
        if self._interlock:
            raise HubGuardError(
                f"hub is in interlock mode, which powers exactly one port at a time -- {action} "
                f"would silently cut every other board on the rack. Clear it with the vendor tool "
                f"(set_operate_mode(0)) before using this server."
            )

    def power(self, channel: int, state: str, confirm: bool = False,
              live_session: str | None = None) -> dict:
        return self._switch("power", channel, state, confirm, live_session)

    def data(self, channel: int, state: str, confirm: bool = False,
             live_session: str | None = None, exclusive: bool = False) -> dict:
        """Set one channel's USB2 data line.

        ``exclusive`` additionally disconnects every OTHER channel's data line, so
        exactly one probe stays enumerated. That keeps ``start_debug_session``'s
        single-probe auto-select on its safe path on a multi-board rack. It is
        never implicit -- un-enumerating three neighbouring boards is exactly the
        kind of invisible side effect this server refuses to hide.
        """
        result = self._switch("data", channel, state, confirm, live_session)
        if not exclusive or result["applied"] is not True or state != "on":
            return result

        hub = self._ensure()
        others = [ch for ch in self._channels if ch != channel]
        turned_off = []
        for other in others:
            if hub.set_channel_usb2_dataline(other, state=0):
                turned_off.append(other)
                # This path talks to the backend directly rather than through
                # _switch (one guard decision covers the whole isolation), so the
                # bookkeeping _switch would have done has to happen here too --
                # otherwise the very case that strands boards, isolating on a rack,
                # is the one case nothing remembers to undo.
                self._we_disconnected.add(other)
        self._we_disconnected.discard(channel)
        result["exclusive"] = True
        result["data_off_channels"] = turned_off
        return result

    def power_cycle(self, channel: int, off_ms: int = 400, settle_ms: int = 1500,
                    confirm: bool = False, live_session: str | None = None,
                    sleep=None, monotonic=None) -> dict:
        """Remove port power for ``off_ms``, restore it, and report what was measured.

        The measurement is not decoration. Bulk capacitance, a coin cell, or a
        VBAT domain can hold VDD up across a short cut, in which case the board
        never actually cold-booted -- and a "cold boot" that silently did nothing
        is exactly the false-success shape this server keeps getting bitten by.
        On a model with an ADC the off-window voltage is sampled and reported, so
        the caller can see whether the rail really collapsed.

        The verdict is conditioned on the port having drawn current BEFORE the cut.
        Measured on real hardware: a port with an ST-Link on it read 167 mV during
        the off window, but an EMPTY port read 3112 mV -- an undriven node floats,
        it is not a board staying alive. Without the pre-cut load check, every
        power cycle of an idle port would warn that the board refused to cold-boot.

        ``sleep``/``monotonic`` are injectable so tests run on a fake clock.
        """
        sleep = sleep or time.sleep
        monotonic = monotonic or time.monotonic

        # Connect before the pre-cut sample: it runs ahead of the first power()
        # call, which is what would otherwise have triggered the connect.
        self._ensure()

        started = monotonic()
        pre_current = self._sample_current(channel)
        self.power(channel, "off", confirm=confirm, live_session=live_session)

        off_voltage = self._sample_voltage(channel)
        sleep(off_ms / 1000.0)
        # Sample again at the end of the window: a slowly draining rail reads high
        # immediately after the cut and only collapses later.
        late_voltage = self._sample_voltage(channel)
        measured = _min_optional(off_voltage, late_voltage)

        self.power(channel, "on", confirm=True)
        if settle_ms:
            sleep(settle_ms / 1000.0)

        result: dict = {
            "channel": channel,
            "action": "power_cycle",
            "off_ms": off_ms,
            "settle_ms": settle_ms,
            "elapsed_ms": int(round((monotonic() - started) * 1000)),
        }
        if pre_current is not None:
            result["pre_current_ma"] = pre_current
        if measured is not None:
            result["measured_off_voltage_mv"] = measured

            if pre_current is not None and pre_current < LOAD_CURRENT_MA:
                # An undriven port floats -- measured at 3112 mV on an empty channel
                # of a real 4CH hub. Calling that "the board stayed powered" would be
                # a confident wrong answer, so say what is actually known.
                result["browned_out"] = None
                result["warning"] = (
                    f"channel {channel} drew {pre_current} mA before the cut, so the "
                    f"{measured} mV read during the off window cannot confirm a cold boot -- "
                    f"an unloaded port floats rather than reading zero. Power WAS removed "
                    f"either way. Causes, in order: nothing is connected to this port; the "
                    f"target is in deep sleep (under {LOAD_CURRENT_MA} mA); or this channel's "
                    f"current sense is temporarily not reporting. That third case is real and "
                    f"TRANSIENT: on a 4CH hub one channel returned 0 mA for several minutes "
                    f"after a power cycle -- surviving an overcurrent-latch clear, two more "
                    f"power cycles and a hub-MCU reboot -- while its voltage sense and power "
                    f"switching kept working, then recovered on its own. Before concluding "
                    f"anything from a 0 mA reading, re-read it and compare against another "
                    f"channel with a known load."
                )
            else:
                result["browned_out"] = measured < BROWNOUT_MV
                if not result["browned_out"]:
                    result["warning"] = (
                        f"channel {channel} still read {measured} mV with power removed while "
                        f"drawing {pre_current} mA before the cut, so the board did NOT "
                        f"cold-boot. It is powered from somewhere else (a second supply, a coin "
                        f"cell, or the probe's own VBUS), or bulk capacitance held it up -- try "
                        f"a longer off_ms."
                    )
        return result

    def measure(self, channels: tuple[int, ...] | None = None, duration_sec: float = 0.0,
                interval_sec: float = 0.05, sleep=None, monotonic=None) -> dict:
        """Sample per-port voltage and current over a window.

        This is the only instrument in this server that sees the target's actual
        power draw, which is what makes a low-power claim checkable: an MCU that
        "entered Stop" and one that only thinks it did read the same over SWD and
        differ by orders of magnitude here.

        Returns the ``{series, summary, timing}`` shape used elsewhere for sampled
        data. A zero duration takes exactly one snapshot.
        """
        sleep = sleep or time.sleep
        monotonic = monotonic or time.monotonic

        hub = self._ensure()
        targets = tuple(channels) if channels else self._channels
        for channel in targets:
            self._validated(channel)

        series: dict[int, list[dict]] = {ch: [] for ch in targets}
        started = monotonic()
        deadline = started + max(duration_sec, 0.0)
        samples = 0
        while True:
            elapsed = monotonic() - started
            snapshot = self._measurements(hub, targets)
            if snapshot is None:
                raise HubUnavailableError(
                    "hub measurement unavailable: this model has no ADC, or the read timed out. "
                    "Use hub(action=describe) to check which features the device reports."
                )
            for channel, sample in snapshot.items():
                series[channel].append({
                    "t_ms": int(round(elapsed * 1000)),
                    "voltage_mv": sample.get("voltage"),
                    "current_ma": sample.get("current"),
                })
            samples += 1
            if monotonic() >= deadline:
                break
            sleep(max(interval_sec, 0.0))

        return {
            "series": {str(ch): points for ch, points in series.items()},
            "summary": {str(ch): _summarize(points) for ch, points in series.items()},
            "timing": {
                "samples": samples,
                "duration_sec": duration_sec,
                "interval_sec": interval_sec,
                "elapsed_ms": int(round((monotonic() - started) * 1000)),
            },
        }

    def _sample_voltage(self, channel: int) -> int | None:
        return self._sample_field(channel, "voltage")

    def _sample_current(self, channel: int) -> int | None:
        return self._sample_field(channel, "current")

    def _sample_field(self, channel: int, field: str) -> int | None:
        hub = self._hub
        if hub is None:
            return None
        sample = self._measurements(hub, (channel,))
        if not sample or channel not in sample:
            return None
        value = sample[channel].get(field)
        return value if isinstance(value, int) else None

    def _switch(self, kind: str, channel: int, state: str, confirm: bool,
                live_session: str | None) -> dict:
        if state not in ("on", "off"):
            raise ValueError("state must be 'on' or 'off'")
        hub = self._ensure()
        channel = self._validated(channel)
        action = f"{kind}_{state}"

        with self._lock:
            if kind == "power":
                self._refuse_interlock(action)
            decision = self.guard.evaluate(action, channel, confirm=confirm, live_session=live_session)
            if decision["action"] == "blocked":
                self.guard.audit(action, channel, decision)
                raise HubGuardError(decision["reason"])
            if decision["action"] == "simulated":
                self.guard.audit(action, channel, decision)
                return {"channel": channel, "action": action, "applied": None,
                        "simulated": True, "reason": decision["reason"]}

            setter = hub.set_channel_power if kind == "power" else hub.set_channel_usb2_dataline
            ok = bool(setter(channel, state=1 if state == "on" else 0))
            if kind == "power":
                if state == "off" and ok:
                    self._we_turned_off.add(channel)
                elif state == "on":
                    self._we_turned_off.discard(channel)
            elif kind == "data":
                # Symmetric with power, and for the same reason: this process took
                # the channel off the bus, so this process owes it back. exclusive
                # isolation routes every "other" channel through here, so the
                # bookkeeping covers the case that actually bites.
                if state == "off" and ok:
                    self._we_disconnected.add(channel)
                elif state == "on":
                    self._we_disconnected.discard(channel)
            self.guard.audit(action, channel, decision, {"acknowledged": ok})

        if not ok:
            raise HubUnavailableError(
                f"hub did not acknowledge {action} on channel {channel}; the link may have dropped"
            )
        # Cutting power or data changes what is enumerated on the USB bus, and
        # detect_probe is CACHED -- and is precisely how a caller confirms this
        # switch took effect. Serving a pre-switch enumeration afterwards would
        # report a bench state that no longer exists, which is worse than the
        # re-enumeration cost it saves. Imported here, not at module scope, to keep
        # the hub layer free of a hard dependency on probe detection.
        from .openocd_config import invalidate_probe_cache
        invalidate_probe_cache()
        return {"channel": channel, "action": action, "applied": True}


def _normalize_map(raw: Any) -> dict[int, dict]:
    """Coerce a YAML ``hub.map`` into {int channel: {serial?, label?}}.

    YAML keys may arrive as ints or strings depending on how the file was written;
    both must resolve to the same channel.
    """
    if not isinstance(raw, dict):
        return {}
    entries: dict[int, dict] = {}
    for key, value in raw.items():
        try:
            channel = int(key)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            entries[channel] = dict(value)
        elif isinstance(value, str):
            entries[channel] = {"label": value}
    return entries


def _summarize(points: list[dict]) -> dict:
    """min/max/mean per measured field, skipping samples the device marked invalid."""
    summary: dict = {"samples": len(points)}
    for field in ("voltage_mv", "current_ma"):
        values = [p[field] for p in points if isinstance(p.get(field), (int, float))]
        if not values:
            continue
        summary[field] = {
            "min": min(values),
            "max": max(values),
            "mean": round(sum(values) / len(values), 3),
        }
    return summary


def _min_optional(*values: int | None) -> int | None:
    present = [value for value in values if value is not None]
    return min(present) if present else None


def _on_off(value: Any) -> str | None:
    if value is None:
        return None
    return "on" if value else "off"


hub_manager = HubManager()
