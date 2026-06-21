from mcp_server.hil_smoke import run_hil_smoke


class FakeServer:
    def __init__(self):
        self.started = []
        self.stopped = False
        self.server_type = None
        self.port = None

    def start(self, server_type, args):
        self.started.append((server_type, args))
        self.server_type = server_type
        self.port = 3333
        return self.port

    def stop(self):
        self.stopped = True


class FakeGdb:
    def __init__(self):
        self.commands = []
        self.stopped = False

    def start_gdb(self):
        self.commands.append("start_gdb")

    def connect(self, host="localhost", port=3333):
        self.commands.append(("connect", host, port))
        return [{"message": "connected"}]

    def execute_cli_command(self, cmd, timeout_sec=1.0):
        self.commands.append(cmd)
        return [{"message": "done"}]

    def read_typed_memory(self, address, width_bits=32, count=1):
        self.commands.append(("read", address, width_bits, count))
        return [{"payload": {"memory": [{"contents": "41c20f41"}]}}]

    def stop_gdb(self):
        self.stopped = True


def test_run_hil_smoke_connects_reads_ids_resumes_and_stops():
    server = FakeServer()
    gdb = FakeGdb()

    result = run_hil_smoke(
        {"server_type": "openocd", "server_args": ["-f", "target/stm32l4x.cfg"], "hil": {"halt": True}},
        server,
        gdb,
    )

    assert result["ok"] is True
    assert result["server"]["port"] == 3333
    assert result["cpuid"]["address"] == "0xE000ED00"
    assert result["dbgmcu_idcode"]["address"] == "0xE0042000"
    assert "monitor halt" in gdb.commands
    assert "monitor resume" in gdb.commands
    assert gdb.stopped is True
    assert server.stopped is True
