"""On-connect sanity checks that catch environment/config faults early.

Phase 2 priority #2 (reliability & correctness). Directly motivated by the HIL
byte-order bug: before the agent spends steps reasoning on garbage, validate that
the link reads memory in the right byte order, that a real Cortex-M is on the
other end, and that the device matches the expected family.

CPUID (0xE000ED00) and DBGMCU IDCODE (0xE0042000) are known fixed-format values,
which makes them ideal probes.
"""

CORTEX_M_PARTNO = {
    0xC20: "Cortex-M0",
    0xC60: "Cortex-M0+",
    0xC21: "Cortex-M1",
    0xC23: "Cortex-M3",
    0xC24: "Cortex-M4",
    0xC27: "Cortex-M7",
    0xD20: "Cortex-M23",
    0xD21: "Cortex-M33",
}

STM32_DEV_ID = {
    0x413: "STM32F40x/41x",
    0x419: "STM32F42x/43x",
    0x431: "STM32F411",
    0x435: "STM32L43x/44x",
    0x462: "STM32L45x/46x",
    0x415: "STM32L47x/48x",
    0x464: "STM32L41x/42x",
    0x460: "STM32G07x/08x",
    0x468: "STM32G43x/44x",
    0x450: "STM32H74x/75x",
}


def evaluate_self_check(cpuid: int, dbgmcu_idcode: int, expected_family: str | None = None) -> dict:
    checks = []

    # Byte-order: a correctly-read ARM CPUID has implementer 0x41 in the top byte
    # AND the architecture constant 0xF in bits[19:16]. A byte-reversed read breaks
    # the constant nibble even when the implementer byte happens to survive.
    implementer = (cpuid >> 24) & 0xFF
    constant = (cpuid >> 16) & 0xF
    byte_order_ok = implementer == 0x41 and constant == 0xF
    checks.append({
        "name": "byte_order",
        "ok": byte_order_ok,
        "detail": f"CPUID=0x{cpuid:08x} implementer=0x{implementer:02x} (expect 0x41) constant=0x{constant:x} (expect 0xf)",
    })

    partno = (cpuid >> 4) & 0xFFF
    core = CORTEX_M_PARTNO.get(partno)
    checks.append({
        "name": "cortex_m_core",
        "ok": core is not None,
        "detail": core or f"unknown CPUID partno 0x{partno:03x}",
    })

    dev_id = dbgmcu_idcode & 0xFFF
    rev_id = (dbgmcu_idcode >> 16) & 0xFFFF
    device = STM32_DEV_ID.get(dev_id)
    dev_ok = True
    detail = f"dev_id=0x{dev_id:03x} {device or '(unknown)'} rev=0x{rev_id:04x}"
    if expected_family:
        prefix = expected_family[:7].lower()  # e.g. "stm32l4"
        dev_ok = device is not None and prefix in device.lower()
        if not dev_ok:
            detail += f"; does not match expected '{expected_family}'"
    checks.append({"name": "dbgmcu_dev_id", "ok": dev_ok, "detail": detail})

    return {
        "ok": all(c["ok"] for c in checks),
        "cpuid": f"0x{cpuid:08x}",
        "dbgmcu_idcode": f"0x{dbgmcu_idcode:08x}",
        "core": core,
        "device": device,
        "checks": checks,
    }
