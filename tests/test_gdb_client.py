import inspect
import re

import pytest
from conftest import FakeGdb

import mcp_server.gdb_client as gdb_client_module
from mcp_server.gdb_client import GdbClientManager
from mcp_server.mi_guard import GdbCommandError
from mcp_server.stop_event import was_already_halted
from mcp_server.timeouts import DEFAULTS


def test_load_symbols_uses_file_exec_and_symbols_without_download():
    client = GdbClientManager()
    client.gdb = FakeGdb()

    client.load_symbols("fw.axf")

    assert client.gdb.commands == [("-file-exec-and-symbols fw.axf", 2.0)]


def test_symbolize_pc_reads_console_output_not_the_command_echo():
    # info symbol returns a 'log' echo of the command plus the real 'console' answer.
    class SymGdb:
        def write(self, command, timeout_sec=1.0):
            return [
                {"type": "log", "payload": command},  # echo — must be ignored
                {"type": "console", "payload": "vTaskDelay + 4 in section .text\n"},
            ]

    client = GdbClientManager()
    client.gdb = SymGdb()
    assert client.symbolize_pc(0x08000400) == "vTaskDelay"


def test_symbolize_pc_returns_empty_when_no_symbol_matches():
    class SymGdb:
        def write(self, command, timeout_sec=1.0):
            return [{"type": "console", "payload": "No symbol matches 0x20000000.\n"}]

    client = GdbClientManager()
    client.gdb = SymGdb()
    assert client.symbolize_pc(0x20000000) == ""


def test_every_named_timeout_used_by_gdb_client_has_a_default():
    # TimeoutConfig.get silently falls back to 'default' (1.0) for unknown names, so a
    # typo'd or missing name would quietly shrink an operation's deadline. Guard against it.
    source = inspect.getsource(gdb_client_module)
    used = set(re.findall(r'timeouts\.get\("(\w+)"\)', source))
    assert used, "expected gdb_client to route timeouts through TimeoutConfig"
    assert used <= set(DEFAULTS), f"names missing from DEFAULTS: {used - set(DEFAULTS)}"


def test_symbolize_pc_caches_lookups_and_load_symbols_invalidates():
    class CountingGdb:
        def __init__(self):
            self.symbol_lookups = 0

        def write(self, command, timeout_sec=1.0):
            if command.startswith("info symbol"):
                self.symbol_lookups += 1
                return [{"type": "console", "payload": "vTaskDelay + 4 in section .text\n"}]
            return [{"type": "result", "message": "done", "payload": None}]

    client = GdbClientManager()
    client.gdb = CountingGdb()

    assert client.symbolize_pc(0x08000400) == "vTaskDelay"
    assert client.symbolize_pc(0x08000400) == "vTaskDelay"
    assert client.gdb.symbol_lookups == 1  # second call served from the cache

    client.load_symbols("fw.axf")  # new symbol table -> cache must be dropped
    assert client.symbolize_pc(0x08000400) == "vTaskDelay"
    assert client.gdb.symbol_lookups == 2


def test_symbolize_pc_caches_negative_results_too():
    class CountingGdb:
        def __init__(self):
            self.symbol_lookups = 0

        def write(self, command, timeout_sec=1.0):
            self.symbol_lookups += 1
            return [{"type": "console", "payload": "No symbol matches 0x20000000.\n"}]

    client = GdbClientManager()
    client.gdb = CountingGdb()

    assert client.symbolize_pc(0x20000000) == ""
    assert client.symbolize_pc(0x20000000) == ""
    assert client.gdb.symbol_lookups == 1


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

    assert response == [{"type": "result", "message": "done", "payload": None}]
    assert client.gdb.commands == [("info registers", 2.0)]


def test_halt_execution_uses_configured_halt_timeout():
    client = GdbClientManager()
    client.gdb = FakeGdb()  # canned replies carry no register value -> probe says "running"
    client.timeouts.set({"halt": 4.5})

    client.halt_execution()

    assert ("-exec-interrupt", 4.5) in client.gdb.commands


def test_formerly_hardcoded_timeouts_are_overridable():
    # These call sites used to carry literal deadlines; they must now honor set_timeouts.
    client = GdbClientManager()
    client.gdb = FakeGdb()
    client.timeouts.set({"symbols": 7.0, "step": 3.5, "run": 42.0, "verify": 99.0})

    client.load_symbols("fw.axf")
    client.step_over()
    client.run_to_line("main.c:10")
    client.verify_flash("fw.axf")

    assert client.gdb.commands == [
        ("-file-exec-and-symbols fw.axf", 7.0),
        ("-exec-next", 3.5),
        ("-exec-until main.c:10", 42.0),
        ("-file-exec-and-symbols fw.axf", 7.0),
        ("compare-sections", 99.0),
    ]


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


def test_extract_first_memory_word_prefers_structured_memory_over_stop_console():
    client = GdbClientManager()

    response = [
        {"type": "console", "payload": "0x08000108 in ?? ()\n"},
        {"type": "notify", "message": "stopped", "payload": {"frame": {"addr": "0x08000108"}}},
        {"type": "result", "payload": {"memory": [{"contents": "30c22f41"}]}},
    ]

    assert client._extract_first_memory_word(response) == 0x412FC230


# --- issue #21: ok:true despite raw GDB errors -------------------------------


class ScriptedGdb:
    """Answers each write from a {command-prefix: records} script."""

    def __init__(self, script, default=None, pending=None):
        self.commands = []
        self._script = script
        self._default = default if default is not None else [
            {"type": "result", "message": "done", "payload": None}
        ]
        self._pending = list(pending or [])

    def write(self, command, timeout_sec=1.0, raise_error_on_timeout=True):
        self.commands.append((command, timeout_sec))
        for prefix, records in self._script.items():
            if command.startswith(prefix):
                return list(records)
        return list(self._default)

    def get_gdb_response(self, timeout_sec=0.1, raise_error_on_timeout=False):
        batch, self._pending = self._pending, []
        return list(batch)


def test_load_symbols_raises_when_gdb_says_no_such_file():
    client = GdbClientManager()
    client.gdb = ScriptedGdb({
        "-file-exec-and-symbols": [
            {"type": "console", "payload": "fw.axf: No such file or directory."},
            {"type": "result", "message": "done", "payload": None},
        ]
    })

    with pytest.raises(GdbCommandError, match="No such file or directory"):
        client.load_symbols("fw.axf")


def test_flash_raises_when_the_erase_fails_instead_of_reporting_success():
    client = GdbClientManager()
    client.gdb = ScriptedGdb({
        "-target-download": [
            {"type": "log", "payload": "Error erasing flash with vFlashErase packet\n"},
            {"type": "result", "message": "done", "payload": None},
        ]
    })

    with pytest.raises(GdbCommandError, match="Error erasing flash"):
        client.load_firmware("fw.elf")


def test_flash_raises_when_the_download_never_reports_completion():
    client = GdbClientManager()
    client.gdb = ScriptedGdb({
        "-target-download": [{"type": "console", "payload": "Loading section .text\n"}]
    })

    with pytest.raises(GdbCommandError, match="did not report completion"):
        client.load_firmware("fw.elf")


def test_typed_memory_write_raises_when_no_symbol_table_is_loaded():
    client = GdbClientManager()
    client.gdb = ScriptedGdb({
        "set {": [
            {"type": "log", "payload": "No symbol table is loaded.  Use the \"file\" command."},
            {"type": "result", "message": "done", "payload": None},
        ]
    })

    with pytest.raises(GdbCommandError, match="No symbol table"):
        client.write_typed_memory("0x20000000", "1", width_bits=32)


def test_windows_backslash_paths_are_normalized_for_gdb():
    # GDB/MI eats backslashes as escapes, so C:\proj\fw.elf silently became a
    # nonexistent path that still reported ok:true (issue #22, second half).
    client = GdbClientManager()
    client.gdb = FakeGdb()

    client.load_symbols(r"C:\proj\build\fw.elf")

    assert client.gdb.commands[0][0] == "-file-exec-and-symbols C:/proj/build/fw.elf"


# --- issue #22: missed stops and leaked SIGINT --------------------------------


def test_halt_execution_does_not_interrupt_an_already_halted_target():
    # A redundant -exec-interrupt leaves a pending interrupt that fires as a
    # spurious SIGINT on the next resume (issue #22). The halted state is known
    # from the *stopped GDB already sent - never by probing the target.
    client = GdbClientManager()
    client.gdb = ScriptedGdb({"-exec-continue": []}, pending=[
        {"type": "notify", "message": "stopped", "payload": {"reason": "breakpoint-hit", "bkptno": "1"}},
    ])

    response = client.halt_execution()

    assert was_already_halted(response)
    assert not any(cmd.startswith("-exec-interrupt") for cmd, _ in client.gdb.commands)


def test_halt_execution_interrupts_a_target_known_to_be_running():
    client = GdbClientManager()
    client.gdb = ScriptedGdb({"-exec-continue": [
        {"type": "notify", "message": "running", "payload": {"thread-id": "all"}},
    ]})
    client.run_and_wait(timeout_sec=0.01)          # leaves it running
    assert client.is_running() is True

    client.halt_execution()

    assert any(cmd.startswith("-exec-interrupt") for cmd, _ in client.gdb.commands)


def test_wait_for_stop_timeout_sends_no_command_to_the_target():
    # THE desync regression, found on a real L151: probing a RUNNING target with
    # an MI command queues it until the target halts, and the late reply offsets
    # every later response, wedging the session. A timeout must stay silent.
    client = GdbClientManager()
    client.gdb = ScriptedGdb({})

    event = client.wait_for_stop(timeout_sec=0.05)

    assert event["stopped"] is False and event["reason"] == "timeout"
    assert client.gdb.commands == [], f"timeout path sent {client.gdb.commands}"


def test_run_and_wait_drops_stale_stop_records_before_resuming():
    # Without the pre-drain, the *stopped left over from a previous halt is
    # returned as if it were the stop of THIS run.
    stale = [{"type": "notify", "message": "stopped", "payload": {"reason": "breakpoint-hit", "bkptno": "1"}}]
    client = GdbClientManager()
    client.gdb = ScriptedGdb({"-exec-continue": []}, pending=stale)

    event = client.run_and_wait(timeout_sec=0.05)

    assert event["stopped"] is False
    assert event["reason"] == "timeout"


def test_wait_for_stop_catches_a_late_stopped_notification():
    # Windows pipe polling delivered the *stopped of a hit breakpoint late - it
    # only surfaced on a later call (issue #22). The final patient drain picks up
    # the straggler instead of reporting a false timeout.
    class LateGdb(ScriptedGdb):
        def __init__(self):
            super().__init__({})
            self._reads = 0

        def get_gdb_response(self, timeout_sec=0.1, raise_error_on_timeout=False):
            self._reads += 1
            if timeout_sec >= 0.5:   # only the longer final read sees it
                return [{"type": "notify", "message": "stopped",
                         "payload": {"reason": "breakpoint-hit", "bkptno": "2",
                                     "frame": {"func": "trigger_divzero", "file": "main.c",
                                               "line": "21", "addr": "0x8000046"}}}]
            return []

    client = GdbClientManager()
    client.gdb = LateGdb()

    event = client.wait_for_stop(timeout_sec=0.05)

    assert event["stopped"] is True
    assert event["reason"] == "breakpoint-hit"
    assert event["frame"]["func"] == "trigger_divzero"
    assert event["frame"]["line"] == 21
    assert client.gdb.commands == [], "the straggler must be read, not commanded for"


def test_verify_flash_raises_when_sections_are_mis_matched():
    # compare-sections reports a mismatch in console text while the MI command
    # itself succeeds — the exact shape of a false ok:true (issue #21).
    client = GdbClientManager()
    client.gdb = ScriptedGdb({
        "compare-sections": [
            {"type": "console", "payload": "Section .text, range 0x8000000 -- 0x8001234: MIS-MATCHED!\n"},
            {"type": "result", "message": "done", "payload": None},
        ]
    })

    with pytest.raises(GdbCommandError, match="does not match the ELF"):
        client.verify_flash("fw.elf")


def test_verify_flash_passes_when_every_section_matches():
    client = GdbClientManager()
    client.gdb = ScriptedGdb({
        "compare-sections": [
            {"type": "console", "payload": "Section .text, range 0x8000000 -- 0x8001234: matched.\n"},
            {"type": "result", "message": "done", "payload": None},
        ]
    })

    assert client.verify_flash("fw.elf")


def test_connect_seeds_the_halted_state_so_the_first_halt_sends_no_interrupt():
    # Attaching emits no *stopped (measured on hardware), so without the seed the
    # first halt_execution interrupts an already-halted target and the stub
    # queues a SIGINT that fires on the next resume (issue #22).
    client = GdbClientManager()
    client.gdb = ScriptedGdb({"-target-select": [
        {"type": "notify", "message": "thread-group-added", "payload": {"id": "i1"}},
    ]})

    client.connect()
    assert client.is_running() is False

    response = client.halt_execution()

    assert was_already_halted(response)
    assert not any(cmd.startswith("-exec-interrupt") for cmd, _ in client.gdb.commands)


def test_connect_does_not_override_a_state_gdb_already_reported():
    client = GdbClientManager()
    client.gdb = ScriptedGdb({"-target-select": [
        {"type": "notify", "message": "running", "payload": {"thread-id": "all"}},
    ]})

    client.connect()

    assert client.is_running() is True


def test_start_gdb_enables_mi_async_before_any_target_is_attached():
    """Without async, GDB accepts NO command while the target runs — not even
    -exec-interrupt, which never answers and wedges every command behind it
    (measured on an L151). GDB refuses the setting once an inferior exists, so it
    has to be the first thing sent after launching GDB."""
    import mcp_server.gdb_client as gdb_client_module

    launched = []

    class FakeController(ScriptedGdb):
        def __init__(self, command=None):
            super().__init__({})
            launched.append(command)

    original = gdb_client_module.GdbController
    gdb_client_module.GdbController = FakeController
    try:
        client = GdbClientManager()
        client.start_gdb()
    finally:
        gdb_client_module.GdbController = original

    assert launched, "GDB was not launched"
    sent = [cmd for cmd, _ in client.gdb.commands]
    assert sent and sent[0] == "-gdb-set mi-async on", f"first command was {sent[:2]}"


def test_start_gdb_survives_a_gdb_without_mi_async():
    import mcp_server.gdb_client as gdb_client_module

    class RefusingController(ScriptedGdb):
        def __init__(self, command=None):
            super().__init__({})

        def write(self, command, timeout_sec=1.0, **kw):
            super().write(command, timeout_sec=timeout_sec, **kw)
            raise RuntimeError("Undefined command: mi-async")

    original = gdb_client_module.GdbController
    gdb_client_module.GdbController = RefusingController
    try:
        client = GdbClientManager()
        client.start_gdb()          # must not raise
    finally:
        gdb_client_module.GdbController = original

    assert client.is_alive()
