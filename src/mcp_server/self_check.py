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

# DBGMCU IDCODE dev_id (bits[11:0]) -> family. Keep the family name prefixed with the
# Lx/Fx/etc. so expected_family matching (e.g. "STM32L1") works.
STM32_DEV_ID = {
    # F0
    0x444: "STM32F03x", 0x445: "STM32F04x/070x6", 0x440: "STM32F05x", 0x448: "STM32F07x", 0x442: "STM32F09x",
    # F1
    0x412: "STM32F10x (low)", 0x410: "STM32F10x (medium)", 0x414: "STM32F10x (high)",
    0x418: "STM32F105/107", 0x420: "STM32F10x (value)", 0x428: "STM32F10x (value high)", 0x430: "STM32F10x (XL)",
    # F2 / F3
    0x411: "STM32F2xx",
    0x422: "STM32F303xB/C", 0x438: "STM32F303x6/8", 0x439: "STM32F301/302x6/8", 0x446: "STM32F303xD/E", 0x432: "STM32F37x",
    # F4
    0x413: "STM32F40x/41x", 0x419: "STM32F42x/43x", 0x423: "STM32F401xB/C", 0x433: "STM32F401xD/E",
    0x431: "STM32F411", 0x441: "STM32F412", 0x421: "STM32F446", 0x434: "STM32F469/479", 0x458: "STM32F410", 0x463: "STM32F413",
    # F7
    0x449: "STM32F74x/75x", 0x451: "STM32F76x/77x", 0x452: "STM32F72x/73x",
    # L0
    0x457: "STM32L01x/02x", 0x425: "STM32L031/041", 0x417: "STM32L05x/06x", 0x447: "STM32L07x/08x",
    # L1
    0x416: "STM32L1xx (Cat.1)", 0x429: "STM32L1xx (Cat.2)", 0x427: "STM32L151/152 (Cat.3)",
    0x436: "STM32L1xx (Cat.4/5)", 0x437: "STM32L1xx (Cat.5/6)",
    # L4 / L5
    0x435: "STM32L43x/44x", 0x462: "STM32L45x/46x", 0x415: "STM32L47x/48x", 0x464: "STM32L41x/42x",
    0x461: "STM32L496/4A6", 0x470: "STM32L4Rx/4Sx", 0x471: "STM32L4P5/Q5", 0x472: "STM32L552/562",
    # G0 / G4
    0x460: "STM32G07x/08x", 0x466: "STM32G03x/04x", 0x456: "STM32G05x/061", 0x467: "STM32G0Bx/0Cx",
    0x468: "STM32G43x/44x", 0x469: "STM32G47x/48x", 0x479: "STM32G491/4A1",
    # H7 / WB / WL / U5
    0x450: "STM32H74x/75x", 0x480: "STM32H7A3/B3", 0x483: "STM32H72x/73x",
    0x495: "STM32WB55", 0x496: "STM32WB35", 0x497: "STM32WLEx/WL5x",
    0x482: "STM32U575/585", 0x481: "STM32U59x/5Ax",
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
