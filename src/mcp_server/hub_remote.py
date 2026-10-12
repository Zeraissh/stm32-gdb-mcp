"""The MCP server's side of the shared hub daemon.

``RemoteHubManager`` has ``HubManager``'s public surface, so every hub tool,
``reset_target(strategy="cold")`` and the recovery ladder work unchanged; the
calls are forwarded to the one process that owns the hub (``hub_daemon``). What
stays local is what is per-session anyway: channel resolution against the
session's profile, and the guard MODE, which travels with each write.

``HubManagerSelector`` is what ``server.hub_manager`` points at. It decides once,
on first hub use, from ``STM32_GDB_MCP_HUB_MODE``:

- ``auto`` (default): use the daemon, spawning it if none is running; fall back
  to the in-process manager when the ``[hub]`` extra is absent or the daemon
  cannot be started.
- ``daemon``: always the daemon; failures surface as ``hub_unavailable``.
- ``local``: the in-process manager, exactly as before the daemon existed.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import threading
import time
import urllib.error
import uuid
from collections.abc import Callable
from typing import Any

from . import hub as hub_module
from .hub import HubManager, HubUnavailableError, _normalize_map
from .hub_daemon import _ensure_dir, log_path, read_state, rpc_call, state_dir

MODE_ENV = "STM32_GDB_MCP_HUB_MODE"
MODES = ("auto", "daemon", "local")
SPAWN_TIMEOUT_SEC = 10.0


def hub_mode() -> str:
    mode = (os.environ.get(MODE_ENV) or "auto").strip().lower()
    return mode if mode in MODES else "auto"


# ------------------------------------------------------------------ connect


def spawn_daemon() -> subprocess.Popen:
    """Start ``python -m mcp_server.hub_daemon serve`` detached from this server.

    Detached because the daemon is shared: when the server that happened to
    spawn it exits, the others on the rack are still using it. On Windows that
    means leaving this server's kill-on-close job object (see process_guard).
    """
    _ensure_dir()
    env = dict(os.environ)
    package_root = os.path.dirname(os.path.dirname(os.path.abspath(hub_module.__file__)))
    env["PYTHONPATH"] = os.pathsep.join(p for p in (package_root, env.get("PYTHONPATH")) if p)
    log = open(log_path(), "ab")  # noqa: SIM115 - handed to the child, closed below
    kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": log,
                              "cwd": state_dir(), "env": env, "close_fds": True}
    cmd = [sys.executable, "-m", "mcp_server.hub_daemon", "serve"]
    try:
        if sys.platform == "win32":
            base = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            try:
                return subprocess.Popen(cmd, creationflags=base | 0x01000000, **kwargs)  # BREAKAWAY_FROM_JOB
            except OSError:
                # An outer job (some IDEs run servers in one) can forbid breakaway.
                # The daemon then dies with this server; the next server respawns it.
                return subprocess.Popen(cmd, creationflags=base, **kwargs)
        return subprocess.Popen(cmd, start_new_session=True, **kwargs)
    finally:
        log.close()


def connect(spawn: bool = True, timeout: float = SPAWN_TIMEOUT_SEC) -> tuple[str, str]:
    """(url, token) of a live daemon, spawning one when allowed and needed."""
    deadline = time.monotonic() + timeout
    spawned = False
    while True:
        state = read_state()
        if state is not None:
            try:
                rpc_call(state["url"], state["token"], "ping", timeout=2)
                return state["url"], state["token"]
            except OSError:
                pass  # still starting, or dying; keep polling until the deadline
        elif spawn and not spawned:
            spawn_daemon()
            spawned = True
        if not spawn and state is None:
            raise HubUnavailableError("hub daemon is not running")
        if time.monotonic() >= deadline:
            raise HubUnavailableError(
                f"hub daemon did not come up within {timeout:.0f}s; its log is {log_path()}. "
                f"Run it in the foreground to see why: python -m mcp_server.hub_daemon serve",
                next_actions=["hub(action=describe)"])
        time.sleep(0.1)


def _refused(exc: OSError) -> bool:
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    return isinstance(reason, ConnectionRefusedError)


# ------------------------------------------------------------------ manager


class RemoteHubManager(HubManager):
    """``HubManager`` whose hardware lives in the hub daemon."""

    def __init__(self, connector: Callable[..., tuple[str, str]] | None = None,
                 client_name: str | None = None) -> None:
        super().__init__()
        self._idle_release_sec = 0  # the daemon owns the port; nothing to release here
        self._connector = connector
        self._endpoint: tuple[str, str] | None = None
        self._pushed_spec: dict | None = None
        self.client_id = uuid.uuid4().hex
        self.client_name = client_name or f"{os.path.basename(os.getcwd()) or 'mcp'} (pid {os.getpid()})"

    # ------------------------------------------------------------ transport

    def _client(self) -> dict:
        return {"id": self.client_id, "pid": os.getpid(), "name": self.client_name}

    def _call(self, method: str, params: dict | None = None, timeout: float = 30.0,
              spawn: bool = True, push_spec: bool = True) -> Any:
        for attempt in (1, 2):
            if self._endpoint is None:
                # Module lookup at call time, so a patched ``connect`` is honoured.
                self._endpoint = (self._connector or connect)(spawn=spawn)
                self._pushed_spec = None  # a new daemon knows nothing about us
            url, token = self._endpoint
            try:
                if push_spec and self._pushed_spec != self._spec:
                    spec = {k: v for k, v in self._spec.items() if k != "guard"}
                    rpc_call(url, token, "configure", {"spec": spec}, client=self._client(), timeout=10)
                    self._pushed_spec = dict(self._spec)
                return rpc_call(url, token, method, params, client=self._client(), timeout=timeout)
            except OSError as exc:
                self._endpoint = None
                # Retry only when the connection was REFUSED: the request never
                # reached a daemon, so re-sending a write cannot double it. A reset
                # mid-call (daemon died during a power cycle) is not retried.
                if attempt == 1 and spawn and _refused(exc):
                    continue
                raise HubUnavailableError(f"hub daemon unreachable: {exc}") from exc
        raise AssertionError("unreachable")

    def _ensure(self):
        raise HubUnavailableError("internal: RemoteHubManager has no local hub connection")

    # ------------------------------------------------------------ surface

    def configure(self, spec: dict | None) -> dict:
        # Local only, never a network call: configure() runs on every hub tool
        # call and on reset/recover paths that must not block on the daemon. The
        # spec is pushed lazily, ahead of the next real call, when it changed.
        return super().configure(spec)

    def is_available(self) -> bool:
        try:
            return bool(self._call("available"))
        except Exception:  # noqa: BLE001 - availability is a question, not an assertion
            return False

    def channels(self) -> tuple[int, ...]:
        self._channels = tuple(self._call("channels"))
        return self._channels

    def describe(self) -> dict:
        result = self._call("describe")
        self._channels = tuple(result.get("channels") or ())
        # Annotate with THIS session's map, not the daemon's merged one, so
        # describe reads exactly as it did in-process.
        entries = _normalize_map(self._spec.get("map"))
        for port in result.get("ports") or []:
            port.pop("mapped", None)
            mapped = entries.get(port.get("channel"))
            if mapped:
                port["mapped"] = mapped
        result["configured_channel"] = self._spec.get("channel")
        result["guard"] = self.guard.policy()
        result["mode"] = "daemon"
        return result

    def power(self, channel: int, state: str, confirm: bool = False,
              live_session: str | None = None) -> dict:
        return self._write("power", {"channel": channel, "state": state, "confirm": confirm,
                                     "live_session": live_session})

    def data(self, channel: int, state: str, confirm: bool = False,
             live_session: str | None = None, exclusive: bool = False) -> dict:
        return self._write("data", {"channel": channel, "state": state, "confirm": confirm,
                                    "live_session": live_session, "exclusive": exclusive})

    def power_cycle(self, channel: int, off_ms: int = 400, settle_ms: int = 1500,
                    confirm: bool = False, live_session: str | None = None,
                    sleep=None, monotonic=None) -> dict:
        return self._write("power_cycle", {"channel": channel, "off_ms": off_ms, "settle_ms": settle_ms,
                                           "confirm": confirm, "live_session": live_session},
                           timeout=30.0 + (off_ms + settle_ms) / 1000.0)

    def measure(self, channels: tuple[int, ...] | None = None, duration_sec: float = 0.0,
                interval_sec: float = 0.05, sleep=None, monotonic=None) -> dict:
        return self._call("measure", {"channels": list(channels) if channels else None,
                                      "duration_sec": duration_sec, "interval_sec": interval_sec},
                          timeout=30.0 + max(duration_sec, 0.0) * 1.5)

    def _write(self, method: str, params: dict, timeout: float = 30.0) -> dict:
        params["guard_mode"] = self.guard.mode
        try:
            return self._call(method, params, timeout=timeout)
        finally:
            # The daemon invalidated ITS probe cache; this process has its own,
            # and detect_probe here is how the caller confirms the switch.
            from .openocd_config import invalidate_probe_cache
            invalidate_probe_cache()

    def close(self, restore_power: bool = True, restore_data: bool = True) -> dict:
        """Hand back what this server switched off; the daemon keeps running."""
        if self._endpoint is None:
            return {"restored_channels": [], "reconnected_channels": []}
        try:
            result = self._call("release", {"restore_power": restore_power, "restore_data": restore_data},
                                timeout=10.0, spawn=False, push_spec=False)
        except Exception:  # noqa: BLE001 - shutdown must not raise
            result = {"restored_channels": [], "reconnected_channels": []}
        self._endpoint = None
        return result

    def dashboard_info(self) -> dict:
        ping = self._call("ping")
        return {"running": True, "url": ping.get("url"), "mode": "daemon",
                "daemon_pid": ping.get("pid"), "clients": ping.get("clients"),
                "note": ("This is the shared hub daemon's dashboard: it shows every MCP server's "
                         "view of the hub and stays up while the daemon runs "
                         "(python -m mcp_server.hub_daemon stop to end it).")}


# ------------------------------------------------------------------ selector


class HubManagerSelector:
    """Picks local vs daemon on first use and forwards everything to the pick."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._mode: str | None = None
        self._remote: RemoteHubManager | None = None
        self.fallback_reason: str | None = None

    @property
    def selected_mode(self) -> str | None:
        return self._mode

    def resolve(self) -> Any:
        with self._lock:
            if self._mode is None:
                self._decide()
        if self._mode == "local":
            # Looked up on every call so tests patching the module global still work.
            return hub_module.hub_manager
        return self._remote

    def _decide(self) -> None:
        requested = hub_mode()
        if requested == "local":
            self._mode = "local"
            return
        if requested == "auto" and importlib.util.find_spec("smartusbhub") is None:
            # No extra means no hub to share; the local manager carries the
            # install hint, and nobody gets a daemon they cannot use.
            self._mode = "local"
            self.fallback_reason = "smartusbhub is not installed"
            return
        remote = RemoteHubManager()
        if requested == "auto":
            try:
                remote._endpoint = connect()
            except Exception as exc:  # noqa: BLE001 - auto falls back, daemon mode does not
                self._mode = "local"
                self.fallback_reason = f"hub daemon unavailable: {exc}"
                return
        self._remote = remote
        self._mode = "daemon"

    def __getattr__(self, name: str) -> Any:
        return getattr(self.resolve(), name)

    def close(self, restore_power: bool = True, restore_data: bool = True) -> dict:
        # Never spawn a daemon just to say goodbye to it.
        if self._mode is None:
            return {"restored_channels": [], "reconnected_channels": []}
        return self.resolve().close(restore_power=restore_power, restore_data=restore_data)


hub_manager = HubManagerSelector()
