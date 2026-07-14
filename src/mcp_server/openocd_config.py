"""Resolve OpenOCD server_args from an MCU + probe, and locate bundled scripts.

UX fix: agents kept hunting the disk for `stlink.cfg` and drawing wrong
conclusions. OpenOCD resolves `-f interface/stlink.cfg` against its own bundled
`scripts/` directory, so the agent should never search for it. This maps a
common STM32 family + probe to the right config files and validates them against
the actual OpenOCD install.
"""

import os
import shutil

# STM32 family key (letter+digit) -> OpenOCD target config.
_TARGET_CFG = {
    "f0": "stm32f0x.cfg",
    "f1": "stm32f1x.cfg",
    "f2": "stm32f2x.cfg",
    "f3": "stm32f3x.cfg",
    "f4": "stm32f4x.cfg",
    "f7": "stm32f7x.cfg",
    "h7": "stm32h7x.cfg",
    "l0": "stm32l0.cfg",
    "l1": "stm32l1.cfg",
    "l4": "stm32l4x.cfg",
    "l5": "stm32l5x.cfg",
    "g0": "stm32g0x.cfg",
    "g4": "stm32g4x.cfg",
    "u5": "stm32u5x.cfg",
    "wb": "stm32wbx.cfg",
    "wl": "stm32wlx.cfg",
}

_INTERFACE_CFG = {
    "stlink": "stlink.cfg",
    "st-link": "stlink.cfg",
    "st_link": "stlink.cfg",
    "jlink": "jlink.cfg",
    "j-link": "jlink.cfg",
    "cmsis-dap": "cmsis-dap.cfg",
    "cmsisdap": "cmsis-dap.cfg",
    "dap": "cmsis-dap.cfg",
}


def _family_key(mcu: str) -> str:
    s = (mcu or "").lower().replace("stm32", "").strip()
    if len(s) >= 2 and s[0].isalpha() and s[1].isdigit():
        key = s[0] + s[1]
        if key in _TARGET_CFG:
            return key
    raise ValueError(f"Unknown or unsupported STM32 family for MCU {mcu!r}. Known: {sorted(_TARGET_CFG)}")


def suggest_server_args(mcu: str, probe: str, scripts_dir: str | None = None, speed_khz: int = 4000) -> dict:
    """Return the OpenOCD ``server_args`` for an MCU + probe, optionally validated.

    Appends ``-c "adapter speed <speed_khz>"`` (default 4 MHz) — the default ST-Link
    SWD clock is only ~480 kHz, so this speeds up flashing and memory reads ~8x.
    Pass speed_khz=0 to omit (use the config default).
    """
    target = _TARGET_CFG[_family_key(mcu)]
    probe_key = (probe or "").lower().strip()
    if probe_key not in _INTERFACE_CFG:
        raise ValueError(f"Unknown probe {probe!r}. Known: {sorted(set(_INTERFACE_CFG))}")
    interface = _INTERFACE_CFG[probe_key]

    args = ["-f", f"interface/{interface}", "-f", f"target/{target}"]
    if speed_khz:
        args += ["-c", f"adapter speed {speed_khz}"]

    result = {
        "interface": interface,
        "target": target,
        "speed_khz": speed_khz,
        "server_args": args,
    }

    if scripts_dir:
        result["scripts_dir"] = scripts_dir
        result["validated"] = (
            os.path.isfile(os.path.join(scripts_dir, "interface", interface))
            and os.path.isfile(os.path.join(scripts_dir, "target", target))
        )
    return result


def find_openocd_scripts(openocd_path: str | None = None) -> str | None:
    """Locate OpenOCD's bundled ``scripts`` directory from the executable path."""
    exe = openocd_path or shutil.which("openocd")
    if not exe:
        return None
    bin_dir = os.path.dirname(os.path.abspath(exe))
    base = os.path.dirname(bin_dir)
    candidates = [
        os.path.join(base, "openocd", "scripts"),     # xpack layout
        os.path.join(base, "scripts"),
        os.path.join(base, "share", "openocd", "scripts"),
    ]
    for candidate in candidates:
        if os.path.isdir(os.path.join(candidate, "interface")):
            return candidate
    return None


def detect_and_suggest(mcu: str, serial: str | None = None, scripts_dir: str | None = None, 
                      speed_khz: int = 4000) -> dict:
    """Auto-detect probe type and return OpenOCD server_args for the given MCU.

    Enumerates connected USB debug probes and uses the detected probe type to
    automatically select the correct interface config. If multiple probes are
    found, optionally filters by serial number.

    Args:
        mcu: MCU or family (e.g. 'STM32L431' or 'STM32F4').
        serial: Optional serial number to select a specific probe (required if multiple probes found).
        scripts_dir: Optional OpenOCD scripts directory for validation.
        speed_khz: Adapter speed in kHz (default 4000).

    Returns:
        Dict with server_args and probe info, or error dict if detection fails.

    Raises:
        ValueError: If MCU is unknown/unsupported, or probe detection is inconclusive.
    """
    from .probe_detector import detect_probes

    probes = detect_probes()
    
    if not probes:
        raise ValueError("No debug probes detected. Check USB connection and drivers.")
    
    # Filter by serial if specified
    if serial:
        matching = [p for p in probes if p.serial and p.serial.lower() == serial.lower()]
        if not matching:
            raise ValueError(
                f"No probe found with serial '{serial}'. Available: "
                f"{[p.serial for p in probes if p.serial]}"
            )
        probes = matching
    
    if len(probes) > 1:
        serials = [p.serial or p.product or p.manufacturer or "(unknown)" for p in probes]
        raise ValueError(
            f"Found {len(probes)} probes, need serial to disambiguate: {serials}. "
            f"Pass serial=<serial> to detect_and_suggest."
        )
    
    probe = probes[0]
    # Normalize probe type for suggest_server_args (it expects "stlink", "jlink", or "cmsis-dap")
    probe_map = {
        "st-link": "stlink",
        "daplink": "cmsis-dap",
    }
    probe_type = probe_map.get(probe.type, probe.type)
    
    result = suggest_server_args(mcu, probe_type, scripts_dir=scripts_dir, speed_khz=speed_khz)
    result["detected_probe"] = probe.to_dict()
    return result

