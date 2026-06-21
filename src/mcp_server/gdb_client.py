from pygdbmi.gdbcontroller import GdbController

from .fault_analysis import FAULT_REGISTER_ADDRESSES


class GdbClientManager:
    def __init__(self):
        self.gdb = None
        
    def start_gdb(self, gdb_path="arm-none-eabi-gdb"):
        if self.gdb:
            self.stop_gdb()
        # Use mi3 for latest GDB machine interface features
        self.gdb = GdbController(command=[gdb_path, "--interpreter=mi3"])
        
    def stop_gdb(self):
        if self.gdb:
            self.gdb.exit()
            self.gdb = None

    def execute_command(self, cmd: str, timeout_sec: float = 1.0):
        if not self.gdb:
            raise RuntimeError("GDB is not running.")
        return self.gdb.write(cmd, timeout_sec=timeout_sec)

    def execute_cli_command(self, cmd: str, timeout_sec: float = 1.0):
        return self.execute_command(cmd, timeout_sec=timeout_sec)

    def connect(self, host="localhost", port=3333):
        """Connects to the GDB Server."""
        return self.execute_command(f"-target-select extended-remote {host}:{port}", timeout_sec=5.0)

    def load_firmware(self, filepath: str):
        """Loads symbols and flashes the firmware to the device."""
        responses = []
        # Load symbols
        responses.extend(self.execute_command(f"-file-exec-and-symbols {filepath}", timeout_sec=2.0))
        # Download (flash) the firmware to target memory
        responses.extend(self.execute_command("-target-download", timeout_sec=60.0))
        return responses

    def reset_halt(self, command: str = "monitor reset halt"):
        """Resets the MCU and halts it. OpenOCD uses 'monitor reset halt'."""
        return self.execute_command(command, timeout_sec=5.0)

    def set_breakpoint(self, location: str):
        return self.execute_command(f"-break-insert {location}")

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

    def read_variable(self, name: str):
        return self.execute_command(f"-data-evaluate-expression {name}")

    def read_call_stack(self):
        return self.execute_command("-stack-list-frames")

    def read_core_registers(self):
        return self.execute_cli_command("info registers", timeout_sec=2.0)

    def disassemble_around_pc(self, instructions: int = 8):
        return self.execute_cli_command(f"x/{instructions}i $pc", timeout_sec=2.0)

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
        return self.execute_command(f"-data-read-memory-bytes {address} {length}")

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

    def _validate_memory_width(self, width_bits: int):
        if width_bits not in (8, 16, 32, 64):
            raise ValueError("width_bits must be one of 8, 16, 32, or 64")

    def _extract_first_memory_word(self, response):
        for record in response:
            payload = record.get("payload")
            if isinstance(payload, dict):
                memory = payload.get("memory")
                if memory and isinstance(memory, list):
                    contents = memory[0].get("contents")
                    if contents:
                        return int(contents[:8], 16)
                contents = payload.get("contents")
                if contents:
                    return int(contents[:8], 16)
            if isinstance(payload, str) and "0x" in payload:
                token = payload.split("0x", 1)[1].split()[0].rstrip(":,")
                return int(token, 16)
        return 0
