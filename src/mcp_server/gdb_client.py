import time

from pygdbmi.gdbcontroller import GdbController

from . import dwt
from .fault_analysis import FAULT_REGISTER_ADDRESSES
from .gdb_decode import decode_backtrace, decode_registers, decode_variables
from .stop_event import parse_stop_event
from .timeouts import TimeoutConfig


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


class GdbClientManager:
    def __init__(self):
        self.gdb = None
        self.timeouts = TimeoutConfig()
        
    def start_gdb(self, gdb_path="arm-none-eabi-gdb"):
        if self.gdb:
            self.stop_gdb()
        # Use mi3 for latest GDB machine interface features
        self.gdb = GdbController(command=[gdb_path, "--interpreter=mi3"])
        
    def stop_gdb(self):
        if self.gdb:
            self.gdb.exit()
            self.gdb = None

    def is_alive(self) -> bool:
        return self.gdb is not None

    def probe_target(self) -> bool:
        """Lightweight check that the target still answers an MI evaluation."""
        if not self.gdb:
            return False
        try:
            self.read_register_value("$pc")
            return True
        except Exception:
            return False

    def execute_command(self, cmd: str, timeout_sec: float = 1.0):
        if not self.gdb:
            raise RuntimeError("GDB is not running.")
        return self.gdb.write(cmd, timeout_sec=timeout_sec)

    def execute_cli_command(self, cmd: str, timeout_sec: float = 1.0):
        return self.execute_command(cmd, timeout_sec=timeout_sec)

    def connect(self, host="localhost", port=3333):
        """Connects to the GDB Server."""
        return self.execute_command(
            f"-target-select extended-remote {host}:{port}", timeout_sec=self.timeouts.get("connect")
        )

    def load_firmware(self, filepath: str):
        """Loads symbols and flashes the firmware to the device."""
        responses = []
        # Load symbols
        responses.extend(self.execute_command(f"-file-exec-and-symbols {filepath}", timeout_sec=2.0))
        # Download (flash) the firmware to target memory
        responses.extend(self.execute_command("-target-download", timeout_sec=self.timeouts.get("download")))
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

    def _drain(self, rounds: int = 5):
        """Consume any pending async/stale GDB responses left in the buffer."""
        if not self.gdb:
            return
        for _ in range(rounds):
            try:
                extra = self.gdb.get_gdb_response(timeout_sec=0.1, raise_error_on_timeout=False)
            except TypeError:
                extra = self.gdb.get_gdb_response(timeout_sec=0.1)
            if not extra:
                break

    def set_breakpoint(self, location: str, condition=None, temporary=False, ignore_count=None):
        command = build_break_insert_command(location, condition, temporary, ignore_count)
        return self.execute_command(command)

    def delete_breakpoint(self, breakpoint_id: str):
        return self.execute_command(f"-break-delete {breakpoint_id}")

    def continue_execution(self):
        return self.execute_command("-exec-continue", timeout_sec=1.0)

    def halt_execution(self):
        return self.execute_command("-exec-interrupt", timeout_sec=1.0)

    def step_over(self):
        return self.execute_command("-exec-next", timeout_sec=1.0)

    def step_into(self):
        return self.execute_command("-exec-step", timeout_sec=1.0)

    def step_out(self):
        return self.execute_command("-exec-finish", timeout_sec=2.0)

    def step_instruction(self, over: bool = False):
        cmd = "-exec-next-instruction" if over else "-exec-step-instruction"
        return self.execute_command(cmd, timeout_sec=1.0)

    def run_to_line(self, location: str):
        return self.execute_command(f"-exec-until {location}", timeout_sec=10.0)

    def read_variable(self, name: str):
        return self.execute_command(f"-data-evaluate-expression {name}")

    def read_call_stack(self):
        return self.execute_command("-stack-list-frames")

    def select_frame(self, level: int):
        return self.execute_command(f"-stack-select-frame {int(level)}")

    def read_frame_variables(self, level: int | None = None):
        """List locals + arguments (with values) for a frame."""
        if level is not None:
            self.select_frame(level)
        return self.execute_command("-stack-list-variables --all-values", timeout_sec=2.0)

    def read_frame_variables_decoded(self, level: int | None = None):
        return decode_variables(self.read_frame_variables(level))

    def read_frame_arguments(self, level: int | None = None):
        if level is not None:
            self.select_frame(level)
        return self.execute_command("-stack-list-arguments --all-values", timeout_sec=2.0)

    def list_source(self, location: str | None = None, count: int = 10):
        """List source lines around a location (function, file:line, or *addr)."""
        if location:
            self.execute_cli_command(f"list {location}", timeout_sec=2.0)
        return self.execute_cli_command(f"list +{int(count)}", timeout_sec=2.0)

    def resolve_address(self, expr: str):
        """Map an address/expression to source line and nearest symbol."""
        responses = []
        responses.extend(self.execute_cli_command(f"info line *({expr})", timeout_sec=2.0))
        responses.extend(self.execute_cli_command(f"info symbol {expr}", timeout_sec=2.0))
        return responses

    def read_core_registers(self):
        return self.execute_cli_command("info registers", timeout_sec=2.0)

    def read_core_registers_decoded(self):
        """Return {name: hex} via the structured MI register queries."""
        names = self.execute_command("-data-list-register-names", timeout_sec=2.0)
        values = self.execute_command("-data-list-register-values x", timeout_sec=2.0)
        return decode_registers(names, values)

    def read_call_stack_decoded(self):
        return decode_backtrace(self.read_call_stack())

    def disassemble_around_pc(self, instructions: int = 8):
        return self.execute_cli_command(f"x/{instructions}i $pc", timeout_sec=2.0)

    def disassemble(self, location: str = "$pc", instructions: int = 8):
        return self.execute_cli_command(f"x/{int(instructions)}i {location}", timeout_sec=2.0)

    def list_functions(self, regex: str | None = None):
        cmd = f"info functions {regex}" if regex else "info functions"
        return self.execute_cli_command(cmd, timeout_sec=3.0)

    def list_variables(self, regex: str | None = None):
        cmd = f"info variables {regex}" if regex else "info variables"
        return self.execute_cli_command(cmd, timeout_sec=3.0)

    def lookup_type(self, expr: str):
        return self.execute_cli_command(f"ptype {expr}", timeout_sec=2.0)

    def sizeof(self, expr: str):
        return self.execute_command(f"-data-evaluate-expression sizeof({expr})", timeout_sec=2.0)

    def address_of(self, symbol: str):
        return self.execute_command(f"-data-evaluate-expression &{symbol}", timeout_sec=2.0)

    def capture_coredump(self, path: str):
        return self.execute_cli_command(f"generate-core-file {path}", timeout_sec=30.0)

    def load_coredump(self, path: str):
        return self.execute_cli_command(f"core-file {path}", timeout_sec=10.0)

    def verify_flash(self, file_path: str):
        responses = []
        responses.extend(self.execute_command(f"-file-exec-and-symbols {file_path}", timeout_sec=2.0))
        responses.extend(self.execute_cli_command("compare-sections", timeout_sec=30.0))
        return responses

    def enable_cycle_counter(self):
        for address, value in dwt.enable_cycle_counter_writes():
            self.write_typed_memory(hex(address), hex(value), width_bits=32)

    def read_cycle_counter(self) -> int:
        return dwt.read_cycle_count(self.read_word)

    def sample_pc(self, count: int = 64):
        return dwt.sample_pc(self.read_word, count)

    def read_register_value(self, expr: str) -> int:
        """Evaluate a register/convenience expression (e.g. '$lr', '$msp') to an int."""
        response = self.execute_command(f"-data-evaluate-expression {expr}", timeout_sec=2.0)
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
        return self.execute_command(cmd)

    def read_memory(self, address: str, length: int):
        return self.execute_command(
            f"-data-read-memory-bytes {address} {length}", timeout_sec=self.timeouts.get("memory")
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
        return self.execute_cli_command(f"set {{{c_type}}}{address} = {value}")

    def get_responses(self, timeout_sec: float = 0.1):
        """Polls for asynchronous events from GDB (e.g. hit breakpoint)."""
        if not self.gdb:
            return []
        return self.gdb.get_gdb_response(timeout_sec=timeout_sec)

    def _wait_for_stop(self, initial, timeout_sec: float):
        """Drain async records until a `*stopped` arrives or timeout elapses."""
        records = list(initial or [])
        event = parse_stop_event(records)
        if event["stopped"]:
            return event, records

        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            try:
                more = self.gdb.get_gdb_response(timeout_sec=0.2, raise_error_on_timeout=False)
            except TypeError:
                # Older pygdbmi without the keyword still returns [] on timeout.
                more = self.gdb.get_gdb_response(timeout_sec=0.2)
            if more:
                records.extend(more)
                event = parse_stop_event(records)
                if event["stopped"]:
                    return event, records
        return parse_stop_event(records), records

    def run_and_wait(self, timeout_sec: float = 10.0):
        """Resume the target and report why it next stops (or a timeout)."""
        if not self.gdb:
            raise RuntimeError("GDB is not running.")
        initial = self.gdb.write("-exec-continue", timeout_sec=0.2)
        event, records = self._wait_for_stop(initial, timeout_sec)
        event["raw_response"] = records
        return event

    def wait_for_stop(self, timeout_sec: float = 10.0):
        """Wait for the next stop event without resuming the target."""
        if not self.gdb:
            raise RuntimeError("GDB is not running.")
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
            if isinstance(payload, str) and "0x" in payload:
                token = payload.split("0x", 1)[1].split()[0].rstrip(":,")
                return int(token, 16)
        return 0
