"""SWO/ITM trace auto-config: make ``printf`` over the single SWO pin work out-of-the-box.

The whole point is to remove hand-crafting. Given the core clock (HCLK) and a desired SWO
baud, this computes the TPIU prescaler and returns the register writes that configure the
**target's** TPIU + ITM straight from the debugger — so the firmware needs no ITM-init code,
only a one-line ``printf`` retarget that writes characters to ITM stimulus port 0. It also
emits the host-side OpenOCD commands needed to capture and decode the stream.

Registers are the Cortex-M architectural debug addresses (same across M3/M4/M7).
"""

# --- Cortex-M debug / trace registers ---
DEMCR = 0xE000EDFC
DEMCR_TRCENA = 1 << 24

# TPIU (Trace Port Interface Unit) — formats the trace for the SWO pin
TPIU_ACPR = 0xE0040010   # Async Clock Prescaler  (= HCLK/SWO - 1)
TPIU_SPPR = 0xE00400F0   # Selected Pin Protocol  (2 = SWO async NRZ / UART)
TPIU_FFCR = 0xE0040304   # Formatter & Flush Control

TPIU_SPPR_SWO_NRZ = 0x2
TPIU_FFCR_SWO = 0x100    # disable the formatter (single source) for SWO

# ITM (Instrumentation Trace Macrocell) — stimulus ports carry printf bytes
ITM_TER = 0xE0000E00     # Trace Enable Register   (bit p enables stimulus port p)
ITM_TCR = 0xE0000E80     # Trace Control Register
ITM_LAR = 0xE0000FB0     # Lock Access Register
ITM_LAR_UNLOCK = 0xC5ACCE55
ITM_TCR_VALUE = 0x00010005  # ITMENA | SYNCENA, ATB/Trace bus ID = 1


def swo_prescaler(hclk_hz: int, swo_hz: int):
    """Return (prescaler_register_value, achieved_swo_hz, exact). TPIU divides HCLK by
    (prescaler+1); an inexact divisor still works if both ends agree on achieved_swo_hz."""
    if hclk_hz <= 0 or swo_hz <= 0:
        raise ValueError("hclk_hz and swo_hz must be positive")
    divisor = max(1, round(hclk_hz / swo_hz))
    achieved = hclk_hz // divisor
    return divisor - 1, achieved, achieved == swo_hz


def itm_tpiu_setup_writes(hclk_hz: int, swo_hz: int, port: int = 0):
    """Target (address, value) writes that fully configure TPIU + ITM for SWO printf.

    Done from the debugger, so firmware needs no ITM/TPIU init — only to send chars to
    the stimulus port. Order matters: unlock and enable trace before the stimulus port.
    """
    prescaler, _achieved, _exact = swo_prescaler(hclk_hz, swo_hz)
    return [
        (DEMCR, DEMCR_TRCENA),          # global trace enable
        (TPIU_SPPR, TPIU_SPPR_SWO_NRZ),  # SWO async (UART-like) on the single pin
        (TPIU_ACPR, prescaler),          # baud = HCLK / (prescaler + 1)
        (TPIU_FFCR, TPIU_FFCR_SWO),      # single-source, no formatter framing
        (ITM_LAR, ITM_LAR_UNLOCK),       # unlock ITM registers
        (ITM_TCR, ITM_TCR_VALUE),        # enable ITM
        (ITM_TER, 1 << port),            # enable the printf stimulus port
    ]


def openocd_swo_capture_commands(hclk_hz: int, swo_hz: int, output: str,
                                 port: int = 0, tpiu_name: str = "$_CHIPNAME.tpiu"):
    """Host-side OpenOCD commands to capture + decode ITM `port` to `output`.

    Modern (OpenOCD >= 0.11) form. `output` may be a file path or ":<tcp-port>". The
    target cfg usually creates the tpiu object (e.g. 'stm32l4x.tpiu'); pass tpiu_name
    if it differs. Run these via `monitor` once the session is connected.
    """
    return [
        f"{tpiu_name} configure -protocol uart -traceclk {hclk_hz} -pin-freq {swo_hz} -output {output}",
        f"itm port {port} on",
    ]


def printf_retarget_snippet() -> str:
    """One-line printf->SWO retarget for the firmware (the only firmware change needed)."""
    return (
        "// printf over SWO: route stdout to ITM stimulus port 0.\n"
        "#include \"stm32xxxx.h\"  // CMSIS, provides ITM_SendChar\n"
        "int _write(int file, char *ptr, int len) {\n"
        "    for (int i = 0; i < len; i++) ITM_SendChar((uint32_t)ptr[i]);\n"
        "    return len;\n"
        "}\n"
        "// then: setvbuf(stdout, NULL, _IONBF, 0); printf(\"hello over SWO\\n\");"
    )
