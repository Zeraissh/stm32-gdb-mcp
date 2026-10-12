"""Live, read-only web dashboard for the programmable USB hub.

``hub(action=describe)`` answers "what is the hub doing NOW" once per call, which
is the wrong shape for a human watching a rack: they want every port's power,
data line, voltage and current on screen at once, with a little history, while
an agent is busy cycling boards. This module serves that page.

It runs INSIDE the MCP server process on purpose. The hub's USB-CDC control
port is exclusive across processes (see ``HubManager``'s idle-release notes), so
a separate dashboard process would either lock the agent out of the hub or be
locked out itself. Sharing the one ``HubManager`` sidesteps that entirely, and
its RLock already serializes the dashboard's reads against the agent's writes.

One sampler thread is the only thing that touches the hub; the HTTP side only
reads the sampler's cache. However many browser tabs are open, the hub sees one
``describe()`` per interval. The page is strictly read-only: every mutating hub
action stays behind the tool layer's guard and audit trail.

While the dashboard runs, the hub connection is touched every interval, so the
30 s idle release never fires and another PROCESS cannot take the hub. Stop the
dashboard (``hub(action=dashboard, state=off)``) to hand the port back.

Standalone, when no MCP server is running::

    python -m mcp_server.hub_dashboard --config bench_hub.yaml
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from collections import deque
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any

DEFAULT_PORT = 8765
DEFAULT_INTERVAL_SEC = 1.0
MIN_INTERVAL_SEC = 0.2
# 15 minutes at the default interval; the page draws the whole window.
DEFAULT_HISTORY = 900
MAX_EVENTS = 200

# A dashboard is a local tool. Binding anything but loopback would publish the
# rack's state to the network, and the Host check below keeps a malicious web
# page from reading it through DNS rebinding.
_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
_MAX_RPC_BODY = 1 << 20


def _load_page() -> bytes:
    return resources.files("mcp_server").joinpath("hub_dashboard.html").read_bytes()


class HubDashboard:
    """Sampler + HTTP server. ``start``/``stop`` are idempotent and thread-safe."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._manager: Any = None
        self._sessions: Callable[[], list[dict]] = list
        self._extra_state: Callable[[], dict] | None = None
        self.last_viewed: float | None = None
        self._interval = DEFAULT_INTERVAL_SEC
        self._history_len = DEFAULT_HISTORY
        self._httpd: ThreadingHTTPServer | None = None
        self._http_thread: threading.Thread | None = None
        self._sampler: threading.Thread | None = None
        self._stop = threading.Event()
        self._requested_port: int | None = None
        self._reset_state()

    def _reset_state(self) -> None:
        self._latest: dict | None = None
        self._error: str | None = None
        self._last_ok: float | None = None
        self._last_attempt: float | None = None
        self._history: dict[int, deque] = {}
        self._events: deque = deque(maxlen=MAX_EVENTS)
        self._sessions_view: list[dict] = []
        self._prev_ports: dict[int, dict] = {}
        self._audit_seen = 0
        self._started_at: float | None = None

    # ------------------------------------------------------------ lifecycle

    @property
    def running(self) -> bool:
        return self._httpd is not None

    def start(self, manager: Any, sessions: Callable[[], list[dict]] | None = None,
              host: str = "127.0.0.1", port: int = DEFAULT_PORT,
              interval_sec: float = DEFAULT_INTERVAL_SEC,
              history: int = DEFAULT_HISTORY,
              rpc: Callable[[bytes, Any], tuple[int, dict]] | None = None,
              extra_state: Callable[[], dict] | None = None) -> dict:
        """Start serving, or re-point a running dashboard at new settings.

        A running dashboard keeps its URL (and the history it has gathered) when
        only the interval or providers change; asking for a different port
        restarts it on that port.
        """
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("the hub dashboard only binds to loopback (127.0.0.1)")
        interval = max(float(interval_sec), MIN_INTERVAL_SEC)
        with self._lock:
            self._manager = manager
            self._sessions = sessions or list
            self._extra_state = extra_state
            self._interval = interval
            if self._httpd is not None and port in (0, self._requested_port, self._httpd.server_address[1]):
                return self.status()
        if self._httpd is not None:
            self.stop()
        with self._lock:
            self._history_len = max(int(history), 10)
            self._reset_state()
            self._httpd = self._bind(host, port)
            self._httpd.dashboard = self  # type: ignore[attr-defined]
            # POST /rpc is mounted only by the hub daemon; a plain dashboard has
            # no write path at all.
            self._httpd.rpc = rpc  # type: ignore[attr-defined]
            self._requested_port = port
            self._started_at = self._clock()
            self._stop = threading.Event()
            self._http_thread = threading.Thread(
                target=self._httpd.serve_forever, name="hub-dashboard-http", daemon=True)
            self._http_thread.start()
            self._sampler = threading.Thread(target=self._run, name="hub-dashboard-sampler", daemon=True)
            self._sampler.start()
            return self.status()

    @staticmethod
    def _bind(host: str, port: int) -> ThreadingHTTPServer:
        try:
            httpd = ThreadingHTTPServer((host, port), _Handler)
        except OSError:
            if port != DEFAULT_PORT:
                raise
            # The default port is a convenience, not a contract: something else on
            # the bench owning 8765 must not make the dashboard unusable.
            httpd = ThreadingHTTPServer((host, 0), _Handler)
        httpd.daemon_threads = True
        return httpd

    def stop(self) -> dict:
        # Detach under the lock, tear down outside it: the sampler takes this lock
        # to publish a sample, so joining it while holding the lock would stall.
        with self._lock:
            self._stop.set()
            httpd, self._httpd = self._httpd, None
            sampler, self._sampler = self._sampler, None
            self._http_thread = None
            self._requested_port = None
        if httpd is not None:
            httpd.dashboard = None  # type: ignore[attr-defined]
            httpd.rpc = None  # type: ignore[attr-defined]
            httpd.shutdown()
            httpd.server_close()
        # The sampler may be blocked inside describe() behind a power cycle that
        # holds the hub lock; never wait long enough to stall server shutdown.
        if sampler is not None and sampler is not threading.current_thread():
            sampler.join(timeout=2.0)
        return {"running": False, "stopped": httpd is not None}

    def status(self) -> dict:
        if self._httpd is None:
            return {"running": False}
        host, port = str(self._httpd.server_address[0]), self._httpd.server_address[1]
        return {
            "running": True,
            # The literal address we bound, never "localhost": Windows resolves that
            # to ::1 first and only falls back to 127.0.0.1 after the refused IPv6
            # connect has retried for ~2 s -- on EVERY hub daemon RPC.
            "url": f"http://{f'[{host}]' if ':' in host else host}:{port}/",
            "port": port,
            "interval_sec": self._interval,
            "history_samples": self._history_len,
            "holds_hub": True,
            "note": ("The dashboard reads the hub every interval, so this process keeps the hub's "
                     "control port: another process cannot open it until hub(action=dashboard, "
                     "state=off). The page is read-only."),
        }

    # -------------------------------------------------------------- sampling

    def _run(self) -> None:
        # Bound to this run's event: a restart swaps in a fresh one, so a sampler
        # still finishing a slow describe() exits instead of running twice.
        stop = self._stop
        while not stop.is_set():
            self.poll_once()
            stop.wait(self._interval)

    def poll_once(self) -> None:
        """Take one sample. Never raises: a hub fault is shown, not fatal."""
        now = self._clock()
        try:
            snapshot = self._manager.describe()
        except Exception as exc:  # noqa: BLE001 - every failure belongs on the page
            with self._lock:
                self._last_attempt = now
                if self._error != str(exc):
                    self._event(now, "error", None, str(exc))
                self._error = str(exc)
            self._refresh_sessions()
            return

        audit = []
        try:
            audit = list(self._manager.guard.get_audit_log())
        except Exception:  # noqa: BLE001 - the audit trail is optional context
            pass

        with self._lock:
            self._last_attempt = now
            if self._error is not None:
                self._event(now, "recovered", None, "hub reachable again")
            self._error = None
            self._last_ok = now
            self._latest = snapshot
            for port in snapshot.get("ports", []):
                ch = port.get("channel")
                if not isinstance(ch, int):
                    continue
                hist = self._history.get(ch)
                if hist is None:
                    hist = self._history[ch] = deque(maxlen=self._history_len)
                hist.append([round(now, 3), port.get("voltage_mv"), port.get("current_ma"),
                             _bit(port.get("power")), _bit(port.get("data"))])
                prev = self._prev_ports.get(ch)
                if prev is not None:
                    for field in ("power", "data"):
                        if prev.get(field) != port.get(field) and port.get(field) is not None:
                            self._event(now, field, ch, f"{field} {prev.get(field)} -> {port.get(field)}")
                self._prev_ports[ch] = {"power": port.get("power"), "data": port.get("data")}
            # The guard's log only appends; a shorter log means it was replaced.
            if len(audit) < self._audit_seen:
                self._audit_seen = 0
            for entry in audit[self._audit_seen:]:
                detail = f"{entry.get('action')} -> {entry.get('decision')}"
                if entry.get("acknowledged") is False:
                    detail += " (NOT acknowledged)"
                self._event(now, "audit", entry.get("channel"), detail, reason=entry.get("reason"),
                            client=entry.get("client"))
            self._audit_seen = len(audit)
        self._refresh_sessions()

    def _refresh_sessions(self) -> None:
        try:
            sessions = list(self._sessions())
        except Exception:  # noqa: BLE001 - session listing is advisory
            sessions = []
        with self._lock:
            self._sessions_view = sessions

    def _event(self, t: float, kind: str, channel: int | None, text: str, **extra: Any) -> None:
        entry = {"t": round(t, 3), "kind": kind, "channel": channel, "text": text}
        entry.update({k: v for k, v in extra.items() if v})
        self._events.append(entry)

    def state(self) -> dict:
        extra: dict = {}
        if self._extra_state is not None:
            try:
                extra = dict(self._extra_state())
            except Exception:  # noqa: BLE001 - extra context must not break the page
                extra = {}
        with self._lock:
            self.last_viewed = self._clock()
            return {
                **extra,
                "now": round(self._clock(), 3),
                "started_at": self._started_at,
                "interval_sec": self._interval,
                "history_samples": self._history_len,
                "connected": self._error is None and self._latest is not None,
                "error": self._error,
                "last_ok": self._last_ok,
                "last_attempt": self._last_attempt,
                "hub": self._latest,
                "history": {str(ch): list(points) for ch, points in self._history.items()},
                "history_fields": ["t", "voltage_mv", "current_ma", "power", "data"],
                "sessions": list(self._sessions_view),
                "events": list(self._events),
            }


def _bit(value: Any) -> int | None:
    if value == "on":
        return 1
    if value == "off":
        return 0
    return None


class _Handler(BaseHTTPRequestHandler):
    server_version = "stm32-gdb-mcp-hub-dashboard"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        # stdout is the MCP transport and stderr is the server log; a request line
        # per poll would drown the log in noise.
        return

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("["):
            name = host.split("]")[0] + "]"
        else:
            name = host.rsplit(":", 1)[0]
        return name in _ALLOWED_HOSTS

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        if not self._host_ok():
            self._send(403, b"forbidden host\n", "text/plain; charset=utf-8")
            return
        path = self.path.split("?", 1)[0]
        dashboard = getattr(self.server, "dashboard", None)
        if path in ("/", "/index.html"):
            self._send(200, _load_page(), "text/html; charset=utf-8")
        elif path == "/api/state":
            if dashboard is None:
                self._send(503, b'{"error": "dashboard stopping"}', "application/json")
                return
            body = json.dumps(dashboard.state(), default=str).encode("utf-8")
            self._send(200, body, "application/json")
        else:
            self._send(404, b"not found\n", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        if not self._host_ok():
            self._send(403, b"forbidden host\n", "text/plain; charset=utf-8")
            return
        rpc = getattr(self.server, "rpc", None)
        if self.path.split("?", 1)[0] != "/rpc" or rpc is None:
            self._send(404, b"not found\n", "text/plain; charset=utf-8")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= _MAX_RPC_BODY:
            self._send(413, b'{"ok": false}', "application/json")
            return
        status, payload = rpc(self.rfile.read(length), self.headers)
        self._send(status, json.dumps(payload, default=str).encode("utf-8"), "application/json")


hub_dashboard = HubDashboard()


def main(argv: list[str] | None = None) -> int:
    """Run the dashboard against its OWN hub connection, for when no MCP server is up."""
    import yaml

    from .hub import HubManager

    parser = argparse.ArgumentParser(description="Read-only web dashboard for the SmartUSBHub.")
    parser.add_argument("--config", help="YAML file with a 'hub:' block (port, map labels).")
    parser.add_argument("--hub-port", help="Hub control port, e.g. COM7 or /dev/ttyACM0. Omit to auto-scan.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="HTTP port (default 8765).")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SEC, help="Seconds between samples.")
    parser.add_argument("--open", action="store_true", help="Open the page in a browser.")
    args = parser.parse_args(argv)

    spec: dict = {}
    if args.config:
        with open(args.config, encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle) or {}
        spec = dict(loaded.get("hub") or {}) if isinstance(loaded, dict) else {}
    if args.hub_port:
        spec["port"] = args.hub_port

    manager = HubManager()
    manager.configure(spec)
    dashboard = HubDashboard()
    info = dashboard.start(manager, port=args.port, interval_sec=args.interval)
    print(f"hub dashboard: {info['url']}  (Ctrl+C to stop)", flush=True)
    if args.open:
        import webbrowser
        webbrowser.open(info["url"])
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        dashboard.stop()
        manager.close(restore_power=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
