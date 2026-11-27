import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

from . import process_guard
from .probe_lock import ProbeLock, probe_lock_manager


def _port_accepts(port: int | None, timeout: float = 0.2) -> bool:
    """True when something is already listening on ``port``."""
    if not port:
        return False
    try:
        with socket.create_connection(("localhost", port), timeout=timeout):
            return True
    except OSError:
        return False


class PortInUseError(RuntimeError):
    """A server already listens on the port and the caller forbade adopting it."""


# OpenOCD's defaults. Used only as a shutdown target when this session's gdb port
# is the default too; a custom gdb port must name its own control port.
_DEFAULT_GDB_PORT = 3333
_DEFAULT_TCL_PORT = 6666
_DEFAULT_TELNET_PORT = 4444

# "refusing to adopt" is the taxonomy needle for port_in_use. Keep it in every
# refusal, including one that names a server this session itself left behind.
_EXPLICIT_PORT_IN_USE = (
    "A GDB server is already listening on port {port} and this session did "
    "not start it; refusing to adopt it because explicit server_args were "
    "passed and would not apply to a server someone else configured. Stop "
    "that process (it may be left over from an earlier failed start), or "
    "choose a different gdb_port."
)


def _cfg_key(path: str | None) -> str | None:
    """Normalize an OpenOCD config path to ``interface/...`` or ``target/...``."""
    if not path:
        return None
    norm = path.replace("\\", "/").lower()
    for marker in ("interface/", "target/"):
        idx = norm.rfind(marker)
        if idx >= 0:
            return norm[idx:]
    return None


def openocd_config_files(args: list[str] | None) -> dict[str, str | None]:
    """The interface and target configs named by an OpenOCD argument list."""
    interface = None
    target = None
    tokens = list(args or [])
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "-f" and index + 1 < len(tokens):
            key = _cfg_key(tokens[index + 1])
            if key and key.startswith("interface/"):
                interface = key
            elif key and key.startswith("target/"):
                target = key
            index += 2
            continue
        index += 1
    return {"interface": interface, "target": target}


def compare_openocd_identity(expected_args: list[str] | None,
                             observed_args: list[str] | None) -> dict:
    """Compare the interface/target this session wants with a listener's argv.

    ``match`` is True when every config this session names is the one the
    listener was started with, False when one of them differs or is missing,
    and None when there is nothing to compare (no expected config, or the
    listener's command line could not be read).
    """
    expected = openocd_config_files(expected_args)
    if observed_args is None:
        return {"expected": expected, "observed": None, "match": None}
    observed = openocd_config_files(observed_args)
    wanted = {name: expected[name] for name in ("interface", "target") if expected[name]}
    if not wanted:
        return {"expected": expected, "observed": observed, "match": None}
    match = all(observed.get(name) == value for name, value in wanted.items())
    return {"expected": expected, "observed": observed, "match": match}


def _openocd_setting(args: list[str] | None, name: str) -> str | None:
    """The value of an OpenOCD ``-c "<name> <value>"`` command, if present."""
    tokens = list(args or [])
    index = 0
    while index < len(tokens):
        if tokens[index] == "-c" and index + 1 < len(tokens):
            parts = tokens[index + 1].split()
            if len(parts) >= 2 and parts[0] == name:
                return parts[1]
            index += 2
            continue
        index += 1
    return None


def control_ports(args: list[str] | None, gdb_port: int | None) -> list[tuple[str, int]]:
    """TCL then telnet ports it is safe to send ``shutdown`` to.

    A port named in ``args`` is included. The OpenOCD defaults (6666/4444) are
    included only for the default gdb port, and only when the args did not
    disable that service. Guessing 6666 on a custom gdb port can shut down a
    different session's server.
    """
    found: list[tuple[str, int]] = []

    def add(kind: str, port: int) -> None:
        if port > 0 and (kind, port) not in found:
            found.append((kind, port))

    for kind, name in (("tcl", "tcl_port"), ("telnet", "telnet_port")):
        raw = _openocd_setting(args, name)
        if raw and raw.isdigit():
            add(kind, int(raw))
    if gdb_port in (None, _DEFAULT_GDB_PORT):
        if _openocd_setting(args, "tcl_port") != "disabled":
            add("tcl", _DEFAULT_TCL_PORT)
        if _openocd_setting(args, "telnet_port") != "disabled":
            add("telnet", _DEFAULT_TELNET_PORT)
    return found


def _listening_inodes(port: int) -> set[str]:
    """Socket inodes in LISTEN state on ``port``, from ``/proc/net/tcp*``."""
    hex_port = f"{port:04X}"
    inodes: set[str] = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(table, encoding="utf-8", errors="replace") as handle:
                next(handle, None)
                for line in handle:
                    parts = line.split()
                    if len(parts) < 10 or parts[3] != "0A":
                        continue
                    if parts[1].rsplit(":", 1)[-1].upper() == hex_port:
                        inodes.add(parts[9])
        except OSError:
            continue
    return inodes


def _listening_pids(port: int) -> list[int]:
    """PIDs with a listening socket on ``port``. Empty when /proc can't say."""
    inodes = _listening_inodes(port)
    if not inodes:
        return []
    markers = {f"socket:[{inode}]" for inode in inodes}
    found: list[int] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return []
    for entry in entries:
        if not entry.isdigit():
            continue
        fd_dir = f"/proc/{entry}/fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(f"{fd_dir}/{fd}")
            except OSError:
                continue
            if target in markers:
                found.append(int(entry))
                break
    return found


def _process_argv(pid: int) -> list[str] | None:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            raw = handle.read()
    except OSError:
        return None
    parts = [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]
    return parts or None


def listener_argv(port: int) -> list[str] | None:
    """Command line of a process listening on ``port``, if it can be read.

    Prefers a process whose arguments name an OpenOCD interface or target.
    None means the listener could not be identified (another OS, or no
    permission); callers must not treat that as a mismatch.
    """
    try:
        pids = _listening_pids(port)
    except OSError:
        return None
    fallback: list[str] | None = None
    for pid in pids:
        argv = _process_argv(pid)
        if not argv:
            continue
        if fallback is None:
            fallback = argv
        parsed = openocd_config_files(argv)
        if parsed["interface"] or parsed["target"]:
            return argv
    return fallback


def _identity_sentence(args: list[str] | None, port: int) -> str:
    """What is listening, compared with the interface/target ``args`` name."""
    expected = openocd_config_files(args)
    if not expected["interface"] and not expected["target"]:
        return ""
    observed_argv = listener_argv(port)
    if observed_argv is None:
        return (" Its interface and target could not be identified, so they "
                "were not checked against this session.")
    observed = openocd_config_files(observed_argv)
    return (
        f" The listening server is interface {observed.get('interface') or 'unknown'}"
        f" and target {observed.get('target') or 'unknown'}; this session wants"
        f" interface {expected.get('interface') or 'unspecified'} and target"
        f" {expected.get('target') or 'unspecified'}."
    )


def _tcl_shutdown(port: int, timeout: float = 1.0) -> bool:
    """Send OpenOCD's length-prefixed TCL ``shutdown``. True if it was sent."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            payload = b"shutdown"
            sock.sendall(len(payload).to_bytes(4, "big") + payload)
            return True
    except OSError:
        return False


def _telnet_shutdown(port: int, timeout: float = 1.0) -> bool:
    """Send OpenOCD's telnet ``shutdown``. True if it was sent.

    OpenOCD's telnet port is a raw TCP command line, not a negotiated telnet.
    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            try:
                sock.recv(1024)  # greeting / prompt; absent is fine
            except OSError:
                pass
            sock.sendall(b"shutdown\r\n")
            return True
    except OSError:
        return False


def server_liveness(manager) -> dict:
    """``server_alive`` plus the port probe behind it.

    Managers that predate :meth:`GdbServerManager.liveness` (test fakes) keep
    the boolean they already report. ``server_port_listening`` is None when
    the port was not probed.
    """
    probe = getattr(manager, "liveness", None)
    if callable(probe):
        info = probe() or {}
        alive = bool(info.get("alive"))
        port_listening = info.get("port_listening")
        mismatch = info.get("mismatch")
    else:
        alive = bool(manager.is_alive())
        port_listening = None
        mismatch = None
    return {
        "server_alive": alive,
        "server_port_listening": port_listening,
        "server_liveness_mismatch": mismatch,
    }


class GdbServerManager:
    def __init__(self):
        self.process = None
        self.server_type = None
        self.port = None
        self.server_args: list[str] = []
        self.log_buffer = []
        self._reader_thread = None
        # True when the server on self.port was already running and we attached to
        # it instead of spawning our own. We do not own it, so stop() leaves it
        # running unless the caller passes stop_adopted=True.
        self.adopted = False
        # Port of the last server this manager spawned and stopped, so a caller can
        # confirm the teardown really freed it (stop() clears self.port).
        self.last_stopped_port: int | None = None
        # Set for the duration of one start_debug_session retry loop. While set,
        # a later attempt must not forget last_stopped_port, and must not adopt
        # a server that attempt itself left listening (issue #77).
        self.preserve_stopped_port = False
        self.refuse_own_leftover = False
        # "explicit", a profile/inferred description, or None. Selects the
        # port_in_use wording when adopt=False. Not a start() argument: test
        # fakes take (server_type, args, adopt) and reject extra keywords.
        self.adopt_refuse_reason: str | None = None
        # Outcome of the most recent stop(), including an adopted shutdown.
        self.last_stop_result: dict | None = None
        # Cross-process probe lock held by THIS start() call.
        self._probe_lock: ProbeLock | None = None

    def _read_output(self):
        if not self.process or not self.process.stdout:
            return
        for line in iter(self.process.stdout.readline, ''):
            if line:
                self.log_buffer.append(line.strip())
                if len(self.log_buffer) > 1000:
                    self.log_buffer.pop(0)

    def start(self, server_type: str, args: list[str] | None = None, adopt: bool = True):
        if self.process and self.process.poll() is None:
            raise RuntimeError("A GDB server is already running.")

        if args is None:
            args = []

        self.server_type = server_type.lower()
        if self.server_type == "openocd":
            cmd = ["openocd"] + args
            # Honor a custom 'gdb_port N' in the args so concurrent OpenOCD instances
            # (one per session) can each bind a distinct port.
            self.port = self._extract_openocd_gdb_port(args, default=3333)
        elif self.server_type == "stlink":
            st_util = shutil.which("st-util")
            if st_util:
                cmd = [st_util] + args
                self.port = self._extract_port(args, default=4242)
            else:
                stlink_gdb = (
                    shutil.which("ST-LINK_gdbserver")
                    or shutil.which("ST-LINK_gdbserver.exe")
                )
                if not stlink_gdb:
                    raise RuntimeError(
                        "stlink backend requires st-util or ST-LINK_gdbserver.exe on PATH."
                    )
                cmd = [stlink_gdb] + self._translate_stlink_args(args)
                self.port = self._extract_port(args, default=4242)
        elif self.server_type == "jlink":
            # JLinkGDBServerCL requires an explicit device often, assuming it's provided in args
            cmd = ["JLinkGDBServerCL"] + args
            self.port = 2331
        else:
            raise ValueError(f"Unknown server type: {server_type}")

        self.log_buffer = []
        # A refused start must not forget a server we had already adopted: the
        # caller still needs stop(stop_adopted=True) to be able to reach it.
        previous_adoption = {
            "adopted": self.adopted,
            "port": self.port,
            "server_args": list(self.server_args),
            "server_type": self.server_type,
        }
        self.adopted = False
        self.server_args = list(args)
        # A retry inside one start_debug_session call must still remember the
        # port the previous attempt spawned. Every other caller forgets it up
        # front, which is what stop()'s record is for after the call returns.
        if not self.preserve_stopped_port:
            self.last_stopped_port = None

        # A server already listening on our port is almost always a previous run
        # that outlived its client, or one the user started by hand. Spawning a
        # second one cannot work -- it fails to bind, and worse, the port probe
        # below can see the OLD server answer and call the start a success.
        # Adopt it instead; the probe is exclusive, so this is the only way to
        # reach the target short of killing a process we do not own.
        #
        # adopt=False means the caller has a concrete configuration (explicit
        # server_args, the debug profile, or args inferred from the MCU and
        # probe). Adopting would silently discard it (issue #47 / #77).
        if _port_accepts(self.port):
            if self._is_own_leftover(args):
                port = self.port
                self.port = None
                self._restore_adoption(previous_adoption)
                # Leave last_stopped_port set so the caller can report
                # server_still_listening. This attempt did start that server.
                raise PortInUseError(
                    f"A GDB server this session started is still listening on port {port} "
                    f"after teardown; refusing to adopt it or start another until that "
                    f"port is free."
                )
            if not adopt:
                port = self.port
                self.port = None
                self._forget_stopped_port(port)
                self._restore_adoption(previous_adoption)
                raise PortInUseError(self._port_in_use_message(port, args))
            mismatch = self._identity_mismatch(args)
            if mismatch is not None:
                port = self.port
                self.port = None
                self._forget_stopped_port(port)
                self._restore_adoption(previous_adoption)
                raise PortInUseError(mismatch)
            self.adopted = True
            self.process = None
            self.last_stopped_port = None
            return self.port

        # --- cross-process probe lock -------------------------------------------
        # Acquire BEFORE spawning so two processes can't both launch a GDB server
        # for the same probe.  The adopted path above skips this: we are not
        # starting our own server, so we don't need to lock the probe.
        lock = probe_lock_manager.acquire(self.server_type, args)
        self._probe_lock = lock

        try:
            # We create a new process group so we can send CTRL_BREAK_EVENT on Windows.
            # sys.platform in an if-statement (not os.name, not a ternary) so mypy skips the
            # Windows-only attribute when checking other platforms.
            if sys.platform == "win32":
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                creationflags = 0

            # stdin=DEVNULL is critical: the MCP server talks JSON-RPC over its own stdin,
            # so a spawned child must NEVER inherit it (it would steal protocol bytes and
            # hang the server with no output).
            # On Linux the child asks the kernel to kill it if we die; on Windows the
            # job object installed at startup covers this. Either way a killed MCP
            # server must not leave a GDB server holding the probe.
            self.process = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                creationflags=creationflags,
                preexec_fn=process_guard.child_preexec(),  # noqa: PLW1509 - None on win32
            )

            # Backfill the child PID so zombie-openocd scenarios are attributable.
            # Use getattr so mock process objects (tests) without .pid don't break.
            child_pid = getattr(self.process, "pid", None)
            if child_pid is not None:
                probe_lock_manager.backfill_child_pid(lock, child_pid)

            self._reader_thread = threading.Thread(target=self._read_output, daemon=True)
            self._reader_thread.start()

            # Wait until the GDB server actually accepts connections (instead of a fixed
            # sleep), so a fast-binding server returns in ~0.2s instead of always 1.5s.
            if not self._wait_for_port(self.port, timeout=10.0):
                logs = self.get_logs()
                process_exited = self.process.poll() is not None
                self.stop()
                if process_exited:
                    raise RuntimeError(f"GDB server failed to start. Logs: {logs}")
                raise RuntimeError(
                    f"GDB server did not open its port within timeout. Logs: {logs}"
                )
        except Exception:
            # Any failure after lock acquisition must release the lock so the
            # probe is not stranded.
            probe_lock_manager.release(self._probe_lock)
            self._probe_lock = None
            raise

        self.last_stopped_port = None
        return self.port

    def _restore_adoption(self, previous: dict) -> None:
        if not previous.get("adopted"):
            return
        self.adopted = True
        self.port = previous.get("port")
        self.server_args = list(previous.get("server_args") or [])
        self.server_type = previous.get("server_type")

    def _forget_stopped_port(self, port: int) -> None:
        """Drop the remembered port when this refusal is about that same port.

        A stranger on the port we spawned means that server is no longer the
        one ``server_still_listening`` should name. A remembered port this
        attempt did not touch is left alone.
        """
        if not self.preserve_stopped_port or self.last_stopped_port == port:
            self.last_stopped_port = None

    def _is_own_leftover(self, args: list[str]) -> bool:
        """True when the listener is a server a previous attempt of this call spawned.

        A command line that names a different interface or target is someone
        else who reused the port, not our leftover. An unreadable command line
        stays "ours": the port is the one we just stopped, and inventing a
        stranger would bring back the misleading refusal.
        """
        if not self.refuse_own_leftover or not self.port:
            return False
        if self.last_stopped_port != self.port or not _port_accepts(self.port):
            return False
        observed = listener_argv(self.port)
        if observed is None:
            return True
        return compare_openocd_identity(args, observed)["match"] is not False

    def _port_in_use_message(self, port: int, args: list[str]) -> str:
        reason = self.adopt_refuse_reason
        if reason in (None, "explicit"):
            message = _EXPLICIT_PORT_IN_USE.format(port=port)
        else:
            message = (
                f"A GDB server is already listening on port {port} and this session did "
                f"not start it; refusing to adopt it because {reason} would not apply to "
                f"a server someone else configured. Stop that process (it may be left "
                f"over from an earlier failed start), or choose a different gdb_port."
            )
        return message + _identity_sentence(args, port)

    def _identity_mismatch(self, args: list[str]) -> str | None:
        """Refusal text when a server we would adopt is a different interface/target.

        Unknown identity is not a mismatch: failing closed there would stop
        attaching to a server whose command line cannot be read, which is the
        previous behavior of adopt=True.
        """
        expected = openocd_config_files(args)
        if not expected["interface"] and not expected["target"]:
            return None
        observed = listener_argv(self.port) if self.port else None
        if compare_openocd_identity(args, observed)["match"] is not False:
            return None
        return (
            f"A GDB server is already listening on port {self.port}; refusing to adopt "
            f"it because its interface/target do not match this session."
            + _identity_sentence(args, self.port or 0)
        )

    def _wait_for_port(self, port: int, timeout: float = 10.0, poll: float = 0.05) -> bool:
        """Poll until the GDB server's TCP port accepts a connection (ready)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                return False  # server process exited before binding the port
            try:
                with socket.create_connection(("localhost", port), timeout=0.2):
                    return True
            except OSError:
                time.sleep(poll)
        return False

    def _extract_openocd_gdb_port(self, args: list[str], default: int) -> int:
        """Find the gdb port from an OpenOCD '-c gdb_port N' command, else the default."""
        for token in args or []:
            if isinstance(token, str) and "gdb_port" in token:
                parts = token.split()
                if len(parts) >= 2 and parts[-1].isdigit():
                    return int(parts[-1])
        return default

    def _extract_port(self, args: list[str], default: int) -> int:
        if not args:
            return default
        for i, token in enumerate(args):
            if token in ("-p", "--port", "--port-number") and i + 1 < len(args):
                try:
                    return int(args[i + 1])
                except ValueError:
                    return default
        return default

    def _translate_stlink_args(self, args: list[str]) -> list[str]:
        # Translate a minimal st-util-style argument set into ST-LINK_gdbserver options.
        translated = ["-d"]
        port = self._extract_port(args, default=4242)
        translated.extend(["-p", str(port)])

        has_cp = bool(args) and any(token == "-cp" for token in args)
        if not has_cp:
            cubeprog = self._find_cubeprogrammer_path()
            if cubeprog:
                translated.extend(["-cp", cubeprog])

        if args and "--no-reset" in args:
            translated.append("-g")

        return translated

    def _find_cubeprogrammer_path(self) -> str | None:
        # Prefer an explicit env override, then common install locations.
        env_path = os.environ.get("STM32CUBEPROGRAMMER_PATH")
        if env_path and os.path.isdir(env_path):
            return env_path

        candidates = [
            r"C:\ST\STM32CubeCLT_1.21.0\STM32CubeProgrammer\bin",
            r"C:\ST\STM32CubeProgrammer\bin",
            r"C:\Program Files\STMicroelectronics\STM32Cube\STM32CubeProgrammer\bin",
            r"C:\Program Files (x86)\STMicroelectronics\STM32Cube\STM32CubeProgrammer\bin",
        ]
        for path in candidates:
            if os.path.isdir(path):
                return path

        return None

    def stop(self, stop_adopted: bool = False):
        """Stop the GDB server we spawned. An adopted one is left running.

        Killing a server we did not start would take down whatever else is using
        it -- the user's own OpenOCD in a terminal, or another session's -- and a
        hard kill has been observed to wedge the ST-Link endpoint until the probe
        is physically unplugged.

        ``stop_adopted=True`` is the explicit opt-in that instead asks an adopted
        OpenOCD to exit, by sending ``shutdown`` on a TCL or telnet port that
        belongs to the same process as the gdb port, then checks that the gdb
        port actually closed. The process is never signalled.
        """
        was_adopted = bool(self.adopted)
        port = self.port
        result: dict = {
            "was_adopted": was_adopted,
            "stop_adopted": bool(stop_adopted),
            "shutdown_sent": False,
            "shutdown_via": None,
            "port": port,
            "port_free": None,
            "detail": None,
        }
        if was_adopted and not stop_adopted:
            self._forget_server()
            result["detail"] = "adopted server left running"
            result["port_free"] = False if port and _port_accepts(port) else True
            self.last_stop_result = result
            return

        if was_adopted and stop_adopted:
            via = self._shutdown_adopted_server()
            result["shutdown_sent"] = via is not None
            result["shutdown_via"] = via
            free = not (port and _port_accepts(port))
            if via and not free and port:
                free = self._wait_port_closed(port)
            result["port_free"] = free
            if free:
                self.last_stopped_port = port
                self._forget_server()
                result["detail"] = f"adopted server shut down via {via}"
            else:
                # Keep adopted and port so the caller can retry the shutdown.
                if via:
                    result["detail"] = f"adopted server is still listening after {via} shutdown"
                else:
                    result["detail"] = (
                        "adopted server is still listening; no telnet/tcl port "
                        "of this server accepted shutdown"
                    )
            self.last_stop_result = result
            return

        if self.process is not None:
            self.last_stopped_port = self.port
        if self.process and self.process.poll() is None:
            # sys.platform, not os.name: mypy then skips the Windows-only attribute.
            if sys.platform == "win32":
                self.process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                self.process.terminate()

            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self._forget_server()
        result["detail"] = "spawned server stopped" if port else "no server"
        result["port_free"] = False if self.last_stopped_port and _port_accepts(self.last_stopped_port) else True
        self.last_stop_result = result

    def _forget_server(self) -> None:
        self.process = None
        self.port = None
        self._reader_thread = None
        self.adopted = False
        probe_lock_manager.release(self._probe_lock)
        self._probe_lock = None

    def _shutdown_adopted_server(self) -> str | None:
        """Send ``shutdown`` to this server's own TCL or telnet port.

        The control port's listener must be the same process as the gdb port.
        Sending ``shutdown`` to 6666 or 4444 on a guess would stop a different
        session. Returns ``"tcl"`` or ``"telnet"`` when the command was sent.
        """
        if self.server_type != "openocd" or not self.port:
            return None
        gdb_pids = set(_listening_pids(self.port))
        if not gdb_pids:
            return None
        for kind, port in control_ports(self.server_args, self.port):
            if port == self.port:
                continue
            if not (set(_listening_pids(port)) & gdb_pids):
                continue
            send = _tcl_shutdown if kind == "tcl" else _telnet_shutdown
            if send(port):
                return kind
        return None

    def _wait_port_closed(self, port: int, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            if not _port_accepts(port, timeout=0.2):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def liveness(self) -> dict:
        """Whether the server is still there, and whether that disagrees with the process.

        A live process handle is enough: probing its gdb port would attach a
        second TCP client. When the handle is gone but ``port`` still accepts
        connections, the port is probed and the mismatch is reported instead of
        collapsing to "not alive" (issue #77). ``port_listening`` is None when
        the port was not probed.
        """
        if self.process is not None:
            process_alive: bool | None = self.process.poll() is None
        else:
            process_alive = None

        if process_alive:
            return {
                "alive": True,
                "process_alive": True,
                "port_listening": None,
                "mismatch": None,
            }

        port_listening = bool(self.port) and _port_accepts(self.port)
        # The handle has exited (or was never ours) while the port we still
        # record is open. An adopted server has no handle by design; that is
        # not a mismatch. A spawned server whose handle died is.
        mismatch = None
        if port_listening and self.process is not None and process_alive is False:
            mismatch = "process_gone_port_listening"
        alive = bool(port_listening and (self.adopted or mismatch))
        return {
            "alive": alive,
            "process_alive": process_alive,
            "port_listening": port_listening,
            "mismatch": mismatch,
        }

    def is_alive(self) -> bool:
        return bool(self.liveness()["alive"])

    def get_logs(self):
        return "\n".join(self.log_buffer)
