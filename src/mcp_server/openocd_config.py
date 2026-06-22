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


def suggest_server_args(mcu: str, probe: str, scripts_dir: str | None = None) -> dict:
    """Return the OpenOCD ``server_args`` for an MCU + probe, optionally validated."""
    target = _TARGET_CFG[_family_key(mcu)]
    probe_key = (probe or "").lower().strip()
    if probe_key not in _INTERFACE_CFG:
        raise ValueError(f"Unknown probe {probe!r}. Known: {sorted(set(_INTERFACE_CFG))}")
    interface = _INTERFACE_CFG[probe_key]

    result = {
        "interface": interface,
        "target": target,
        "server_args": ["-f", f"interface/{interface}", "-f", f"target/{target}"],
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
