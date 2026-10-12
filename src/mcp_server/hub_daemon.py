"""One process owns the hub; every MCP server talks to it.

The hub's USB-CDC control port opens in exactly one process at a time, so with
several MCP servers on a rack (one IDE window per board) the first server to
touch the hub locked every other one out, and an in-process dashboard could only
ever show its own server's view. This daemon is the single owner instead:

- **Reads** go through one ``HubManager``. The dashboard sampler publishes one
  snapshot per interval to every viewer, and a client's ``describe`` is a fresh
  read (a cached one could show "on" right after that client switched a port off).
- **Writes** lock only the channels they touch (``hub_locks``): a power cycle
  on channel 1 never interleaves with another switch on channel 1, but a switch
  on channel 4 runs alongside it. Exclusive isolation and port changes lock the
  whole rack; a discovery run holds a rack-wide lease across its calls. The
  guard decides with the CALLING client's guard mode (thread-local), so one
  server set to ``allow`` for CI does not relax the guard for any other.
- **Restore-on-exit** is tracked per client: what a client switched off is put
  back when that client closes, or when its PID is found dead.

Everything binds to 127.0.0.1. The dashboard page and ``/api/state`` are
read-only; ``POST /rpc`` needs the token from the per-user state file.

    python -m mcp_server.hub_daemon serve [--config bench_hub.yaml]
    python -m mcp_server.hub_daemon status
    python -m mcp_server.hub_daemon stop

MCP servers spawn it on first hub use (``STM32_GDB_MCP_HUB_MODE=auto``, the
default); see ``hub_remote``.
"""

from __future__ import annotations

import argparse
import getpass
import hmac
import json
import os
import secrets
import signal
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from . import __version__
from .hub import GUARD_MODES, HubBusyError, HubGuardError, HubManager, HubUnavailableError
from .hub_dashboard import DEFAULT_INTERVAL_SEC, DEFAULT_PORT, HubDashboard
from .hub_locks import ALL, ChannelLocks
from .probe_lock import _pid_alive

# Bumped whenever an RPC's shape changes. A client refuses a daemon speaking a
# different protocol rather than half-working against it (the usual cause is a
# daemon still running from before an upgrade).
PROTOCOL = 3

# With nobody calling and nobody watching for this long, the daemon exits and
# hands the hub back -- unless it still owes a channel its power or data line.
DEFAULT_IDLE_EXIT_SEC = 600.0
MAINTENANCE_SEC = 2.0
# How long a write waits for another client's write to finish before it is
# refused. Refused, not run late: a power cut decided 30 s ago may no longer be
# what anyone wants. A caller can pass max_wait_sec per call.
DEFAULT_MAX_WAIT_SEC = 30.0
# A client whose heartbeat is older than this is shown as stale on the
# dashboard. Its sessions still count as live for the interlock until its PID
# is gone: assuming a silent server is idle is how a flash gets interrupted.
HEARTBEAT_STALE_SEC = 10.0
# A lease (held across several calls, e.g. by discovery) expires on its own after
# this long unless released, so a hung client cannot lock the rack forever.
DEFAULT_LEASE_TTL_SEC = 600.0
MAX_LEASE_TTL_SEC = 3600.0

_ERROR_TYPES: dict[str, type[Exception]] = {
    "HubUnavailableError": HubUnavailableError,
    "HubBusyError": HubBusyError,
    "HubGuardError": HubGuardError,
    "ValueError": ValueError,
    "TypeError": TypeError,
}


# ------------------------------------------------------------------ state files


def state_dir() -> str:
    """Per-user directory for the daemon's lock, state (with token) and log."""
    override = os.environ.get("STM32_GDB_MCP_HUB_DAEMON_DIR")
    if override:
        return override
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 - no login name (containers); still per-uid below
        user = str(getattr(os, "getuid", lambda: "user")())
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in user) or "user"
    return os.path.join(tempfile.gettempdir(), "stm32-gdb-mcp", f"hub-daemon-{safe}")


def state_path() -> str:
    return os.path.join(state_dir(), "daemon.json")


def _lock_path() -> str:
    return os.path.join(state_dir(), "daemon.lock")


def log_path() -> str:
    return os.path.join(state_dir(), "daemon.log")


def _ensure_dir() -> None:
    os.makedirs(state_dir(), mode=0o700, exist_ok=True)


def read_state() -> dict | None:
    """The running daemon's state file, or None when absent, corrupt, or stale."""
    try:
        with open(state_path(), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not data.get("url") or not data.get("token"):
        return None
    if not _pid_alive(data.get("pid")):
        return None
    return data


def _write_private(path: str, payload: dict) -> None:
    # The state file carries the write token: create it owner-only from the
    # start rather than chmod-ing after a window in which it was readable.
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    os.replace(tmp, path)


def _remove(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _acquire_instance_lock() -> bool:
    """O_EXCL lock file holding our PID; a lock whose PID is dead is stale."""
    path = _lock_path()
    for _attempt in range(2):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            try:
                with open(path, encoding="utf-8") as handle:
                    holder = int(handle.read().strip() or 0)
            except (OSError, ValueError):
                holder = 0
            if holder and _pid_alive(holder):
                return False
            _remove(path)
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
        return True
    return False


# ------------------------------------------------------------------ rpc client


def rpc_call(url: str, token: str, method: str, params: dict | None = None,
             client: dict | None = None, timeout: float = 30.0) -> Any:
    """POST one call to the daemon; re-raise its error as the original type.

    Transport failures surface as ``OSError`` (``urllib.error.URLError`` is
    one) so the caller can tell "daemon gone" apart from "hub said no".
    """
    body = json.dumps({"method": method, "params": params or {}, "client": client or {},
                       "protocol": PROTOCOL}).encode("utf-8")
    request = urllib.request.Request(
        url.rstrip("/") + "/rpc", data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    # Loopback only: never route the call through a configured HTTP proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except Exception:  # noqa: BLE001 - a non-JSON error body is just a status
            raise HubUnavailableError(f"hub daemon answered HTTP {exc.code}") from exc
    if payload.get("ok"):
        return payload.get("result")
    error = payload.get("error") or {}
    message = error.get("message") or "hub daemon call failed"
    cls = _ERROR_TYPES.get(error.get("type") or "", HubUnavailableError)
    if cls is HubUnavailableError:
        raise HubUnavailableError(message, next_actions=error.get("next_actions"))
    raise cls(message)


# ------------------------------------------------------------------ daemon


class HubDaemon:
    """The hub owner. ``start``/``stop`` run it in-process (tests use that directly)."""

    def __init__(self, manager: HubManager | None = None, token: str | None = None,
                 idle_exit_sec: float = DEFAULT_IDLE_EXIT_SEC,
                 clock=time.time) -> None:
        self.manager = manager or HubManager()
        self.token = token or secrets.token_urlsafe(32)
        self.idle_exit_sec = idle_exit_sec
        self._clock = clock
        self.dashboard = HubDashboard(clock=clock)
        # Per-channel write locks: writes on different channels run concurrently.
        self.locks = ChannelLocks()
        self._meta_lock = threading.Lock()
        self._clients: dict[str, dict] = {}
        self._specs: dict[str, dict] = {}
        self._base_spec: dict = {}
        # client id -> channels that client switched off and nobody has put back.
        self._owed: dict[str, dict[str, set[int]]] = {}
        self._last_activity = clock()
        self.stopped = threading.Event()
        self.info: dict = {}
        # client id -> {"rows": [session rows], "at": last heartbeat}.
        self._sessions: dict[str, dict] = {}
        # Bumped on every applied switch. Clients compare it before serving a
        # cached detect_probe, so one server's switch invalidates everyone's cache.
        self.generation = 0
        # lease id -> {"client", "name", "token", "expires_at", "reason"}.
        self._leases: dict[str, dict] = {}
        self.manager.live_session_for = self._live_on

    # -------------------------------------------------------------- lifecycle

    def start(self, port: int = DEFAULT_PORT, interval_sec: float = DEFAULT_INTERVAL_SEC,
              base_spec: dict | None = None) -> dict:
        self._base_spec = dict(base_spec or {})
        self.manager.configure(self._merged_spec())
        status = self.dashboard.start(self.manager, sessions=self._session_rows, port=port,
                                      interval_sec=interval_sec, rpc=self.handle_rpc,
                                      extra_state=self._extra_state)
        self.info = {"pid": os.getpid(), "port": status["port"], "url": status["url"],
                     "token": self.token, "protocol": PROTOCOL, "version": __version__,
                     "started_at": round(self._clock(), 3)}
        return dict(self.info)

    def stop(self) -> dict:
        """Stop serving and put back everything any client still owes."""
        self.stopped.set()
        self.dashboard.stop()
        # Let an in-flight cycle finish its restore before closing, but do not
        # let a stuck one keep the daemon from ever shutting down.
        token = self.locks.acquire(ALL, {"client": "daemon", "method": "shutdown"}, timeout=10.0)
        try:
            return self.manager.close(restore_power=True, restore_data=True)
        finally:
            self.locks.release(token)

    def maintain(self) -> bool:
        """Periodic housekeeping. Returns True when the daemon should exit."""
        now = self._clock()
        with self._meta_lock:
            expired = [lid for lid, lease in self._leases.items() if lease["expires_at"] <= now]
        for lease_id in expired:
            self._drop_lease(lease_id)
        for client_id, meta in list(self._clients.items()):
            if meta.get("pid") and not _pid_alive(meta["pid"]):
                # Its MCP server died without saying goodbye; do what its own
                # shutdown would have done. A restore that cannot get its
                # channels yet keeps the debt, and the client entry, for the
                # next pass.
                self._drop_leases_of(client_id)
                result = self._release(client_id, restore_power=True, restore_data=True,
                                       reason="client exited", wait=0.5)
                if not result.get("deferred"):
                    self._forget(client_id)
        if not self.idle_exit_sec:
            return False
        last = max(self._last_activity, self.dashboard.last_viewed or 0)
        owes = bool(self.manager._we_turned_off or self.manager._we_disconnected)
        return not owes and self._clock() - last > self.idle_exit_sec

    # -------------------------------------------------------------------- rpc

    def handle_rpc(self, body: bytes, headers: Any) -> tuple[int, dict]:
        auth = (headers.get("Authorization") or "") if headers is not None else ""
        if not hmac.compare_digest(auth.encode("utf-8"), f"Bearer {self.token}".encode()):
            return 401, {"ok": False, "error": {"type": "HubUnavailableError",
                                                "message": "hub daemon: bad or missing token"}}
        try:
            request = json.loads(body.decode("utf-8") or "{}")
            if not isinstance(request, dict):
                raise ValueError("request must be a JSON object")
        except (UnicodeDecodeError, ValueError) as exc:
            return 400, {"ok": False, "error": {"type": "ValueError", "message": f"bad request: {exc}"}}

        if request.get("protocol") != PROTOCOL:
            return 409, {"ok": False, "error": {
                "type": "HubUnavailableError",
                "message": (f"hub daemon speaks protocol {PROTOCOL} (version {__version__}) but this "
                            f"client sent {request.get('protocol')!r}; it is probably left over from "
                            f"before an upgrade. Stop it: python -m mcp_server.hub_daemon stop"),
                "next_actions": ["python -m mcp_server.hub_daemon stop"]}}

        method = request.get("method")
        handler = getattr(self, f"_rpc_{method}", None) if isinstance(method, str) else None
        if handler is None:
            return 404, {"ok": False, "error": {"type": "ValueError", "message": f"unknown method {method!r}"}}
        client = self._touch_client(request.get("client") or {})
        params = request.get("params") or {}
        try:
            return 200, {"ok": True, "result": handler(client, params)}
        except tuple(_ERROR_TYPES.values()) as exc:
            error: dict[str, Any] = {"type": type(exc).__name__, "message": str(exc)}
            if isinstance(exc, HubUnavailableError) and exc.next_actions:
                error["next_actions"] = exc.next_actions
            return 200, {"ok": False, "error": error}
        except Exception as exc:  # noqa: BLE001 - never kill the daemon over one call
            return 200, {"ok": False, "error": {"type": "HubUnavailableError",
                                                "message": f"hub daemon error: {type(exc).__name__}: {exc}"}}

    def _touch_client(self, raw: dict) -> str:
        client_id = str(raw.get("id") or "anonymous")
        now = self._clock()
        self._last_activity = now
        with self._meta_lock:
            meta = self._clients.setdefault(client_id, {"id": client_id, "first_seen": round(now, 3), "calls": 0})
            pid = raw.get("pid")
            meta.update({"pid": pid if isinstance(pid, int) else None,
                         "name": str(raw.get("name") or client_id[:8]),
                         "last_seen": round(now, 3)})
            meta["calls"] += 1
        return client_id

    # Each _rpc_* takes (client_id, params) and returns a JSON-able result.

    def _rpc_ping(self, client: str, params: dict) -> dict:
        return {"pid": os.getpid(), "protocol": PROTOCOL, "version": __version__,
                "url": self.info.get("url"), "clients": len(self._clients)}

    def _rpc_configure(self, client: str, params: dict) -> dict:
        spec = params.get("spec")
        with self._meta_lock:
            self._specs[client] = dict(spec) if isinstance(spec, dict) else {}
        merged = self._merged_spec()
        current = self.manager.spec
        if all(merged.get(key) == current.get(key) for key in ("port", "exclude_ports")):
            # Labels only: nothing about the connection changes, so this must not
            # queue behind a running power cycle (every call pushes its spec first).
            self.manager.configure(merged)
            return {"configured": True}
        # A different hub port drops the connection: take EVERY channel, so it
        # never lands between some cycle's cut and its restore.
        token = self.locks.acquire(ALL, {"client": self._name(client), "method": "configure"},
                                   timeout=DEFAULT_MAX_WAIT_SEC)
        if token is None:
            raise HubBusyError(self._busy_message("configure", ALL, DEFAULT_MAX_WAIT_SEC))
        try:
            self.manager.configure(merged)
        finally:
            self.locks.release(token)
        return {"configured": True}

    def _rpc_channels(self, client: str, params: dict) -> list[int]:
        return list(self.manager.channels())

    def _rpc_available(self, client: str, params: dict) -> bool:
        return self.manager.is_available()

    def _rpc_describe(self, client: str, params: dict) -> dict:
        result = self.manager.describe()
        result["daemon"] = self._daemon_summary()
        return result

    def _rpc_measure(self, client: str, params: dict) -> dict:
        channels = params.get("channels")
        return self.manager.measure(
            tuple(channels) if channels else None,
            duration_sec=float(params.get("duration_sec", 0.0)),
            interval_sec=float(params.get("interval_sec", 0.05)))

    def _rpc_heartbeat(self, client: str, params: dict) -> dict:
        """Register this client's debug sessions (and prove it is alive)."""
        rows = []
        for raw in params.get("sessions") or []:
            if not isinstance(raw, dict):
                continue
            channel = raw.get("channel")
            rows.append({
                "session": str(raw.get("session") or "default"),
                "channel": channel if isinstance(channel, int) and not isinstance(channel, bool) else None,
                "channel_source": raw.get("channel_source"),
                "channel_error": raw.get("channel_error"),
                "server_alive": bool(raw.get("server_alive")),
                "probe_serial": raw.get("probe_serial"),
                "mcu": raw.get("mcu"),
            })
        with self._meta_lock:
            self._sessions[client] = {"rows": rows, "at": round(self._clock(), 3)}
        return {"generation": self.generation}

    def _rpc_generation(self, client: str, params: dict) -> dict:
        return {"generation": self.generation}

    def _rpc_live_sessions(self, client: str, params: dict) -> list[dict]:
        return [row for row in self._session_rows() if row["server_alive"]]

    def _rpc_power(self, client: str, params: dict) -> dict:
        return self._write(client, "power", params, _one(params), lambda: self.manager.power(
            params["channel"], params["state"], confirm=bool(params.get("confirm")),
            live_session=params.get("live_session")))

    def _rpc_data(self, client: str, params: dict) -> dict:
        # exclusive touches every channel's data line, so it locks them all.
        channels = ALL if params.get("exclusive") else _one(params)
        return self._write(client, "data", params, channels, lambda: self.manager.data(
            params["channel"], params["state"], confirm=bool(params.get("confirm")),
            live_session=params.get("live_session"), exclusive=bool(params.get("exclusive"))))

    def _rpc_power_cycle(self, client: str, params: dict) -> dict:
        return self._write(client, "power_cycle", params, _one(params), lambda: self.manager.power_cycle(
            params["channel"], off_ms=int(params.get("off_ms", 400)),
            settle_ms=int(params.get("settle_ms", 1500)), confirm=bool(params.get("confirm")),
            live_session=params.get("live_session")))

    def _rpc_lease_acquire(self, client: str, params: dict) -> dict:
        """Hold EVERY channel across several calls (discovery's toggle-and-look loop).

        Writes that carry the lease id run without taking channel locks; every
        other write waits behind it like behind any other holder, and is refused
        after its max_wait -- so discovery is never interleaved with a switch
        from another server.
        """
        ttl = min(max(float(params.get("ttl_sec", DEFAULT_LEASE_TTL_SEC)), 1.0), MAX_LEASE_TTL_SEC)
        max_wait = max(float(params.get("max_wait_sec", DEFAULT_MAX_WAIT_SEC)), 0.0)
        reason = str(params.get("reason") or "lease")
        name = self._name(client)
        token = self.locks.acquire(ALL, {"client": name, "method": reason, "lease": True,
                                         "since": round(self._clock(), 3)}, timeout=max_wait)
        if token is None:
            raise HubBusyError(self._busy_message(reason, ALL, max_wait))
        lease_id = secrets.token_hex(8)
        with self._meta_lock:
            self._leases[lease_id] = {"client": client, "name": name, "token": token, "reason": reason,
                                      "expires_at": self._clock() + ttl}
        return {"lease": lease_id, "ttl_sec": ttl}

    def _rpc_lease_release(self, client: str, params: dict) -> dict:
        lease_id = str(params.get("lease") or "")
        with self._meta_lock:
            lease = self._leases.get(lease_id)
            if lease is None or lease["client"] != client:
                return {"released": False}
        self._drop_lease(lease_id)
        return {"released": True}

    def _rpc_release(self, client: str, params: dict) -> dict:
        self._drop_leases_of(client)
        result = self._release(client, restore_power=bool(params.get("restore_power", True)),
                               restore_data=bool(params.get("restore_data", True)), reason="client closed",
                               wait=5.0)
        if not result.get("deferred"):
            self._forget(client)
        return result

    def _rpc_shutdown(self, client: str, params: dict) -> dict:
        self.stopped.set()
        return {"stopping": True}

    # ------------------------------------------------------------- internals

    def _write(self, client: str, method: str, params: dict, channels, action) -> dict:
        """Run one mutating call holding only ``channels``, as the calling client.

        Writes on other channels run concurrently, so the caller's guard mode and
        audit attribution are thread-local (``HubPowerGuard.acting_as``), never a
        swap of the shared guard. Everything the guard looks at -- including who
        is live on the channel -- is read once the channels are held, so a write
        that queued is judged on the hub as it is now, not as it was when sent.
        """
        mode = params.get("guard_mode")
        if mode is not None and mode not in GUARD_MODES:
            raise ValueError(f"guard_mode must be one of: {', '.join(GUARD_MODES)}")
        max_wait = max(float(params.get("max_wait_sec", DEFAULT_MAX_WAIT_SEC)), 0.0)
        name = self._name(client)
        token = None
        waited_ms = 0
        if not self._holds_lease(client, params.get("lease")):
            queued_at = time.monotonic()
            token = self.locks.acquire(channels, {"client": name, "method": method,
                                                  "since": round(self._clock(), 3)}, timeout=max_wait)
            if token is None:
                raise HubBusyError(self._busy_message(method, channels, max_wait))
            waited_ms = int(round((time.monotonic() - queued_at) * 1000))
        try:
            with self.manager.guard.acting_as(mode=mode, client=name):
                result = action()
            self._account(client, result)
            if result.get("applied") is True or result.get("action") == "power_cycle":
                self._bump()
        finally:
            self.locks.release(token)
        if waited_ms >= 50:
            result["queued_ms"] = waited_ms
        return result

    def _busy_message(self, method: str, channels, waited: float) -> str:
        holders = self.locks.holders(channels)
        if holders:
            described = []
            for row in holders[:3]:
                chans = "every channel" if row["channels"] == "all" else \
                    "channel " + ", ".join(str(ch) for ch in row["channels"])
                described.append(f"{row['client']}'s {row['method']} on {chans} "
                                 f"(running {row['running_sec']:.1f}s)")
            holder = "; ".join(described)
        else:
            holder = "earlier queued writes"
        on = " on every channel" if channels is ALL else \
            " on channel " + ", ".join(str(ch) for ch in sorted(channels))
        return (f"hub busy: {holder} held what this {method}{on} needs for the whole "
                f"{waited:.0f}s it waited, so it was NOT executed -- a cut decided that long ago "
                f"may no longer be wanted. Check hub(action=describe) and retry if it still is.")

    def _holds_lease(self, client: str, lease_id: Any) -> bool:
        if not lease_id:
            return False
        with self._meta_lock:
            lease = self._leases.get(str(lease_id))
            return bool(lease and lease["client"] == client and lease["expires_at"] > self._clock())

    def _drop_lease(self, lease_id: str) -> None:
        with self._meta_lock:
            lease = self._leases.pop(lease_id, None)
        if lease is not None:
            self.locks.release(lease["token"])

    def _drop_leases_of(self, client: str) -> None:
        with self._meta_lock:
            ids = [lid for lid, lease in self._leases.items() if lease["client"] == client]
        for lease_id in ids:
            self._drop_lease(lease_id)

    def _bump(self) -> None:
        with self._meta_lock:
            self.generation += 1

    def _name(self, client: str) -> str:
        return str(self._clients.get(client, {}).get("name") or client)

    def _live_on(self, channel: int) -> str | None:
        """Who, on any connected server, has a live GDB server on ``channel``."""
        for row in self._session_rows():
            if row["server_alive"] and row["channel"] == channel:
                return f"{row['session']} @ {row['client']}"
        return None

    def _session_rows(self) -> list[dict]:
        """Every connected server's registered sessions, newest server first."""
        now = self._clock()
        rows = []
        with self._meta_lock:
            entries = [(cid, dict(entry), dict(self._clients.get(cid) or {}))
                       for cid, entry in self._sessions.items()]
        for client_id, entry, meta in sorted(entries, key=lambda e: -(e[1].get("at") or 0)):
            if meta.get("pid") and not _pid_alive(meta["pid"]):
                continue  # its GDB servers died with it (job object / PDEATHSIG)
            stale = now - (entry.get("at") or 0) > HEARTBEAT_STALE_SEC
            for row in entry["rows"]:
                rows.append({**row, "client": meta.get("name") or client_id[:8], "client_id": client_id,
                             "stale": stale})
        return rows

    def _forget(self, client: str) -> None:
        with self._meta_lock:
            self._clients.pop(client, None)
            self._specs.pop(client, None)
            self._sessions.pop(client, None)

    def _account(self, client: str, result: dict) -> None:
        if result.get("applied") is not True:
            return
        with self._meta_lock:
            self._account_locked(client, result)

    def _account_locked(self, client: str, result: dict) -> None:
        owed = self._owed.setdefault(client, {"power": set(), "data": set()})
        action, channel = result.get("action"), result.get("channel")
        if not isinstance(channel, int):
            return
        if action == "power_off":
            owed["power"].add(channel)
        elif action == "data_off":
            owed["data"].add(channel)
        elif action in ("power_on", "data_on"):
            # Whoever turns a channel back on settles the debt, whoever owed it.
            kind = action.split("_")[0]
            for other in self._owed.values():
                other[kind].discard(channel)
        for off in result.get("data_off_channels") or ():
            owed["data"].add(off)

    def _release(self, client: str, restore_power: bool, restore_data: bool, reason: str,
                 wait: float = DEFAULT_MAX_WAIT_SEC) -> dict:
        """Put back what ``client`` switched off, holding just those channels.

        If they cannot be had within ``wait`` (someone is mid-cycle on one), the
        debt is kept and the result says ``deferred`` -- dropping it would leave
        a board dark with nobody left who knows.
        """
        restored: list[int] = []
        reconnected: list[int] = []
        failed: list[dict] = []
        with self._meta_lock:
            owed = self._owed.pop(client, {"power": set(), "data": set()})
        wanted = {kind: set(owed[kind]) if enabled else set()
                  for kind, enabled in (("power", restore_power), ("data", restore_data))}
        channels = wanted["power"] | wanted["data"]
        if not channels:
            return {"restored_channels": restored, "reconnected_channels": reconnected, "failed": failed}
        name = self._name(client)
        token = self.locks.acquire(channels, {"client": name, "method": f"restore ({reason})",
                                              "since": round(self._clock(), 3)}, timeout=wait)
        if token is None:
            with self._meta_lock:
                kept = self._owed.setdefault(client, {"power": set(), "data": set()})
                kept["power"] |= owed["power"]
                kept["data"] |= owed["data"]
            return {"restored_channels": [], "reconnected_channels": [], "failed": [],
                    "deferred": self._busy_message("restore", channels, wait)}
        try:
            with self.manager.guard.acting_as(client=f"{name} ({reason})"):
                for kind, done, still_off in (("power", restored, self.manager._we_turned_off),
                                              ("data", reconnected, self.manager._we_disconnected)):
                    for channel in sorted(wanted[kind]):
                        if channel not in still_off:
                            continue
                        try:
                            switch = self.manager.power if kind == "power" else self.manager.data
                            switch(channel, "on", confirm=True)
                            done.append(channel)
                        except Exception as exc:  # noqa: BLE001 - restore every channel we can
                            failed.append({"channel": channel, "kind": kind, "reason": str(exc)})
            if restored or reconnected:
                self._bump()
        finally:
            self.locks.release(token)
        return {"restored_channels": restored, "reconnected_channels": reconnected, "failed": failed}

    def _merged_spec(self) -> dict:
        """The daemon's own view: hub port from the latest client, labels from all.

        ``guard`` is deliberately dropped -- each write carries its caller's
        mode -- and a map entry keeps every client's label so the dashboard can
        name every board on the rack.
        """
        merged = {k: v for k, v in self._base_spec.items() if k != "guard"}
        base_map = {str(k): dict(v) if isinstance(v, dict) else {"label": v}
                    for k, v in (self._base_spec.get("map") or {}).items()}
        with self._meta_lock:
            specs = list(self._specs.values())
        for spec in specs:
            for key in ("port", "exclude_ports"):
                if spec.get(key):
                    merged[key] = spec[key]
            for channel, entry in (spec.get("map") or {}).items():
                value = dict(entry) if isinstance(entry, dict) else {"label": entry}
                base_map.setdefault(str(channel), {}).update(value)
        if base_map:
            merged["map"] = base_map
        return merged

    def _daemon_summary(self) -> dict:
        return {"pid": os.getpid(), "url": self.info.get("url"), "version": __version__,
                "clients": len(self._clients)}

    def _extra_state(self) -> dict:
        with self._meta_lock:
            clients = []
            for meta in self._clients.values():
                row = dict(meta)
                owed = self._owed.get(meta["id"]) or {}
                row["owes_power"] = sorted(owed.get("power") or ())
                row["owes_data"] = sorted(owed.get("data") or ())
                clients.append(row)
        for row in clients:
            row["heartbeat_at"] = (self._sessions.get(row["id"]) or {}).get("at")
        return {"mode": "daemon", "daemon": self._daemon_summary(), "clients": clients,
                "inflight": self.locks.holders(), "waiting": self.locks.waiting(),
                "generation": self.generation}


def _one(params: dict):
    """The single channel a call targets, as a lock set (ALL if it is not an int)."""
    channel = params.get("channel")
    return {channel} if isinstance(channel, int) and not isinstance(channel, bool) else ALL


# ------------------------------------------------------------------ cli


def serve(args: argparse.Namespace) -> int:
    _ensure_dir()
    if not _acquire_instance_lock():
        print("hub daemon already running", file=sys.stderr)
        return 0
    try:
        base_spec: dict = {}
        if args.config:
            import yaml
            with open(args.config, encoding="utf-8") as handle:
                loaded = yaml.safe_load(handle) or {}
            base_spec = dict(loaded.get("hub") or {}) if isinstance(loaded, dict) else {}
        if args.hub_port:
            base_spec["port"] = args.hub_port

        daemon = HubDaemon(idle_exit_sec=args.idle_exit)
        info = daemon.start(port=args.port, interval_sec=args.interval, base_spec=base_spec)
        _write_private(state_path(), info)
        print(f"hub daemon pid {info['pid']} serving {info['url']}", file=sys.stderr, flush=True)

        def _on_signal(signum, frame):
            daemon.stopped.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _on_signal)
            except (ValueError, OSError):
                pass

        while not daemon.stopped.wait(MAINTENANCE_SEC):
            if daemon.maintain():
                print("hub daemon idle; exiting", file=sys.stderr, flush=True)
                break
        restored = daemon.stop()
        print(f"hub daemon stopped; restored {restored}", file=sys.stderr, flush=True)
        return 0
    finally:
        state = None
        try:
            with open(state_path(), encoding="utf-8") as handle:
                state = json.load(handle)
        except (OSError, json.JSONDecodeError):
            pass
        if isinstance(state, dict) and state.get("pid") == os.getpid():
            _remove(state_path())
        _remove(_lock_path())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Single owner of the programmable USB hub.")
    sub = parser.add_subparsers(dest="command")
    run = sub.add_parser("serve", help="Run the daemon in the foreground (default).")
    run.add_argument("--config", help="YAML file with a 'hub:' block (port, map labels).")
    run.add_argument("--hub-port", help="Hub control port, e.g. COM7. Omit to auto-scan.")
    run.add_argument("--port", type=int, default=DEFAULT_PORT, help="HTTP port on 127.0.0.1 (default 8765).")
    run.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SEC, help="Dashboard sample interval.")
    run.add_argument("--idle-exit", type=float, default=DEFAULT_IDLE_EXIT_SEC,
                     help="Exit after this many idle seconds (0 = never).")
    sub.add_parser("status", help="Print the running daemon's URL and clients.")
    sub.add_parser("stop", help="Stop the running daemon (restores what clients switched off).")
    args = parser.parse_args(argv)

    if args.command in (None, "serve"):
        if args.command is None:
            args = run.parse_args([])
        return serve(args)

    state = read_state()
    if state is None:
        print("hub daemon is not running")
        return 1
    if args.command == "status":
        try:
            ping = rpc_call(state["url"], state["token"], "ping", timeout=5)
        except (OSError, HubUnavailableError) as exc:
            print(f"hub daemon pid {state['pid']} at {state['url']} is not answering: {exc}")
            return 1
        print(f"hub daemon pid {ping['pid']} v{ping['version']} at {state['url']} "
              f"({ping['clients']} client(s))")
        return 0
    rpc_call(state["url"], state["token"], "shutdown", timeout=5)
    print(f"hub daemon pid {state['pid']} stopping")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
