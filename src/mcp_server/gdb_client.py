import time
from collections import OrderedDict

from pygdbmi.gdbcontroller import GdbController

from . import dwt
from .fault_analysis import FAULT_REGISTER_ADDRESSES
from .gdb_decode import (
    decode_backtrace,
    decode_breakpoints,
    decode_registers,
    decode_symbol_resolution,
    decode_variables,
)
from .mi_guard import GdbCommandError, ensure_ok, has_terminal_result
from .stop_event import ALREADY_HALTED_RECORD, parse_stop_event
from .timeouts import TimeoutConfig


def gdb_path(path: str) -> str:
    """Render a filesystem path as a GDB command argument.

    Two Windows hazards, both measured on hardware:

    * GDB/MI treats backslashes as escape characters, so ``C:\\proj\\fw.elf``
      silently degrades into a nonexistent file. Forward slashes work on every
      platform GDB runs on.
    * GDB splits a command on whitespace, so an unquoted ``C:/Program Files/...``
      is truncated to ``C:/Program``. Quoting is not optional on Windows, where
      Program Files and user names with spaces are everywhere.

    The result includes the surrounding quotes, so callers interpolate it
    directly: ``f"-file-exec-and-symbols {gdb_path(p)}"``.
    """
    normalized = str(path).replace("\\", "/").replace('"', '\\"')
    return f'"{normalized}"'


def gdb_expr(expr: str) -> str:
    """Render a C expression as a single GDB/MI command argument.

    GDB/MI splits a dash-command into argv on whitespace before dispatching it,
    so ``-data-evaluate-expression *(unsigned long *)0x08006000`` arrives as four
    arguments and GDB answers with a bare usage string instead of a value. Every
    cast, every ``sizeof``, and every multi-argument call was affected, and the
    resulting failure was reported to the agent as "target may be running or
    symbols may be missing" — a hardware diagnosis for a quoting bug (issue #38).

    Deliberately NOT ``gdb_path``: that helper rewrites backslashes to forward
    slashes, which is right for a Windows path and wrong for an expression, where
    a backslash is a C escape. Here backslashes are doubled so they survive GDB's
    unquoting intact.
    """
    escaped = str(expr).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def build_break_insert_command(location, condition=None, temporary=False, ignore_count=None):
    """Construct a `-break-insert` MI command with optional condition/temporary/ignore.

    Kept as a module function so the flag composition can be unit-tested without GDB.
    """
    parts = ["-break-insert"]
    if temporary:
        parts.append("-t")
    if condition is not None and str(condition).strip():
        parts.append(f'-c "{condition}"')
    if ignore_count is not None:
        parts.append(f"-i {int(ignore_count)}")
    parts.append(location)
    return " ".join(parts)


_SYMBOL_CACHE_MAX = 4096


class GdbClientManager:
    def __init__(self):
        self.gdb = None
        self.timeouts = TimeoutConfig()
        # PC -> symbol name cache for profiling; entries are only valid for the
        # currently loaded symbol table, so every symbol-table mutation clears it.
        self._symbol_cache: OrderedDict[int, str] = OrderedDict()
        # Whether the target is running, tracked from GDB's own *running/*stopped
        # async records. None = not known yet. Never probe a running target with a
        # command to find this out: the command is queued and only answered once
        # the target halts, which offsets every later response by one and wedges
        # the session (found on real L151 hardware).
        self._running: bool | None = None

    def start_gdb(self, gdb_path="arm-none-eabi-gdb"):
        if self.gdb:
            self.stop_gdb()
        self._symbol_cache.clear()
        self._running = None
        # Use mi3 for latest GDB machine interface features
        self.gdb = GdbController(command=[gdb_path, "--interpreter=mi3"])
        self._enable_async()

    def _enable_async(self):
        """Turn on MI async mode, which must happen BEFORE a target is attached.

        In the default all-stop synchronous mode GDB accepts no command at all
        while the target runs — not even -exec-interrupt, which simply never
        answers (measured on an L151: 5 s, zero records, and every later command
        wedged behind it). halt_execution can only work on a running target with
        async on. GDB rejects the setting once an inferior exists ("Cannot change
        this setting while the inferior is running"), hence doing it here.
        """
        try:
            self.execute_command("-gdb-set mi-async on", timeout_sec=self.timeouts.get("default"))
        except Exception:
            # An old GDB without mi-async still works for halted-target flows.
            pass

    def stop_gdb(self):
        if self.gdb:
            self.gdb.exit()
            self.gdb = None
        self._symbol_cache.clear()
        self._running = None

    def is_alive(self) -> bool:
        return self.gdb is not None

    def is_running(self) -> bool | None:
        """Last known run state from GDB's async records (None = unknown)."""
        return self._running

    def probe_target(self) -> bool:
        """Check that the target answers an MI evaluation.

        Only safe when the target is expected to be HALTED — on a running target
        the evaluation is not answered until it stops, and the late reply
        desynchronizes the pipe. Callers that merely need the run state should
        read ``is_running()`` instead.
        """
        if not self.gdb:
            return False
        try:
            self.read_register_value("$pc")
            return True
        except Exception:
            return False

    def _note_state(self, records):
        """Update the running/halted state from GDB's async notifications."""
        for record in records or []:
            message = record.get("message")
            if message == "running":
                self._running = True
            elif message == "stopped":
                self._running = False
        return records

    def execute_command(self, cmd: str, timeout_sec: float | None = None):
        if not self.gdb:
            raise RuntimeError("GDB is not running.")
        if timeout_sec is None:
            timeout_sec = self.timeouts.get("default")
        return self._note_state(self.gdb.write(cmd, timeout_sec=timeout_sec))

    def execute_cli_command(self, cmd: str, timeout_sec: float | None = None):
        return self.execute_command(cmd, timeout_sec=timeout_sec)

    def connect(self, host="localhost", port=3333):
        """Connects to the GDB Server.

        Attaching does not produce a ``*stopped`` record (measured against
        OpenOCD on an L151: only thread-group-added and console text), so the run
        state has to be seeded here. Halted is the right seed because GDB servers
        halt the target when GDB attaches — that is exactly why self_check's
        identity reads succeed immediately after connect. Any later
        ``*running``/``*stopped`` corrects it.

        Seeding matters: without it the first halt_execution has no state to
        consult, sends -exec-interrupt at an already-halted target, and the stub
        queues an interrupt that fires as a spurious SIGINT on the next resume —
        the symptom reported in issue #22.
        """
        resp = self.execute_command(
            f"-target-select extended-remote {host}:{port}", timeout_sec=self.timeouts.get("connect")
        )
        if self._running is None:
            self._running = False
        return resp

    def _execute_until_result(self, cmd: str, timeout_sec: float, poll_sec: float = 0.5):
        """Issue ``cmd`` and keep reading until GDB sends a terminal result record.

        ``pygdbmi.write()`` returns the first batch of records that happens to be
        available, not the batch containing the terminating ``^done``. For a quick
        command they are the same batch; for a long transfer they are not — the
        first batch is the ``+download`` progress stream and ``^done`` lands in a
        later read.

        That mattered because ``ensure_ok(..., require_result=True)`` demands the
        terminal record, so a perfectly healthy flash was reported as "did not
        report completion — treat it as NOT done". Measured on an STM32L151 over
        ST-Link: both the 24 KiB bootloader and the 31 KiB application were
        written correctly (vector tables and the boot descriptor read back byte
        exact) while the tool reported failure. Raising the ``download`` timeout
        from 60 s to 300 s changed nothing, which is what ruled out a slow flash
        and pointed at the read loop.

        Reporting a successful flash as failed is the mirror image of issue #21:
        it is safer than the reverse, but it still leaves the agent unable to tell
        a real write-protect fault from a bookkeeping artefact.
        """
        # The first read gets the full budget: pygdbmi treats timeout_sec as a
        # ceiling, not a dwell, so a command that finishes in one batch behaves
        # exactly as it did before this helper existed.
        deadline = time.monotonic() + timeout_sec
        records = list(self.execute_command(cmd, timeout_sec=timeout_sec) or [])
        while not has_terminal_result(records) and time.monotonic() < deadline:
            extra = self._read_async(poll_sec)
            if extra:
                records.extend(self._note_state(extra))
        return records

    def load_symbols(self, filepath: str):
        """Loads symbols/exec from an ELF/AXF without flashing the device.

        Symbols are per-GDB-session, so after a fresh connect (or recover_session)
        this is needed before symbol breakpoints resolve — unless load_firmware ran.
        """
        self._symbol_cache.clear()
        resp = self._execute_until_result(
            f"-file-exec-and-symbols {gdb_path(filepath)}", self.timeouts.get("symbols")
        )
        return ensure_ok(resp, f"load_symbols({filepath})", require_result=True)

    def load_firmware(self, filepath: str):
        """Loads symbols and flashes the firmware to the device.

        Both steps are checked against the raw MI records: a flash that errored
        (or never reported terminal completion) raises instead of pretending
        success (issue #21).
        """
        responses = []
        responses.extend(self.load_symbols(filepath))
        # Download (flash) the firmware to target memory
        download = self._execute_until_result("-target-download", self.timeouts.get("download"))
        responses.extend(ensure_ok(download, f"flash download({filepath})", require_result=True))
        return responses

    def reset_halt(self, command: str = "monitor reset halt"):
        """Resets the MCU and halts it. OpenOCD uses 'monitor reset halt'."""
        resp = self.execute_command(command, timeout_sec=self.timeouts.get("reset"))
        self._drain()
        # The first memory-AP access after a reset was observed on hardware to return
        # stale data (e.g. CPUID read back as 0x01000000). Issue one throwaway read of
        # a constant register to prime the AP so the next real read is coherent.
        try:
            self.read_memory("0xE000ED00", 4)
        except Exception:
            pass
        return resp

    def reset_run(self, command: str = "monitor reset run"):
        """Reset the MCU and let it run (Keil-style 'Reset and Run' after download)."""
        return self.execute_command(command, timeout_sec=self.timeouts.get("reset"))

    def set_adapter_speed(self, khz: int):
        """Set the SWD/JTAG adapter clock at runtime (kHz). Higher = faster flash/reads."""
        return self.execute_cli_command(f"monitor adapter speed {int(khz)}", timeout_sec=self.timeouts.get("monitor"))

    def _drain(self, rounds: int = 5):
        """Consume any pending async/stale GDB responses left in the buffer."""
        self._drain_collect(rounds)

    def _drain_collect(self, rounds: int = 5, per_read_sec: float = 0.1) -> list:
        """Like _drain, but returns the drained records for inspection."""
        records: list = []
        if not self.gdb:
            return records
        for _ in range(rounds):
            extra = self._read_async(per_read_sec)
            if not extra:
                break
            records.extend(extra)
        return self._note_state(records)

    def _read_async(self, timeout_sec: float):
        """One non-raising async read (older pygdbmi lacks the keyword)."""
        try:
            return self.gdb.get_gdb_response(timeout_sec=timeout_sec, raise_error_on_timeout=False)
        except TypeError:
            return self.gdb.get_gdb_response(timeout_sec=timeout_sec)

    def set_breakpoint(self, location: str, condition=None, temporary=False, ignore_count=None):
        # A breakpoint GDB refused but we reported as set is the worst kind of
        # false success: the agent then waits for a stop that can never come and
        # reads the timeout as "the code path was not reached" (issue #22).
        command = build_break_insert_command(location, condition, temporary, ignore_count)
        resp = self.execute_command(command)
        return ensure_ok(resp, f"set breakpoint at {location}")

    def delete_breakpoint(self, breakpoint_id: str):
        resp = self.execute_command(f"-break-delete {breakpoint_id}")
        return ensure_ok(resp, f"delete breakpoint {breakpoint_id}")

    def list_breakpoints_decoded(self):
        """Return breakpoints with hit counts (times each has actually been reached)."""
        return decode_breakpoints(self.execute_command("-break-list", timeout_sec=self.timeouts.get("breakpoint")))

    def continue_execution(self):
        resp = self.execute_command("-exec-continue", timeout_sec=self.timeouts.get("default"))
        return ensure_ok(resp, "continue")

    def halt_execution(self):
        """Interrupt the target — but only if it is actually running.

        Sending -exec-interrupt to an already-halted target leaves a pending
        interrupt in the remote stub that fires on the NEXT resume as a spurious
        immediate SIGINT (issue #22). So drain first and consult the run state
        GDB has already told us about; the state must never be established by
        sending a command, which on a running target would not be answered until
        it halts and would desynchronize every later response.
        """
        pending = self._drain_collect()
        if parse_stop_event(pending)["stopped"] or self._running is False:
            pending.append(ALREADY_HALTED_RECORD.copy())
            return pending
        resp = self.execute_command("-exec-interrupt", timeout_sec=self.timeouts.get("halt"))
        # Absorb the *stopped the interrupt produces so it cannot later be mistaken
        # for the stop of the NEXT resume.
        return pending + list(resp) + self._drain_collect(per_read_sec=0.3)

    def step_over(self):
        return ensure_ok(self.execute_command("-exec-next", timeout_sec=self.timeouts.get("step")),
                         "step over")

    def step_into(self):
        return ensure_ok(self.execute_command("-exec-step", timeout_sec=self.timeouts.get("step")),
                         "step into")

    def step_out(self):
        return ensure_ok(self.execute_command("-exec-finish", timeout_sec=self.timeouts.get("finish")),
                         "step out")

    def step_instruction(self, over: bool = False):
        cmd = "-exec-next-instruction" if over else "-exec-step-instruction"
        return ensure_ok(self.execute_command(cmd, timeout_sec=self.timeouts.get("step")),
                         "step instruction")

    def run_to_line(self, location: str):
        resp = self.execute_command(f"-exec-until {location}", timeout_sec=self.timeouts.get("run"))
        return ensure_ok(resp, f"run to {location}")

    def read_variable(self, name: str):
        return self.execute_command(f"-data-evaluate-expression {gdb_expr(name)}",
                                    timeout_sec=self.timeouts.get("evaluate"))

    def read_call_stack(self):
        return self.execute_command("-stack-list-frames")

    def select_frame(self, level: int):
        return self.execute_command(f"-stack-select-frame {int(level)}")

    def read_frame_variables(self, level: int | None = None):
        """List locals + arguments (with values) for a frame."""
        if level is not None:
            self.select_frame(level)
        return self.execute_command("-stack-list-variables --all-values", timeout_sec=self.timeouts.get("stack"))

    def read_frame_variables_decoded(self, level: int | None = None):
        return decode_variables(self.read_frame_variables(level))

    def read_frame_arguments(self, level: int | None = None):
        if level is not None:
            self.select_frame(level)
        return self.execute_command("-stack-list-arguments --all-values", timeout_sec=self.timeouts.get("stack"))

    def list_source(self, location: str | None = None, count: int = 10):
        """List source lines around a location (function, file:line, or *addr).

        Both listings are returned. ``list X`` prints the window around X and
        ``list +N`` continues past it; returning only the second answered a
        request for "source around main" with the lines AFTER main. That was
        invisible while the handler discarded the text entirely (issue #40).
        """
        responses = []
        if location:
            responses.extend(
                self.execute_cli_command(f"list {location}", timeout_sec=self.timeouts.get("source")))
        responses.extend(
            self.execute_cli_command(f"list +{int(count)}", timeout_sec=self.timeouts.get("source")))
        return responses

    def resolve_address(self, expr: str):
        """Map an address/expression to source line and nearest symbol."""
        responses = []
        responses.extend(self.execute_cli_command(f"info line *({expr})", timeout_sec=self.timeouts.get("symbols")))
        responses.extend(self.execute_cli_command(f"info symbol {expr}", timeout_sec=self.timeouts.get("symbols")))
        return responses

    def resolve_address_decoded(self, expr: str) -> dict:
        """Structured {resolved, symbol, offset, section, file, line} for an address."""
        return decode_symbol_resolution(self.resolve_address(expr))

    def read_core_registers(self):
        return self.execute_cli_command("info registers", timeout_sec=self.timeouts.get("registers"))

    def read_core_registers_decoded(self):
        """Return {name: hex} via the structured MI register queries."""
        names = self.execute_command("-data-list-register-names", timeout_sec=self.timeouts.get("registers"))
        values = self.execute_command("-data-list-register-values x", timeout_sec=self.timeouts.get("registers"))
        return decode_registers(names, values)

    def read_call_stack_decoded(self):
        return decode_backtrace(self.read_call_stack())

    def disassemble_around_pc(self, instructions: int = 8):
        return self.execute_cli_command(f"x/{instructions}i $pc", timeout_sec=self.timeouts.get("source"))

    def disassemble(self, location: str = "$pc", instructions: int = 8):
        return self.execute_cli_command(f"x/{int(instructions)}i {location}", timeout_sec=self.timeouts.get("source"))

    def list_functions(self, regex: str | None = None):
        cmd = f"info functions {regex}" if regex else "info functions"
        return self.execute_cli_command(cmd, timeout_sec=self.timeouts.get("symbol_list"))

    def list_variables(self, regex: str | None = None):
        cmd = f"info variables {regex}" if regex else "info variables"
        return self.execute_cli_command(cmd, timeout_sec=self.timeouts.get("symbol_list"))

    def lookup_type(self, expr: str):
        return self.execute_cli_command(f"ptype {expr}", timeout_sec=self.timeouts.get("symbols"))

    def sizeof(self, expr: str):
        return self.execute_command(f"-data-evaluate-expression {gdb_expr(f'sizeof({expr})')}",
                                    timeout_sec=self.timeouts.get("evaluate"))

    def address_of(self, symbol: str):
        return self.execute_command(f"-data-evaluate-expression {gdb_expr(f'&{symbol}')}",
                                    timeout_sec=self.timeouts.get("evaluate"))

    def capture_coredump(self, path: str):
        return self.execute_cli_command(f"generate-core-file {gdb_path(path)}", timeout_sec=self.timeouts.get("coredump"))

    def load_coredump(self, path: str):
        self._symbol_cache.clear()
        return self.execute_cli_command(f"core-file {gdb_path(path)}", timeout_sec=self.timeouts.get("coredump_load"))

    def verify_flash(self, file_path: str):
        responses = []
        responses.extend(self.load_symbols(file_path))
        compared = self.execute_cli_command("compare-sections", timeout_sec=self.timeouts.get("verify"))
        ensure_ok(compared, f"verify_flash({file_path})")
        # compare-sections reports differing sections as "MIS-MATCHED!" in console
        # text while the MI command itself succeeds — the exact shape of a false
        # ok:true (issue #21).
        mismatched = [
            record.get("payload").strip()
            for record in compared
            if isinstance(record.get("payload"), str) and "MIS-MATCHED" in record["payload"]
        ]
        if mismatched:
            raise GdbCommandError(
                f"verify_flash({file_path}) failed: target flash does not match the ELF — "
                + "; ".join(mismatched)
            )
        responses.extend(compared)
        return responses

    def enable_cycle_counter(self):
        for address, value in dwt.enable_cycle_counter_writes():
            self.write_typed_memory(hex(address), hex(value), width_bits=32)

    def read_cycle_counter(self) -> int:
        return dwt.read_cycle_count(self.read_word)

    def sample_pc(self, count: int = 64):
        return dwt.sample_pc(self.read_word, count)

    def enable_pc_sampling(self):
        for address, value in dwt.enable_pc_sampling_writes():
            self.write_typed_memory(hex(address), hex(value), width_bits=32)

    def symbolize_pc(self, pc: int) -> str:
        """Best-effort function name for a PC via `info symbol` (e.g. 'vTaskDelay')."""
        key = pc & 0xFFFFFFFF
        cached = self._symbol_cache.get(key)
        if cached is not None:
            self._symbol_cache.move_to_end(key)
            return cached
        name = self._symbolize_pc_uncached(key)
        self._symbol_cache[key] = name
        if len(self._symbol_cache) > _SYMBOL_CACHE_MAX:
            self._symbol_cache.popitem(last=False)
        return name

    def _symbolize_pc_uncached(self, pc: int) -> str:
        try:
            responses = self.execute_cli_command(
                f"info symbol 0x{pc:08x}", timeout_sec=self.timeouts.get("evaluate"))
        except Exception:
            return ""
        # The real answer comes as 'console' stream records; 'log' records are just the
        # command echo (e.g. "info symbol 0x...") — skip those.
        for record in responses:
            if record.get("type") != "console":
                continue
            payload = record.get("payload")
            if isinstance(payload, str) and payload.strip():
                text = payload.strip()
                if text.startswith("No symbol"):
                    return ""
                # "vTaskDelay + 4 in section .text" -> "vTaskDelay"
                return text.split(" + ")[0].split(" in section")[0].strip()
        return ""

    def profile_pc(self, count: int = 128, enable: bool = True):
        """Statistical PC-sample profiler -> symbolized hot-spot histogram."""
        if enable:
            self.enable_pc_sampling()
        samples = dwt.sample_pc(self.read_word, count)
        return dwt.build_pc_profile(samples, self.symbolize_pc)

    def read_register_value(self, expr: str) -> int:
        """Evaluate a register/convenience expression (e.g. '$lr', '$msp') to an int."""
        response = self.execute_command(f"-data-evaluate-expression {gdb_expr(expr)}",
                                        timeout_sec=self.timeouts.get("evaluate"))
        for record in response:
            payload = record.get("payload")
            if isinstance(payload, dict) and payload.get("value") is not None:
                value = payload["value"].split()[0].strip()
                return int(value, 0)
        raise ValueError(f"Could not evaluate register expression: {expr}")

    def read_word(self, address) -> int:
        """Read a single 32-bit word at an address (int or hex string)."""
        addr = address if isinstance(address, str) else hex(address)
        return self._extract_first_memory_word(self.read_typed_memory(addr, width_bits=32, count=1))

    def read_fault_registers(self):
        registers = {}
        for name, address in FAULT_REGISTER_ADDRESSES.items():
            response = self.read_typed_memory(address, width_bits=32, count=1)
            registers[name] = self._extract_first_memory_word(response)
        return registers

    def set_watchpoint(self, variable_or_address: str, access_type: str = "rw"):
        if access_type == "r":
            cmd = f"rwatch {variable_or_address}"
        elif access_type == "w":
            cmd = f"watch {variable_or_address}"
        else:
            cmd = f"awatch {variable_or_address}"
        # Measured on hardware: `watch 0x20000010` is refused with "Cannot watch
        # constant value" while the tool still answered "Watchpoint set".
        return ensure_ok(self.execute_command(cmd), f"watch {variable_or_address}")

    def read_memory(self, address: str, length: int):
        # The address is an expression too ("&buf", "main + 4"), and
        # -data-read-memory-bytes argv-splits exactly like -data-evaluate-expression.
        return self.execute_command(
            f"-data-read-memory-bytes {gdb_expr(address)} {length}", timeout_sec=self.timeouts.get("memory")
        )

    def write_memory(self, address: str, value: str):
        return self.write_typed_memory(address, value, width_bits=32)

    def read_typed_memory(self, address: str, width_bits: int = 32, count: int = 1):
        self._validate_memory_width(width_bits)
        if count < 1:
            raise ValueError("count must be >= 1")
        byte_count = (width_bits // 8) * count
        return self.read_memory(address, byte_count)

    def write_typed_memory(self, address: str, value: str, width_bits: int = 32):
        self._validate_memory_width(width_bits)
        c_type = {
            8: "uint8_t",
            16: "uint16_t",
            32: "uint32_t",
            64: "uint64_t",
        }[width_bits]
        resp = self.execute_cli_command(f"set {{{c_type}}}{address} = {value}")
        return ensure_ok(resp, f"write to {address}")

    def get_responses(self, timeout_sec: float = 0.1):
        """Polls for asynchronous events from GDB (e.g. hit breakpoint)."""
        if not self.gdb:
            return []
        return self.gdb.get_gdb_response(timeout_sec=timeout_sec)

    def _wait_for_stop(self, initial, timeout_sec: float):
        """Drain async records until a `*stopped` arrives or timeout elapses.

        Windows pipe polling has been observed to deliver the `*stopped` of a hit
        breakpoint late — it only surfaced on a later call (issue #22), leaving
        the agent convinced the path was never reached. So before declaring a
        timeout, read once more with a longer per-read window to catch a
        straggler. This deliberately does NOT probe the target with a command:
        on a still-running target that command is only answered once it halts,
        and the late reply desynchronizes every later response (seen wedging a
        real L151 session).
        """
        records = list(initial or [])
        event = parse_stop_event(records)
        if event["stopped"]:
            return event, records

        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            more = self._read_async(0.2)
            if more:
                records.extend(self._note_state(more))
                event = parse_stop_event(records)
                if event["stopped"]:
                    return event, records

        straggler = self._drain_collect(rounds=2, per_read_sec=0.5)
        if straggler:
            records.extend(straggler)
        return parse_stop_event(records), records

    def run_and_wait(self, timeout_sec: float | None = None):
        """Resume the target and report why it next stops (or a timeout)."""
        if not self.gdb:
            raise RuntimeError("GDB is not running.")
        if timeout_sec is None:
            timeout_sec = self.timeouts.get("run")
        # Stale async records (e.g. the stop of a previous halt) must not
        # masquerade as the NEW stop this resume is waiting for.
        self._drain()
        try:
            initial = self.gdb.write("-exec-continue", timeout_sec=0.2, raise_error_on_timeout=False)
        except TypeError:
            initial = self.gdb.write("-exec-continue", timeout_sec=0.2)
        self._note_state(initial)
        ensure_ok(initial, "run_and_wait: continue")
        event, records = self._wait_for_stop(initial, timeout_sec)
        event["raw_response"] = records
        return event

    def wait_for_stop(self, timeout_sec: float | None = None):
        """Wait for the next stop event without resuming the target."""
        if not self.gdb:
            raise RuntimeError("GDB is not running.")
        if timeout_sec is None:
            timeout_sec = self.timeouts.get("run")
        event, records = self._wait_for_stop([], timeout_sec)
        event["raw_response"] = records
        return event

    def _validate_memory_width(self, width_bits: int):
        if width_bits not in (8, 16, 32, 64):
            raise ValueError("width_bits must be one of 8, 16, 32, or 64")

    @staticmethod
    def _le_hex_word_to_int(contents: str) -> int:
        """Decode the first 32-bit word of a GDB/MI byte string.

        `-data-read-memory-bytes` returns bytes in target memory order, which is
        little-endian on Cortex-M. The first word is the first 8 hex chars, read
        low byte first (e.g. "35640110" -> 0x10016435).
        """
        word = contents.strip()[:8]
        return int.from_bytes(bytes.fromhex(word), "little")

    def _extract_first_memory_word(self, response):
        # GDB can emit async connection/stop records before the actual memory result.
        # Prefer structured MI memory payloads across the whole response so a console
        # line such as "0x08000108 in ?? ()" is never mistaken for the read value.
        for record in response:
            payload = record.get("payload")
            if isinstance(payload, dict):
                memory = payload.get("memory")
                if memory and isinstance(memory, list):
                    contents = memory[0].get("contents")
                    if contents:
                        return self._le_hex_word_to_int(contents)
                contents = payload.get("contents")
                if contents:
                    return self._le_hex_word_to_int(contents)
        for record in response:
            payload = record.get("payload")
            if isinstance(payload, str) and "0x" in payload:
                token = payload.split("0x", 1)[1].split()[0].rstrip(":,")
                return int(token, 16)
        return 0
