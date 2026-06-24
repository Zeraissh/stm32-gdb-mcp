import time

from mcp_server.log_reader import FileLogReader, LogRingBuffer, ProcessLogReader, SerialLogReader


def test_file_log_reader_tails_appended_lines(tmp_path):
    path = tmp_path / "swo_itm.log"
    path.write_text("preexisting\n")  # should be skipped (seek to end on start)

    reader = FileLogReader("swo", poll_interval=0.01)
    reader.start(str(path))
    try:
        with open(path, "a") as f:
            f.write("hello over SWO\n")
            f.write("second line\n")
            f.flush()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and len(reader.get_logs()) < 2:
            time.sleep(0.02)
        lines = [e["line"] for e in reader.get_logs()]
        assert "hello over SWO" in lines
        assert "second line" in lines
        assert "preexisting" not in lines  # only new output after start
    finally:
        reader.stop()
    assert reader.is_running() is False


class FakeStdout:
    def __init__(self, lines):
        self.lines = list(lines)

    def readline(self):
        if not self.lines:
            return ""
        return self.lines.pop(0)


class FakeProcess:
    def __init__(self, lines):
        self.stdout = FakeStdout(lines)
        self.terminated = False
        self.killed = False

    def poll(self):
        return None if not self.terminated else 0

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        self.terminated = True
        return 0

    def kill(self):
        self.killed = True
        self.terminated = True


def test_log_ring_buffer_keeps_recent_entries_and_can_clear():
    buffer = LogRingBuffer(max_entries=2)

    buffer.append("rtt", "first")
    buffer.append("rtt", "second")
    buffer.append("rtt", "third")

    entries = buffer.get()
    assert [entry["line"] for entry in entries] == ["second", "third"]
    assert entries[0]["index"] == 2
    assert buffer.get(clear=True) == entries
    assert buffer.get() == []


def test_process_log_reader_captures_process_stdout_lines():
    created = []

    def factory(command, **kwargs):
        created.append((command, kwargs))
        return FakeProcess(["hello\n", "world\n"])

    reader = ProcessLogReader("rtt", process_factory=factory)
    reader.start(["JLinkRTTClient", "-Device", "STM32F407VG"])
    time.sleep(0.05)

    assert created[0][0] == ["JLinkRTTClient", "-Device", "STM32F407VG"]
    assert [entry["line"] for entry in reader.get_logs()] == ["hello", "world"]
    assert reader.status()["running"] is True
    reader.stop()
    assert reader.status()["running"] is False


class FakeSerial:
    def __init__(self, lines):
        self.lines = list(lines)
        self.is_open = True
        self.closed = False

    def readline(self):
        if not self.lines:
            time.sleep(0.01)
            return b""
        return self.lines.pop(0)

    def close(self):
        self.closed = True
        self.is_open = False


def test_serial_log_reader_captures_uart_lines_and_closes_port():
    created = []

    def factory(**kwargs):
        created.append(kwargs)
        return FakeSerial([b"boot\r\n", b"ready\r\n"])

    reader = SerialLogReader(serial_factory=factory)
    reader.start(port="COM7", baudrate=115200, timeout=0.1)
    time.sleep(0.05)

    assert created == [{"port": "COM7", "baudrate": 115200, "timeout": 0.1}]
    assert [entry["line"] for entry in reader.get_logs()] == ["boot", "ready"]
    assert reader.status()["running"] is True
    reader.stop()
    assert reader.status()["running"] is False
