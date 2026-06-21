import os
import signal
import subprocess
import threading
from datetime import datetime, timezone


class LogRingBuffer:
    def __init__(self, max_entries: int = 2000):
        self.max_entries = max_entries
        self.entries = []
        self._next_index = 1
        self._lock = threading.Lock()

    def append(self, source: str, line: str):
        entry = {
            "index": self._next_index,
            "time": datetime.now(timezone.utc).isoformat(),
            "source": source,
            "line": line.rstrip("\r\n"),
        }
        with self._lock:
            self._next_index += 1
            self.entries.append(entry)
            if len(self.entries) > self.max_entries:
                self.entries = self.entries[-self.max_entries:]

    def get(self, limit: int | None = None, since_index: int | None = None, clear: bool = False):
        with self._lock:
            entries = list(self.entries)
            if since_index is not None:
                entries = [entry for entry in entries if entry["index"] > since_index]
            if limit is not None:
                entries = entries[-limit:]
            if clear:
                self.entries = []
            return entries

    def clear(self):
        with self._lock:
            self.entries = []


class ProcessLogReader:
    def __init__(self, source: str, process_factory=None, max_entries: int = 2000):
        self.source = source
        self.buffer = LogRingBuffer(max_entries=max_entries)
        self.process_factory = process_factory or subprocess.Popen
        self.process = None
        self.command = None
        self._reader_thread = None

    def start(self, command: list[str]):
        if self.is_running():
            raise RuntimeError(f"{self.source} log reader is already running")
        if not command:
            raise ValueError("command must not be empty")

        self.command = list(command)
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        self.process = self.process_factory(
            self.command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            creationflags=creationflags,
        )
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

    def stop(self):
        if self.process and self.is_running():
            if os.name == "nt":
                try:
                    self.process.send_signal(signal.CTRL_BREAK_EVENT)
                except AttributeError:
                    self.process.terminate()
            else:
                self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None
        self._reader_thread = None

    def get_logs(self, limit: int | None = None, since_index: int | None = None, clear: bool = False):
        return self.buffer.get(limit=limit, since_index=since_index, clear=clear)

    def clear(self):
        self.buffer.clear()

    def status(self):
        return {
            "source": self.source,
            "running": self.is_running(),
            "command": self.command,
            "entry_count": len(self.buffer.get()),
        }

    def is_running(self):
        return self.process is not None and self.process.poll() is None

    def _read_loop(self):
        if not self.process or not self.process.stdout:
            return
        for line in iter(self.process.stdout.readline, ""):
            if line:
                self.buffer.append(self.source, line)


class SerialLogReader:
    def __init__(self, source: str = "uart", serial_factory=None, max_entries: int = 2000, encoding: str = "utf-8"):
        self.source = source
        self.buffer = LogRingBuffer(max_entries=max_entries)
        self.serial_factory = serial_factory
        self.encoding = encoding
        self.serial_port = None
        self.port = None
        self.baudrate = None
        self._reader_thread = None
        self._stop_event = threading.Event()

    def start(self, port: str, baudrate: int = 115200, timeout: float = 0.1):
        if self.is_running():
            raise RuntimeError(f"{self.source} log reader is already running")
        if not port:
            raise ValueError("port must not be empty")

        factory = self.serial_factory or self._load_pyserial_factory()
        self.port = port
        self.baudrate = baudrate
        self._stop_event.clear()
        self.serial_port = factory(port=port, baudrate=baudrate, timeout=timeout)
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

    def stop(self):
        self._stop_event.set()
        if self.serial_port is not None:
            try:
                self.serial_port.close()
            finally:
                self.serial_port = None
        self._reader_thread = None

    def get_logs(self, limit: int | None = None, since_index: int | None = None, clear: bool = False):
        return self.buffer.get(limit=limit, since_index=since_index, clear=clear)

    def clear(self):
        self.buffer.clear()

    def status(self):
        return {
            "source": self.source,
            "running": self.is_running(),
            "port": self.port,
            "baudrate": self.baudrate,
            "entry_count": len(self.buffer.get()),
        }

    def is_running(self):
        return self.serial_port is not None and bool(getattr(self.serial_port, "is_open", False))

    def _read_loop(self):
        while not self._stop_event.is_set() and self.serial_port is not None:
            line = self.serial_port.readline()
            if not line:
                if not self.is_running():
                    return
                continue
            if isinstance(line, bytes):
                text = line.decode(self.encoding, errors="replace")
            else:
                text = str(line)
            self.buffer.append(self.source, text)

    def _load_pyserial_factory(self):
        try:
            import serial
        except ImportError as exc:
            raise RuntimeError("pyserial is required for UART logging. Install with `pip install pyserial`.") from exc
        return serial.Serial
