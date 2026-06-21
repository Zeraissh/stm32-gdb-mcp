from mcp_server.gdb_client import GdbClientManager


class FakeGdb:
    def __init__(self):
        self.commands = []

    def write(self, command, timeout_sec=1.0):
        self.commands.append((command, timeout_sec))
        return [{"message": "done"}]


def test_read_core_registers_uses_gdb_cli_info_registers():
    client = GdbClientManager()
    client.gdb = FakeGdb()

    response = client.read_core_registers()

    assert response == [{"message": "done"}]
    assert client.gdb.commands == [("info registers", 2.0)]


def test_write_typed_memory_uses_explicit_c_width():
    client = GdbClientManager()
    client.gdb = FakeGdb()

    client.write_typed_memory("0x20000000", "0x12345678", width_bits=32)

    assert client.gdb.commands == [
        ("set {uint32_t}0x20000000 = 0x12345678", 1.0)
    ]


def test_read_typed_memory_reads_byte_count_for_width_and_count():
    client = GdbClientManager()
    client.gdb = FakeGdb()

    client.read_typed_memory("0x20000000", width_bits=16, count=4)

    assert client.gdb.commands == [
        ("-data-read-memory-bytes 0x20000000 8", 1.0)
    ]
