"""One process owns the hub; every MCP server talks to it.

The hub's USB-CDC control port opens in exactly one process at a time, so with
several MCP servers on a rack (one IDE window per board) the first server to
touch the hub locked every other one out, and an in-process dashboard could only
ever show its own server's view. This daemon is the single owner instead:

- **Reads** go through one ``HubManager``. The dashboard sampler publishes one
  snapshot per interval to every viewer, and a client's ``describe`` is a fresh
  read (a cached one could show "on" right after that client switched a port off).
- **Writes** are serialized by one daemon-wide lock, so a power cycle (cut,
  wait, restore, settle) can never interleave with another client's switch.
  The guard decides with the CALLING client's guard mode, so one server set to
  ``allow`` for CI does not relax the guard for every other server on the rack.
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
from .probe_lock import _pid_alive

# Bumped whenever an RPC's shape changes. A client refuses a daemon speaking a
# different protocol rather than half-working against it (the usual cause is a
# daemon still running from before an upgrade).
PROTOCOL = 1

# With nobody calling and nobody watching for this long, the daemon exits and
# hands the hub back -- unless it still owes a channel its power or data line.
DEFAULT_IDLE_EXIT_SEC = 600.0
MAINTENANCE_SEC = 2.0

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
        self._write_lock = threading.RLock()
        self._meta_lock = threading.Lock()
        self._clients: dict[str, dict] = {}
        self._specs: dict[str, dict] = {}
        self._base_spec: dict = {}
        # client id -> channels that client switched off and nobody has put back.
        self._owed: dict[str, dict[str, set[int]]] = {}
        self._last_activity = clock()
        self.stopped = threading.Event()
        self.info: dict = {}

    # -------------------------------------------------------------- lifecycle

    def start(self, port: int = DEFAULT_PORT, interval_sec: float = DEFAULT_INTERVAL_SEC,
              base_spec: dict | None = None) -> dict:
        self._base_spec = dict(base_spec or {})
        self.manager.configure(self._merged_spec())
        status = self.dashboard.start(self.manager, port=port, interval_sec=interval_sec,
                                      rpc=self.handle_rpc, extra_state=self._extra_state)
        self.info = {"pid": os.getpid(), "port": status["port"], "url": status["url"],
                     "token": self.token, "protocol": PROTOCOL, "version": __version__,
                     "started_at": round(self._clock(), 3)}
        return dict(self.info)

    def stop(self) -> dict:
        """Stop serving and put back everything any client still owes."""
        self.stopped.set()
        self.dashboard.stop()
        with self._write_lock:
            restored = self.manager.close(restore_power=True, restore_data=True)
        return restored

    def maintain(self) -> bool:
        """Periodic housekeeping. Returns True when the daemon should exit."""
        for client_id, meta in list(self._clients.items()):
            if meta.get("pid") and not _pid_alive(meta["pid"]):
                # Its MCP server died without saying goodbye; do what its own
                # shutdown would have done.
                self._release(client_id, restore_power=True, restore_data=True, reason="client exited")
                with self._meta_lock:
                    self._clients.pop(client_id, None)
                    self._specs.pop(client_id, None)
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
        with self._write_lock:
            self.manager.configure(self._merged_spec())
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

    def _rpc_power(self, client: str, params: dict) -> dict:
        return self._write(client, params, lambda: self.manager.power(
            params["channel"], params["state"], confirm=bool(params.get("confirm")),
            live_session=params.get("live_session")))

    def _rpc_data(self, client: str, params: dict) -> dict:
        return self._write(client, params, lambda: self.manager.data(
            params["channel"], params["state"], confirm=bool(params.get("confirm")),
            live_session=params.get("live_session"), exclusive=bool(params.get("exclusive"))))

    def _rpc_power_cycle(self, client: str, params: dict) -> dict:
        return self._write(client, params, lambda: self.manager.power_cycle(
            params["channel"], off_ms=int(params.get("off_ms", 400)),
            settle_ms=int(params.get("settle_ms", 1500)), confirm=bool(params.get("confirm")),
            live_session=params.get("live_session")))

    def _rpc_release(self, client: str, params: dict) -> dict:
        result = self._release(client, restore_power=bool(params.get("restore_power", True)),
                               restore_data=bool(params.get("restore_data", True)), reason="client closed")
        with self._meta_lock:
            self._clients.pop(client, None)
            self._specs.pop(client, None)
        return result

    def _rpc_shutdown(self, client: str, params: dict) -> dict:
        self.stopped.set()
        return {"stopping": True}

    # ------------------------------------------------------------- internals

    def _write(self, client: str, params: dict, action) -> dict:
        """Run one mutating call under the daemon-wide lock, as the calling client.

        The guard mode is the CALLER's, swapped in for this call only; the lock
        makes the swap invisible to everyone else. New audit entries are tagged
        with the client so the shared trail says who did what.
        """
        mode = params.get("guard_mode")
        if mode is not None and mode not in GUARD_MODES:
            raise ValueError(f"guard_mode must be one of: {', '.join(GUARD_MODES)}")
        with self._write_lock:
            guard = self.manager.guard
            previous = guard.mode
            seen = len(guard.audit_log)
            if mode is not None:
                guard.mode = mode
            try:
                result = action()
            finally:
                guard.mode = previous
                name = self._clients.get(client, {}).get("name", client)
                for entry in guard.audit_log[seen:]:
                    entry.setdefault("client", name)
            self._account(client, result)
        return result

    def _account(self, client: str, result: dict) -> None:
        if result.get("applied") is not True:
            return
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

    def _release(self, client: str, restore_power: bool, restore_data: bool, reason: str) -> dict:
        restored: list[int] = []
        reconnected: list[int] = []
        failed: list[dict] = []
        with self._write_lock:
            owed = self._owed.pop(client, {"power": set(), "data": set()})
            seen = len(self.manager.guard.audit_log)
            for kind, enabled, done, still_off in (
                    ("power", restore_power, restored, self.manager._we_turned_off),
                    ("data", restore_data, reconnected, self.manager._we_disconnected)):
                if not enabled:
                    continue
                for channel in sorted(owed[kind]):
                    if channel not in still_off:
                        continue
                    try:
                        switch = self.manager.power if kind == "power" else self.manager.data
                        switch(channel, "on", confirm=True)
                        done.append(channel)
                    except Exception as exc:  # noqa: BLE001 - restore every channel we can
                        failed.append({"channel": channel, "kind": kind, "reason": str(exc)})
            for entry in self.manager.guard.audit_log[seen:]:
                entry.setdefault("client", f"{self._clients.get(client, {}).get('name', client)} ({reason})")
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
        return {"mode": "daemon", "daemon": self._daemon_summary(), "clients": clients}


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
