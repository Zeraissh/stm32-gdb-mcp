CPUID_ADDRESS = "0xE000ED00"
DBGMCU_IDCODE_ADDRESS = "0xE0042000"


def run_hil_smoke(config: dict, gdb_server, gdb_client) -> dict:
    server_type = config["server_type"]
    server_args = config.get("server_args", [])
    hil = config.get("hil", {})
    halt = hil.get("halt", True)
    result = {"ok": False, "server": {"type": server_type}, "steps": []}

    try:
        port = gdb_server.start(server_type, server_args)
        result["server"]["port"] = port
        gdb_client.start_gdb()
        result["connect"] = gdb_client.connect("localhost", port)
        if halt:
            result["halt"] = gdb_client.execute_cli_command("monitor halt", timeout_sec=5.0)
        result["cpuid"] = _read_word(gdb_client, CPUID_ADDRESS)
        result["dbgmcu_idcode"] = _read_word(gdb_client, DBGMCU_IDCODE_ADDRESS)
        result["resume"] = gdb_client.execute_cli_command("monitor resume", timeout_sec=2.0)
        result["ok"] = True
        return result
    finally:
        gdb_client.stop_gdb()
        gdb_server.stop()


def _read_word(gdb_client, address: str) -> dict:
    response = gdb_client.read_typed_memory(address, width_bits=32, count=1)
    return {
        "address": address,
        "raw_response": response,
    }
