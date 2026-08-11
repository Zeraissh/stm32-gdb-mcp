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
from typing import Any, Protocol

GUARD_MODES = ("allow", "confirm", "dry_run")
DEFAULT_GUARD_MODE = "confirm"

# Actions that remove power or data from a port. Reads never go through the guard.
MUTATING_ACTIONS = ("power_off", "power_on", "data_off", "data_on", "power_cycle")

INSTALL_HINT = "pip install 'stm32-gdb-mcp[hub]'"


class HubUnavailableError(RuntimeError):
    """The hub cannot be reached: extra not installed, no device, or link down."""


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
            if self._hub is not None and not self._link_lost:
                if self._hub.is_connected():
                    return self._hub
                self._link_lost = True
            if self._link_lost:
                self._disconnect_locked()
            return self._connect_locked()

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
            raise HubBusyError(
                f"hub port busy: {exc}. Another stm32-gdb-mcp process or vendor tool holds the "
                f"hub's control port."
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
        hub, self._hub = self._hub, None
        self._channels = ()
        self._info = {}
        self._link_lost = False
        if hub is not None:
            try:
                hub.disconnect()
            except Exception:  # noqa: BLE001 - teardown must not raise
                pass

    def close(self, restore_power: bool = True) -> dict:
        """Disconnect, optionally re-powering channels this process turned off.

        Wired into ``server._shutdown_all_sessions``: a client that kills the MCP
        server must not leave a bench rack dark.
        """
        restored: list[int] = []
        with self._lock:
            if self._hub is not None and restore_power and self._we_turned_off:
                for channel in sorted(self._we_turned_off):
                    try:
                        if self._hub.set_channel_power(channel, state=1):
                            restored.append(channel)
                    except Exception:  # noqa: BLE001 - shutdown must not raise
                        pass
            self._we_turned_off.clear()
            self._disconnect_locked()
        return {"restored_channels": restored}

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

        spec = dict((profile or {}).get("hub") or {}) or self._spec
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

        raise HubUnavailableError(
            "hub channel unmapped: no channel given, none in the debug profile, and no hub.map "
            "entry matches this session. Set it with debug_profile(action=set, hub={\"channel\": N})."
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
        if not channels:
            return None
        try:
            raw = hub.get_channel_measurements(*channels)
        except Exception:  # noqa: BLE001 - models without an ADC raise FeatureNotSupportedError
            return None
        if not isinstance(raw, dict):
            return None
        return {ch: raw[ch] for ch in channels if ch in raw}

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
        result["exclusive"] = True
        result["data_off_channels"] = turned_off
        return result

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
            self.guard.audit(action, channel, decision, {"acknowledged": ok})

        if not ok:
            raise HubUnavailableError(
                f"hub did not acknowledge {action} on channel {channel}; the link may have dropped"
            )
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


def _on_off(value: Any) -> str | None:
    if value is None:
        return None
    return "on" if value else "off"


hub_manager = HubManager()
