from mcp_server.gdb_client import GdbClientManager


class FakeGdb:
    def __init__(self):
        self.commands = []

    def write(self, command, timeout_sec=1.0):
        self.commands.append((command, timeout_sec))
        return [{"message": "done"}]

    def get_gdb_response(self, timeout_sec=0.1, raise_error_on_timeout=False):
        return []


def test_load_symbols_uses_file_exec_and_symbols_without_download():
    client = GdbClientManager()
    client.gdb = FakeGdb()

    client.load_symbols("fw.axf")

    assert client.gdb.commands == [("-file-exec-and-symbols fw.axf", 2.0)]


def test_reset_halt_primes_the_ap_with_a_throwaway_read():
    client = GdbClientManager()
    client.gdb = FakeGdb()

    client.reset_halt("monitor reset halt")

    commands = [c[0] for c in client.gdb.commands]
    # the reset command runs first, then a throwaway read of the constant CPUID
    # register primes the memory-AP so the next real read is coherent.
    assert commands[0] == "monitor reset halt"
    assert any("-data-read-memory-bytes 0xE000ED00 4" in c for c in commands)


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
        ("-data-read-memory-bytes 0x20000000 8", 2.0)  # routed through the 'memory' timeout
    ]


def test_extract_first_memory_word_decodes_little_endian_contents():
    client = GdbClientManager()

    # GDB/MI -data-read-memory-bytes returns target bytes in memory (little-endian)
    # order; 0x10016435 is stored as bytes 35 64 01 10.
    response = [{"payload": {"memory": [{"contents": "35640110"}]}}]

    assert client._extract_first_memory_word(response) == 0x10016435


def test_extract_first_memory_word_only_uses_first_word_of_a_block():
    client = GdbClientManager()

    response = [{"payload": {"memory": [{"contents": "3564011000000000"}]}}]

    assert client._extract_first_memory_word(response) == 0x10016435
