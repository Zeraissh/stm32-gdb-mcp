import inspect
import re

import pytest
from conftest import FakeGdb

import mcp_server.gdb_client as gdb_client_module
from mcp_server.gdb_client import GdbClientManager
from mcp_server.mi_guard import GdbCommandError
from mcp_server.stop_event import was_already_halted, was_already_running
from mcp_server.timeouts import DEFAULTS


def test_load_symbols_uses_file_exec_and_symbols_without_download():
    client = GdbClientManager()
    client.gdb = FakeGdb()

    client.load_symbols("fw.axf")

    assert client.gdb.commands == [('-file-exec-and-symbols "fw.axf"', 2.0)]


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
    assert any('-data-read-memory-bytes "0xE000ED00" 4' in c for c in commands)


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
        ('-file-exec-and-symbols "fw.axf"', 7.0),
        ("-exec-next", 3.5),
        ("-exec-until main.c:10", 42.0),
        ('-file-exec-and-symbols "fw.axf"', 7.0),
        ("compare-sections", 99.0),
    ]


def test_write_typed_memory_writes_raw_bytes_in_target_order():
    client = GdbClientManager()
    client.gdb = FakeGdb()

    client.write_typed_memory("0x20000000", "0x12345678", width_bits=32)

    # Contents go out in target memory order (little-endian on Cortex-M), and the
    # address is quoted because -data-write-memory-bytes argv-splits like the read side.
    assert client.gdb.commands == [
        ('-data-write-memory-bytes "0x20000000" 78563412', 2.0)
    ]


@pytest.mark.parametrize(("width_bits", "value", "contents"), [
    (8, "0xff", "ff"),
    (16, "0x1234", "3412"),
    (32, "0x1", "01000000"),          # a small value still occupies the full width
    (64, "0x1122334455667788", "8877665544332211"),
    (32, "-1", "ffffffff"),           # negatives truncate like set {uint32_t} did
    (32, "4096", "00100000"),         # decimal literals too
])
def test_write_typed_memory_encodes_each_width(width_bits, value, contents):
    client = GdbClientManager()
    client.gdb = FakeGdb()

    client.write_typed_memory("0x40023834", value, width_bits=width_bits)

    assert client.gdb.commands == [
        (f'-data-write-memory-bytes "0x40023834" {contents}', 2.0)
    ]


def test_encode_memory_bytes_honors_byte_order():
    # The only knob a big-endian target would need: width handling is identical,
    # the byte order flips. Kept explicit so the two directions can't drift apart.
    assert gdb_client_module.encode_memory_bytes(0x12345678, 32, "little") == "78563412"
    assert gdb_client_module.encode_memory_bytes(0x12345678, 32, "big") == "12345678"
    assert gdb_client_module.encode_memory_bytes(0x1234, 16, "big") == "1234"
    assert gdb_client_module.TARGET_BYTE_ORDER == "little"


def test_write_typed_memory_succeeds_with_no_symbol_table_loaded():
    # issue #51: `set {uint32_t}0x40023834 = ...` needs an ELF for the uint32_t
    # typedef, so poking a peripheral on a bare board failed with "No symbol table
    # is loaded" — while reading the very same address worked. A numeric write must
    # never touch the expression evaluator.
    client = GdbClientManager()
    client.gdb = ScriptedGdb({
        "set {": [
            {"type": "log", "payload": "No symbol table is loaded.  Use the \"file\" command."},
            {"type": "result", "message": "done", "payload": None},
        ]
    })

    client.write_typed_memory("0x40023834", "0x1000000", width_bits=32)

    assert client.gdb.commands == [
        ('-data-write-memory-bytes "0x40023834" 00000001', 2.0)
    ]


def test_write_typed_memory_still_evaluates_symbolic_values():
    # A value that is not a literal genuinely needs the symbol table, so it keeps
    # going through GDB's expression evaluator rather than being encoded here.
    client = GdbClientManager()
    client.gdb = FakeGdb()

    client.write_typed_memory("&g_flags", "g_seed + 1", width_bits=16)

    assert client.gdb.commands == [
        ("set {uint16_t}&g_flags = g_seed + 1", 1.0)
    ]


def test_read_typed_memory_reads_byte_count_for_width_and_count():
    client = GdbClientManager()
    client.gdb = FakeGdb()

    client.read_typed_memory("0x20000000", width_bits=16, count=4)

    assert client.gdb.commands == [
        ('-data-read-memory-bytes "0x20000000" 8', 2.0)  # routed through the 'memory' timeout
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
    # _execute_until_result waits out the download timeout for the terminal record
    # that never comes; without a short deadline this one test costs 60 s.
    client.timeouts.set({"download": 0.2})

    with pytest.raises(GdbCommandError, match="did not report completion"):
        client.load_firmware("fw.elf")


def test_symbolic_memory_write_raises_when_no_symbol_table_is_loaded():
    # A symbolic value is the one write that legitimately needs symbols, and GDB's
    # complaint about their absence must still surface instead of reporting ok:true.
    client = GdbClientManager()
    client.gdb = ScriptedGdb({
        "set {": [
            {"type": "log", "payload": "No symbol table is loaded.  Use the \"file\" command."},
            {"type": "result", "message": "done", "payload": None},
        ]
    })

    with pytest.raises(GdbCommandError, match="No symbol table"):
        client.write_typed_memory("0x20000000", "g_counter", width_bits=32)


def test_windows_backslash_paths_are_normalized_for_gdb():
    # GDB/MI eats backslashes as escapes, so C:\proj\fw.elf silently became a
    # nonexistent path that still reported ok:true (issue #22, second half).
    client = GdbClientManager()
    client.gdb = FakeGdb()

    client.load_symbols(r"C:\proj\build\fw.elf")

    assert client.gdb.commands[0][0] == '-file-exec-and-symbols "C:/proj/build/fw.elf"'


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


def test_verify_flash_keeps_the_comparison_records_as_evidence():
    # A passing verify must carry the compare-sections records in its response:
    # without them a real verification is indistinguishable from one that never
    # compared anything (the tool layer surfaces this as raw_response).
    client = GdbClientManager()
    client.gdb = ScriptedGdb({
        "compare-sections": [
            {"type": "console", "payload": "Section .text, range 0x8000000 -- 0x8001234: matched.\n"},
            {"type": "result", "message": "done", "payload": None},
        ]
    })

    responses = client.verify_flash("fw.elf")

    payloads = [r.get("payload") for r in responses if isinstance(r.get("payload"), str)]
    assert any("matched." in p for p in payloads)


def test_compare_sections_report_picks_only_the_mismatched_sections():
    # Parser precision: a mixed report must yield exactly the MIS-MATCHED entries,
    # never the matched ones — a false positive here would tell an agent its
    # symbols are wrong when they are fine.
    client = GdbClientManager()
    client.gdb = ScriptedGdb({
        "compare-sections": [
            {"type": "console", "payload": "Section .text, range 0x8000000 -- 0x8001234: matched.\n"},
            {"type": "console", "payload": "Section .data, range 0x20000000 -- 0x20000100: MIS-MATCHED!\n"},
            {"type": "console", "payload": "Section .rodata, range 0x8001234 -- 0x8002000: matched.\n"},
            {"type": "console", "payload": "Section .bss, range 0x20000100 -- 0x20000200: MIS-MATCHED!\n"},
            {"type": "result", "message": "done", "payload": None},
        ]
    })

    report = client.compare_sections_report()

    assert report["checked"] is True
    assert report["reason"] is None
    assert len(report["mismatched"]) == 2
    assert all("MIS-MATCHED" in entry for entry in report["mismatched"])
    assert any(".data" in entry for entry in report["mismatched"])
    assert any(".bss" in entry for entry in report["mismatched"])
    assert not any("matched." in entry and "MIS-MATCHED" not in entry for entry in report["mismatched"])


def test_compare_sections_report_never_raises_when_the_command_blows_up():
    # load_symbols calls this on every load; a throw here would turn a benign
    # "symbols loaded for a not-yet-flashed board" into a hard tool failure.
    class ExplodingGdb:
        def write(self, command, timeout_sec=1.0):
            raise RuntimeError("target is not connected")

    client = GdbClientManager()
    client.gdb = ExplodingGdb()

    report = client.compare_sections_report()

    assert report["checked"] is False
    assert report["mismatched"] == []
    assert "not connected" in report["reason"]
    assert report["records"] == []


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


def test_set_watchpoint_raises_when_gdb_refuses_the_expression():
    # Measured on an L151: `watch 0x20000010` is refused with "Cannot watch
    # constant value" while the tool still answered "Watchpoint set".
    client = GdbClientManager()
    client.gdb = ScriptedGdb({"watch": [
        {"type": "log", "payload": "Cannot watch constant value `0x20000010'.\n"},
        {"type": "result", "message": "error", "payload": {"msg": "Cannot watch constant value `0x20000010'."}},
    ]})

    with pytest.raises(GdbCommandError, match="Cannot watch constant value"):
        client.set_watchpoint("0x20000010", access_type="w")


def test_set_breakpoint_raises_when_gdb_rejects_the_location():
    # A breakpoint reported as set but never created makes the agent wait for a
    # stop that cannot come, and read the timeout as "path not reached" (#22).
    client = GdbClientManager()
    client.gdb = ScriptedGdb({"-break-insert": [
        {"type": "result", "message": "error", "payload": {"msg": "Function \"nope\" not defined."}},
    ]})

    with pytest.raises(GdbCommandError, match="not defined"):
        client.set_breakpoint("nope")


def test_step_raises_when_the_target_refuses_to_step():
    client = GdbClientManager()
    client.gdb = ScriptedGdb({"-exec-next": [
        {"type": "result", "message": "error", "payload": {"msg": "The program is not being run."}},
    ]})

    with pytest.raises(GdbCommandError, match="not being run"):
        client.step_over()


def test_gdb_path_quotes_and_normalizes_windows_paths():
    from mcp_server.gdb_client import gdb_path

    # Backslashes are MI escapes; an unquoted space truncates the filename.
    assert gdb_path(r"C:\proj\fw.elf") == '"C:/proj/fw.elf"'
    assert gdb_path(r"C:\Program Files\app\fw.elf") == '"C:/Program Files/app/fw.elf"'
    assert gdb_path("/home/dev/fw.elf") == '"/home/dev/fw.elf"'
    assert gdb_path('weird"name.elf') == '"weird\\"name.elf"'


def test_load_symbols_quotes_a_path_containing_spaces():
    # Measured on hardware: a real ELF under a path with spaces failed to load
    # because GDB truncated the filename at the first space.
    client = GdbClientManager()
    client.gdb = ScriptedGdb({})

    client.load_symbols(r"C:\Program Files\fw.elf")

    sent = [cmd for cmd, _ in client.gdb.commands]
    assert sent == ['-file-exec-and-symbols "C:/Program Files/fw.elf"'], sent


def test_coredump_paths_are_quoted_too():
    client = GdbClientManager()
    client.gdb = ScriptedGdb({})

    client.capture_coredump(r"C:\dumps\my run\core.bin")
    client.load_coredump(r"C:\dumps\my run\core.bin")

    sent = [cmd for cmd, _ in client.gdb.commands]
    assert sent == [
        'generate-core-file "C:/dumps/my run/core.bin"',
        'core-file "C:/dumps/my run/core.bin"',
    ], sent


class SplitBatchGdb:
    """pygdbmi stand-in whose terminal ^done arrives in a LATER read.

    This is what a real flash looks like: write() returns the +download progress
    stream, and the ^done that terminates the operation only shows up on a
    subsequent get_gdb_response(). Observed against OpenOCD + ST-Link on an
    STM32L151, where a fully successful 24 KiB flash was reported as
    "did not report completion".
    """

    def __init__(self, batches_before_result=2):
        self.commands = []
        self._remaining = batches_before_result

    def write(self, command, timeout_sec=1.0):
        self.commands.append((command, timeout_sec))
        # Progress records only - deliberately no terminal result yet.
        return [{"type": "output", "message": "download", "payload": "+download,{section=\".text\"}"}]

    def get_gdb_response(self, timeout_sec=0.1, raise_error_on_timeout=False):
        if self._remaining > 0:
            self._remaining -= 1
            return [{"type": "output", "message": "download", "payload": "+download,{section=\".data\"}"}]
        return [{"type": "result", "message": "done", "payload": None}]


def test_flash_download_waits_for_terminal_result_across_reads():
    """A flash whose ^done lands in a later batch must not be reported as failed."""
    manager = GdbClientManager()
    manager.gdb = SplitBatchGdb()

    records = manager.load_firmware("fw.axf")

    assert any(r.get("type") == "result" and r.get("message") == "done" for r in records)
    assert any(cmd == "-target-download" for cmd, _ in manager.gdb.commands)


def test_flash_download_still_fails_when_no_result_ever_arrives():
    """The issue #21 guard must survive: a genuinely stalled flash still raises."""

    class NeverCompletes(SplitBatchGdb):
        def get_gdb_response(self, timeout_sec=0.1, raise_error_on_timeout=False):
            return [{"type": "output", "message": "download", "payload": "+download,{}"}]

    manager = GdbClientManager()
    manager.gdb = NeverCompletes()
    manager.timeouts.set({"symbols": 0.2, "download": 0.2})

    with pytest.raises(GdbCommandError, match="did not report completion"):
        manager.load_firmware("fw.axf")


# --- issue #38: expressions must reach GDB/MI as ONE argument ---
#
# GDB/MI splits a dash-command into argv on whitespace, so an unquoted
# "*(unsigned long *)0x08006000" arrived as four arguments and GDB answered
# "-data-evaluate-expression: Usage: -data-evaluate-expression expression".
# Every cast, sizeof, and multi-argument call was unusable, and nothing in the
# suite asserted the emitted MI string — which is why it survived four releases.

def _only_command(client):
    assert len(client.gdb.commands) == 1, client.gdb.commands
    return client.gdb.commands[0][0]


def test_read_variable_quotes_an_expression_containing_spaces():
    client = GdbClientManager()
    client.gdb = FakeGdb()

    client.read_variable("*(unsigned long *)0x08006000")

    assert _only_command(client) == (
        '-data-evaluate-expression "*(unsigned long *)0x08006000"')


def test_read_variable_quotes_a_multi_argument_call():
    client = GdbClientManager()
    client.gdb = FakeGdb()

    client.read_variable("Boot_IsAppVectorValid(0x08006000, backupSize)")

    assert _only_command(client) == (
        '-data-evaluate-expression "Boot_IsAppVectorValid(0x08006000, backupSize)"')


def test_sizeof_and_address_of_quote_the_whole_expression():
    client = GdbClientManager()
    client.gdb = FakeGdb()

    client.sizeof("struct boot_record")
    client.address_of("g_state")

    assert client.gdb.commands[0][0] == '-data-evaluate-expression "sizeof(struct boot_record)"'
    assert client.gdb.commands[1][0] == '-data-evaluate-expression "&g_state"'


def test_read_register_value_quotes_its_expression():
    client = GdbClientManager()
    client.gdb = FakeGdb([{"type": "result", "message": "done", "payload": {"value": "0x20004fd0"}}])

    assert client.read_register_value("$sp") == 0x20004FD0
    assert _only_command(client) == '-data-evaluate-expression "$sp"'


def test_read_memory_quotes_an_address_expression():
    client = GdbClientManager()
    client.gdb = FakeGdb()

    client.read_memory("&g_buffer[0]", 8)

    assert _only_command(client) == '-data-read-memory-bytes "&g_buffer[0]" 8'


def test_expression_quoting_preserves_backslashes_instead_of_pathifying_them():
    # gdb_path rewrites \ -> / because a path needs it; an expression does not,
    # where a backslash is a C escape. Sharing that helper would corrupt char literals.
    client = GdbClientManager()
    client.gdb = FakeGdb()

    client.read_variable(r"c == '\n'")

    # The backslash survives as a backslash, doubled so GDB's unquoting yields
    # exactly one back — not rewritten to a forward slash the way a path would be.
    assert _only_command(client) == r'''-data-evaluate-expression "c == '\\n'"'''
    assert "/" not in _only_command(client)


def test_expression_quoting_escapes_embedded_quotes():
    client = GdbClientManager()
    client.gdb = FakeGdb()

    client.read_variable('strcmp(name, "abc")')

    assert _only_command(client) == '-data-evaluate-expression "strcmp(name, \\"abc\\")"'


# --- issue #37: a failed register read must not be returned as target state ---

def _register_gdb(values):
    class RegGdb:
        def __init__(self):
            self.commands = []

        def write(self, command, timeout_sec=1.0):
            self.commands.append((command, timeout_sec))
            if "register-names" in command:
                return [{"type": "result", "message": "done",
                         "payload": {"register-names": ["r0", "sp", "pc", "xpsr"]}}]
            return [{"type": "result", "message": "done", "payload": {"register-values": values}}]

        def get_gdb_response(self, timeout_sec=0.1, raise_error_on_timeout=False):
            return []

    return RegGdb()


def test_read_core_registers_decoded_raises_on_an_all_zero_set():
    client = GdbClientManager()
    client.gdb = _register_gdb([
        {"number": "0", "value": "0x0"}, {"number": "1", "value": "0x0"},
        {"number": "2", "value": "0x0"}, {"number": "3", "value": "0x0"},
    ])

    with pytest.raises(GdbCommandError) as excinfo:
        client.read_core_registers_decoded()

    assert "implausible" in str(excinfo.value)
    assert "Thumb" in str(excinfo.value)


def test_read_core_registers_decoded_returns_a_real_halted_set():
    client = GdbClientManager()
    client.gdb = _register_gdb([
        {"number": "0", "value": "0x10"}, {"number": "1", "value": "0x20004fd0"},
        {"number": "2", "value": "0x8000456"}, {"number": "3", "value": "0x61000000"},
    ])

    assert client.read_core_registers_decoded() == {
        "r0": "0x10", "sp": "0x20004fd0", "pc": "0x8000456", "xpsr": "0x61000000",
    }


def test_read_core_registers_decoded_raises_on_a_gdb_error_instead_of_decoding_to_empty():
    class ErrGdb:
        def write(self, command, timeout_sec=1.0):
            return [{"type": "result", "message": "error",
                     "payload": {"msg": "Could not read registers; remote failure reply '0E'"}}]

        def get_gdb_response(self, timeout_sec=0.1, raise_error_on_timeout=False):
            return []

    client = GdbClientManager()
    client.gdb = ErrGdb()

    with pytest.raises(GdbCommandError) as excinfo:
        client.read_core_registers_decoded()

    assert "remote failure reply" in str(excinfo.value)


# --- issue #40: resolve_address must return GDB's answer, not echo the input ---

def test_resolve_address_decoded_returns_symbol_offset_and_source():
    class SymGdb:
        def write(self, command, timeout_sec=1.0):
            if "info line" in command:
                return [{"type": "console", "payload":
                         'Line 412 of "boot.c" starts at address 0x8000c74 '
                         "<Boot_ValidateStaging+148> and ends at 0x8000c78 <Boot_WriteState>.\n"}]
            return [{"type": "console", "payload": "Boot_ValidateStaging + 148 in section .text\n"}]

        def get_gdb_response(self, timeout_sec=0.1, raise_error_on_timeout=False):
            return []

    client = GdbClientManager()
    client.gdb = SymGdb()

    out = client.resolve_address_decoded("0x08000c74")

    assert out["resolved"] is True
    assert out["symbol"] == "Boot_ValidateStaging"
    assert out["offset"] == 148
    assert out["file"] == "boot.c"
    assert out["line"] == 412


# --- issue #34: GDB's charset conversion must be out of the loop ---

def test_start_gdb_pins_the_charset_so_char_reads_cannot_fail(monkeypatch):
    # On a host whose iconv cannot serve the locale, EVERY char read came back as
    # "0 '<error reading variable: Converting character sets: Invalid argument.>'".
    client = GdbClientManager()
    fake = FakeGdb()
    monkeypatch.setattr(gdb_client_module, "GdbController", lambda command: fake)

    client.start_gdb()

    commands = [c[0] for c in fake.commands]
    assert "-gdb-set charset ASCII" in commands


def test_a_gdb_that_rejects_the_charset_setting_does_not_break_startup(monkeypatch):
    class GrumpyGdb(FakeGdb):
        def write(self, command, timeout_sec=1.0):
            super().write(command, timeout_sec)
            if "charset" in command:
                raise RuntimeError("Undefined command")
            return [{"type": "result", "message": "done", "payload": None}]

    fake = GrumpyGdb()
    monkeypatch.setattr(gdb_client_module, "GdbController", lambda command: fake)

    GdbClientManager().start_gdb()  # must not raise


# --- issue #33: resume must be as idempotent as halt already is ---

def test_continue_execution_is_a_no_op_when_the_target_is_already_running():
    client = GdbClientManager()
    client.gdb = FakeGdb()
    client._running = True

    resp = client.continue_execution()

    assert was_already_running(resp)
    assert client.gdb.commands == []  # no -exec-continue, so no "not halted" error


def test_continue_execution_resumes_a_halted_target():
    client = GdbClientManager()
    client.gdb = FakeGdb()
    client._running = False

    client.continue_execution()

    assert [c[0] for c in client.gdb.commands] == ["-exec-continue"]


def test_continue_execution_still_resumes_when_the_state_is_unknown():
    client = GdbClientManager()
    client.gdb = FakeGdb()
    client._running = None

    client.continue_execution()

    assert [c[0] for c in client.gdb.commands] == ["-exec-continue"]


# --- issue #42: erase a flash range through the server's own session ---

def test_flash_erase_pads_to_the_drivers_sector_boundaries():
    client = GdbClientManager()
    client.gdb = FakeGdb()

    client.flash_erase(0x08016000, 0x1000)

    # 'pad' is what stops "address range ... is not sector-aligned"; the sector
    # size belongs to the OpenOCD driver, not to a table in this server.
    assert client.gdb.commands == [("monitor flash erase_address pad 0x8016000 4096", 30.0)]


def test_flash_erase_raises_when_the_erase_reports_an_error():
    client = GdbClientManager()
    client.gdb = FakeGdb([
        {"type": "console", "payload": "Error: failed to erase memory\n"},
        {"type": "result", "message": "done", "payload": None},
    ])

    with pytest.raises(GdbCommandError) as excinfo:
        client.flash_erase(0x08016000, 4096)

    assert "failed to erase memory" in str(excinfo.value)


def test_flash_erase_raises_without_a_terminal_result_record():
    # Same guard as the flash download: a transfer that never reported completion
    # must not read as done.
    client = GdbClientManager()
    client.gdb = FakeGdb([{"type": "console", "payload": "erasing...\n"}])
    client.timeouts.set({"erase": 0.2})  # the poll loop runs to the deadline

    with pytest.raises(GdbCommandError):
        client.flash_erase(0x08016000, 4096)


def test_flash_erase_rejects_a_zero_length():
    client = GdbClientManager()
    client.gdb = FakeGdb()

    with pytest.raises(ValueError):
        client.flash_erase(0x08016000, 0)


# --- review findings: things the first pass of these fixes got wrong ---

def test_continue_execution_resumes_a_target_that_stopped_on_its_own():
    # _running only flips to False when a *stopped is READ off the pipe, so it is
    # stale the instant the target hits a breakpoint. A guard that trusted it
    # unread would skip the one resume that was actually needed.
    class StoppedGdb(FakeGdb):
        def __init__(self):
            super().__init__()
            self._async = [{"type": "notify", "message": "stopped",
                            "payload": {"reason": "breakpoint-hit", "frame": {"addr": "0x8000456"}}}]

        def get_gdb_response(self, timeout_sec=0.1, raise_error_on_timeout=False):
            pending, self._async = self._async, []
            return pending

    client = GdbClientManager()
    client.gdb = StoppedGdb()
    client._running = True  # stale: the *stopped has not been read yet

    resp = client.continue_execution()

    assert [c[0] for c in client.gdb.commands] == ["-exec-continue"]
    assert not was_already_running(resp)


def test_continue_execution_still_short_circuits_a_genuinely_running_target():
    client = GdbClientManager()
    client.gdb = FakeGdb()
    client._running = True

    resp = client.continue_execution()

    assert was_already_running(resp)
    assert client.gdb.commands == []


def test_list_source_returns_the_window_around_the_location_not_only_after_it():
    class ListGdb:
        def __init__(self):
            self.commands = []

        def write(self, command, timeout_sec=1.0):
            self.commands.append((command, timeout_sec))
            if command.startswith("list main"):
                return [{"type": "console", "payload": "40\tint main(void) {\n"}]
            return [{"type": "console", "payload": "50\t  next_window();\n"}]

        def get_gdb_response(self, timeout_sec=0.1, raise_error_on_timeout=False):
            return []

    client = GdbClientManager()
    client.gdb = ListGdb()

    records = client.list_source("main", 10)

    # Returning only the second listing answered "source around main" with the
    # lines AFTER main.
    text = "".join(r["payload"] for r in records)
    assert "int main(void)" in text
    assert "next_window()" in text


