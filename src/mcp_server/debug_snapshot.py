from datetime import datetime, timezone

from .fault_analysis import diagnose_fault_registers


def _hex_registers(registers: dict) -> dict:
    result = {}
    for name, value in registers.items():
        if isinstance(value, int):
            result[name] = f"0x{value & 0xFFFFFFFF:08x}"
        else:
            result[name] = value
    return result


def _safe_call(label, func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except Exception as exc:
        return {"error": f"{label} failed: {exc}"}


def collect_debug_snapshot(
    gdb_client,
    gdb_manager,
    project_context: dict | None = None,
    rtos_context: dict | None = None,
    log_context: dict | None = None,
) -> dict:
    fault_registers = _safe_call("fault register capture", gdb_client.read_fault_registers)
    if isinstance(fault_registers, dict) and "error" not in fault_registers:
        fault_diagnosis = diagnose_fault_registers(fault_registers)
        fault_registers = _hex_registers(fault_registers)
    else:
        fault_diagnosis = fault_registers

    snapshot = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "session": {
            "server_type": getattr(gdb_manager, "server_type", None),
            "port": getattr(gdb_manager, "port", None),
        },
        "core_registers": _safe_call("core register capture", gdb_client.read_core_registers),
        "fault_registers": fault_registers,
        "fault_diagnosis": fault_diagnosis,
        "call_stack": _safe_call("call stack capture", gdb_client.read_call_stack),
        "disassembly": _safe_call("PC disassembly", gdb_client.disassemble_around_pc),
        "gdb_events": _safe_call("GDB event capture", gdb_client.get_responses),
        "server_logs": _safe_call("GDB server log capture", gdb_manager.get_logs),
    }
    if project_context is not None:
        snapshot["project"] = project_context
    if rtos_context is not None:
        snapshot["rtos"] = rtos_context
    if log_context is not None:
        snapshot["logs"] = log_context
    return snapshot
