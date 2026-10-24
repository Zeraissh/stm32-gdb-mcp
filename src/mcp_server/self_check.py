"""On-connect sanity checks that catch environment/config faults early.

Phase 2 priority #2 (reliability & correctness). Directly motivated by the HIL
byte-order bug: before the agent spends steps reasoning on garbage, validate that
the link reads memory in the right byte order, that a real Cortex-M is on the
other end, and that the device matches the expected family.

CPUID (0xE000ED00) and DBGMCU IDCODE are known fixed-format values, which makes
them ideal probes. The IDCODE address is not the same on every STM32: the
0xE0042000 base used by F4/WB-class parts reads as zero on H7, F0/G0/L0, and
L5/U5. ``dbgmcu_idcode_candidates`` picks the address from the expected family
or, failing that, from the CPUID part number.
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
    0x455: "STM32U535/U545", 0x482: "STM32U575/585", 0x481: "STM32U59x/5Ax",
}

# DBGMCU IDCODE is the register at offset 0 of DBGMCU (named DBG on G0/C0).
# Bases checked against CMSIS device headers (DBGMCU_BASE / DBG_BASE) and OpenOCD
# (stm32h7x.c DBGMCU_IDCODE_REGISTER, stm32l4x.h DBGMCU_IDCODE_*, stm32f1x.c by
# CPUID, stm32lx.c DBGMCU_IDCODE_L0).
DBGMCU_IDCODE_LEGACY = 0xE0042000  # F1/F2/F3/F4/F7, L1/L4, G4, WB, WL
DBGMCU_IDCODE_LOW_APB = 0x40015800  # F0/G0/L0/C0/U0
DBGMCU_IDCODE_H7 = 0x5C001000  # H74x/H75x, H72x/H73x, H7A/H7B
DBGMCU_IDCODE_EPPB = 0xE0044000  # L5/U5/U3/WBA
DBGMCU_IDCODE_H5 = 0x44024000  # H5 AHB3 DBGMCU (not the L5/U5 EPPB window)

# Longest prefix wins, so "stm32wba" is not classified as "stm32wb".
_FAMILY_IDCODE_ADDRESS: tuple[tuple[str, int], ...] = (
    ("stm32h7", DBGMCU_IDCODE_H7),
    ("stm32h5", DBGMCU_IDCODE_H5),
    ("stm32f0", DBGMCU_IDCODE_LOW_APB),
    ("stm32g0", DBGMCU_IDCODE_LOW_APB),
    ("stm32l0", DBGMCU_IDCODE_LOW_APB),
    ("stm32c0", DBGMCU_IDCODE_LOW_APB),
    ("stm32u0", DBGMCU_IDCODE_LOW_APB),
    ("stm32l5", DBGMCU_IDCODE_EPPB),
    ("stm32u5", DBGMCU_IDCODE_EPPB),
    ("stm32u3", DBGMCU_IDCODE_EPPB),
    ("stm32wba", DBGMCU_IDCODE_EPPB),
    ("stm32f1", DBGMCU_IDCODE_LEGACY),
    ("stm32f2", DBGMCU_IDCODE_LEGACY),
    ("stm32f3", DBGMCU_IDCODE_LEGACY),
    ("stm32f4", DBGMCU_IDCODE_LEGACY),
    ("stm32f7", DBGMCU_IDCODE_LEGACY),
    ("stm32l1", DBGMCU_IDCODE_LEGACY),
    ("stm32l4", DBGMCU_IDCODE_LEGACY),
    ("stm32g4", DBGMCU_IDCODE_LEGACY),
    ("stm32wb", DBGMCU_IDCODE_LEGACY),
    ("stm32wl", DBGMCU_IDCODE_LEGACY),
)

# Used only when no family prefix matched. Cortex-M7 is absent on purpose:
# F7 kept the legacy base and H7 moved, so a bare M7 CPUID has to try both.
_CPUID_PARTNO_IDCODE_ADDRESS = {
    0xC20: DBGMCU_IDCODE_LOW_APB,  # Cortex-M0
    0xC60: DBGMCU_IDCODE_LOW_APB,  # Cortex-M0+
    0xC21: DBGMCU_IDCODE_LEGACY,  # Cortex-M1
    0xC23: DBGMCU_IDCODE_LEGACY,  # Cortex-M3
    0xC24: DBGMCU_IDCODE_LEGACY,  # Cortex-M4
    0xD20: DBGMCU_IDCODE_LOW_APB,  # Cortex-M23
    0xD21: DBGMCU_IDCODE_EPPB,  # Cortex-M33 (H5 shares this core; name the family)
}
_CORTEX_M7_PARTNO = 0xC27


def _family_key(family: str) -> str:
    return "".join(ch for ch in family.lower() if ch.isalnum())


def _address_for_family(family: str) -> int | None:
    key = _family_key(family)
    best: int | None = None
    best_len = 0
    for prefix, address in _FAMILY_IDCODE_ADDRESS:
        if len(prefix) > best_len and key.startswith(prefix):
            best = address
            best_len = len(prefix)
    return best


def dbgmcu_idcode_candidates(expected_family: str | None = None, cpuid: int | None = None) -> list[int]:
    """DBGMCU IDCODE addresses to try, most specific first.

    A recognized family (or profile MCU) selects exactly one address. With no
    family, the CPUID part number selects one — except Cortex-M7, where F7 and
    H7 disagree. That case returns the legacy address and then the H7 address.
    Anything still unrecognized keeps the historical ``0xE0042000`` read.
    """
    if expected_family:
        address = _address_for_family(expected_family)
        if address is not None:
            return [address]
    if cpuid is not None:
        partno = (int(cpuid) >> 4) & 0xFFF
        if partno == _CORTEX_M7_PARTNO:
            # Legacy first: on H7 that read returns 0 and does not fault (measured
            # on STM32H750VB). An F7 then matches a known DEV_ID and the H7
            # address is never touched.
            return [DBGMCU_IDCODE_LEGACY, DBGMCU_IDCODE_H7]
        mapped = _CPUID_PARTNO_IDCODE_ADDRESS.get(partno)
        if mapped is not None:
            return [mapped]
    return [DBGMCU_IDCODE_LEGACY]


def read_dbgmcu_idcode(read_word, expected_family: str | None = None, cpuid: int | None = None) -> tuple[int, int]:
    """Read DBGMCU IDCODE. Returns ``(value, address)``.

    ``read_word(address) -> int`` is the only target access. A single candidate
    is read as-is, including zero. The Cortex-M7 pair stops at the first value
    whose DEV_ID is in ``STM32_DEV_ID``; a fault on the second address keeps
    the first read.
    """
    candidates = dbgmcu_idcode_candidates(expected_family, cpuid)
    if len(candidates) == 1:
        address = candidates[0]
        return int(read_word(address)) & 0xFFFFFFFF, address

    fallback: tuple[int, int] | None = None
    last_error: Exception | None = None
    for address in candidates:
        try:
            value = int(read_word(address)) & 0xFFFFFFFF
        except Exception as exc:  # noqa: BLE001 — the other M7 address may fault
            last_error = exc
            continue
        if (value & 0xFFF) in STM32_DEV_ID:
            return value, address
        if fallback is None or ((fallback[0] & 0xFFF) == 0 and (value & 0xFFF) != 0):
            fallback = (value, address)
    if fallback is not None:
        return fallback
    if last_error is not None:
        raise last_error
    return 0, candidates[0]


def evaluate_self_check(
    cpuid: int,
    dbgmcu_idcode: int,
    expected_family: str | None = None,
    idcode_address: int | None = None,
) -> dict:
    checks = []

    # Byte order: a correctly-read ARM CPUID has implementer 0x41 in the top byte
    # and the architecture constant in bits[19:16]. That nibble is 0xF on the
    # CPUID scheme (M3/M4/M7/M33) and 0xC on ARMv6-M (M0/M0+: F0/G0/L0). A
    # byte-reversed read breaks it even when the implementer byte survives.
    implementer = (cpuid >> 24) & 0xFF
    constant = (cpuid >> 16) & 0xF
    byte_order_ok = implementer == 0x41 and constant in (0xC, 0xF)
    checks.append({
        "name": "byte_order",
        "ok": byte_order_ok,
        "detail": (
            f"CPUID=0x{cpuid:08x} implementer=0x{implementer:02x} (expect 0x41) "
            f"constant=0x{constant:x} (expect 0xf, or 0xc on Cortex-M0/M0+)"
        ),
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
        if not dev_ok and _is_advisory_u5_dbgmcu_miss(dev_id, expected_family, core):
            dev_ok = True
            detail += (
                f"; DBGMCU IDCODE unavailable, but CPUID reports {core} "
                "which is consistent with STM32U5"
            )
        elif not dev_ok:
            detail += f"; does not match expected '{expected_family}'"
    checks.append({"name": "dbgmcu_dev_id", "ok": dev_ok, "detail": detail})

    result = {
        "ok": all(c["ok"] for c in checks),
        "cpuid": f"0x{cpuid:08x}",
        "dbgmcu_idcode": f"0x{dbgmcu_idcode:08x}",
        "core": core,
        "device": device,
        "checks": checks,
    }
    if idcode_address is not None:
        result["dbgmcu_idcode_address"] = f"0x{idcode_address:08x}"
    return result


def _is_advisory_u5_dbgmcu_miss(dev_id: int, expected_family: str, core: str | None) -> bool:
    # A zero IDCODE used to be what self_check always saw on U5, because it read
    # the F4-era base. The address is now 0xE0044000. This remains for a genuine
    # zero at that address (DBGMCU clock still gated) when CPUID is Cortex-M33.
    return dev_id == 0 and expected_family.upper().startswith("STM32U5") and core == "Cortex-M33"
