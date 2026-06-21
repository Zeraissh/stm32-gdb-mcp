FAULT_REGISTER_ADDRESSES = {
    "CFSR": "0xE000ED28",
    "HFSR": "0xE000ED2C",
    "DFSR": "0xE000ED30",
    "MMFAR": "0xE000ED34",
    "BFAR": "0xE000ED38",
    "AFSR": "0xE000ED3C",
}

CFSR_FLAGS = {
    0: ("IACCVIOL", "MemManage", "Instruction access violation."),
    1: ("DACCVIOL", "MemManage", "Data access violation."),
    3: ("MUNSTKERR", "MemManage", "MemManage fault during exception unstacking."),
    4: ("MSTKERR", "MemManage", "MemManage fault during exception stacking."),
    5: ("MLSPERR", "MemManage", "Lazy FP state preservation MemManage fault."),
    7: ("MMARVALID", "MemManage", "MMFAR contains a valid fault address."),
    8: ("IBUSERR", "BusFault", "Instruction bus error."),
    9: ("PRECISERR", "BusFault", "Precise data bus error."),
    10: ("IMPRECISERR", "BusFault", "Imprecise data bus error."),
    11: ("UNSTKERR", "BusFault", "BusFault during exception unstacking."),
    12: ("STKERR", "BusFault", "BusFault during exception stacking."),
    13: ("LSPERR", "BusFault", "Lazy FP state preservation BusFault."),
    15: ("BFARVALID", "BusFault", "BFAR contains a valid fault address."),
    16: ("UNDEFINSTR", "UsageFault", "Undefined instruction."),
    17: ("INVSTATE", "UsageFault", "Invalid EPSR state."),
    18: ("INVPC", "UsageFault", "Invalid exception return PC."),
    19: ("NOCP", "UsageFault", "Coprocessor access error."),
    24: ("UNALIGNED", "UsageFault", "Unaligned access trap."),
    25: ("DIVBYZERO", "UsageFault", "Divide by zero trap."),
}

HFSR_FLAGS = {
    1: ("VECTTBL", "Fault on vector table read."),
    30: ("FORCED", "Configurable fault escalated to HardFault."),
    31: ("DEBUGEVT", "Debug event triggered HardFault."),
}


def _as_int(value):
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value, 0)
    return 0


def _hex32(value):
    return f"0x{value & 0xFFFFFFFF:08x}"


def diagnose_fault_registers(registers: dict) -> dict:
    cfsr = _as_int(registers.get("CFSR", 0))
    hfsr = _as_int(registers.get("HFSR", 0))
    bfar = _as_int(registers.get("BFAR", 0))
    mmfar = _as_int(registers.get("MMFAR", 0))

    active_flags = []
    descriptions = []
    classes = []

    for bit, (name, fault_class, description) in CFSR_FLAGS.items():
        if cfsr & (1 << bit):
            active_flags.append(name)
            descriptions.append({"flag": name, "class": fault_class, "description": description})
            if fault_class not in classes:
                classes.append(fault_class)

    hfsr_flags = []
    for bit, (name, description) in HFSR_FLAGS.items():
        if hfsr & (1 << bit):
            hfsr_flags.append(name)
            descriptions.append({"flag": name, "class": "HardFault", "description": description})

    if not classes and hfsr:
        classes.append("HardFault")

    fault_addresses = {}
    if "BFARVALID" in active_flags:
        fault_addresses["BFAR"] = hex(bfar)
    if "MMARVALID" in active_flags:
        fault_addresses["MMFAR"] = hex(mmfar)

    summary_parts = []
    if classes:
        summary_parts.append(", ".join(classes))
    else:
        summary_parts.append("No configurable fault bits are active")
    if "FORCED" in hfsr_flags:
        summary_parts.append("forced escalation to HardFault")
    if fault_addresses:
        summary_parts.append("valid fault address captured")

    return {
        "raw": {
            "CFSR": _hex32(cfsr),
            "HFSR": _hex32(hfsr),
            "BFAR": _hex32(bfar),
            "MMFAR": _hex32(mmfar),
        },
        "fault_classes": classes,
        "active_flags": active_flags,
        "hfsr_flags": hfsr_flags,
        "fault_addresses": fault_addresses,
        "descriptions": descriptions,
        "summary": "; ".join(summary_parts),
    }
