import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

from . import process_guard


class GdbServerManager:
    def __init__(self):
        self.process = None
        self.server_type = None
        self.port = None
        self.log_buffer = []
        self._reader_thread = None

    def _read_output(self):
        if not self.process or not self.process.stdout:
            return
        for line in iter(self.process.stdout.readline, ''):
            if line:
                self.log_buffer.append(line.strip())
                if len(self.log_buffer) > 1000:
                    self.log_buffer.pop(0)

    def start(self, server_type: str, args: list[str] | None = None):
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

        return self.port

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

    def stop(self):
        if self.process and self.process.poll() is None:
            if os.name == 'nt':
                self.process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                self.process.terminate()
            
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None
        self.port = None
        self._reader_thread = None

    def is_alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def get_logs(self):
        return "\n".join(self.log_buffer)
