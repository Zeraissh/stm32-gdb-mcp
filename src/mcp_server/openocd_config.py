"""Resolve OpenOCD server_args from an MCU + probe, and locate bundled scripts.

UX fix: agents kept hunting the disk for `stlink.cfg` and drawing wrong
conclusions. OpenOCD resolves `-f interface/stlink.cfg` against its own bundled
`scripts/` directory, so the agent should never search for it. This maps a
common STM32 family + probe to the right config files and validates them against
the actual OpenOCD install.
"""

import os
import platform
import re
import shutil
import subprocess

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


# Known USB VID:PID -> (probe_type, product_name)
_USB_PROBE_MAP = {
    ("0483", "3748"): ("stlink", "ST-Link/V2"),
    ("0483", "374b"): ("stlink", "ST-Link/V2-1"),
    ("0483", "374d"): ("stlink", "ST-Link/V3E"),
    ("0483", "374e"): ("stlink", "ST-Link/V3"),
    ("0483", "374f"): ("stlink", "ST-Link/V3 (HLA)"),
    ("0d28", "0204"): ("cmsis-dap", "DAPLink CMSIS-DAP"),
    ("1fc9", "0143"): ("cmsis-dap", "LPC-Link2 CMSIS-DAP"),
    ("1366", "0101"): ("jlink", "J-Link"),
    ("1366", "0105"): ("jlink", "J-Link Pro"),
}


def _parse_openocd_adapter_list(output: str) -> list:
    """Parse ``openocd -c 'adapter list' -c 'exit'`` stdout+stderr into probe dicts."""
    probes = []
    seen = set()
    for line in output.splitlines():
        ll = line.lower()
        if "st-link" in ll or "stlink" in ll:
            probe_type = "stlink"
        elif "cmsis-dap" in ll or "daplink" in ll:
            probe_type = "cmsis-dap"
        elif "j-link" in ll or "jlink" in ll:
            probe_type = "jlink"
        else:
            continue
        product = line.strip()
        serial_m = re.search(r'\bserial[:\s]+([A-Fa-f0-9]+)', line, re.IGNORECASE)
        serial = serial_m.group(1) if serial_m else None
        key = (probe_type, serial, product)
        if key in seen:
            continue
        seen.add(key)
        entry = {"type": probe_type, "product": product}
        if serial:
            entry["serial"] = serial
        probes.append(entry)
    return probes


def _detect_probe_lsusb() -> list:
    """Enumerate USB devices on Linux/macOS via ``lsusb`` and match VID:PID."""
    try:
        result = subprocess.run(["lsusb"], capture_output=True, text=True, timeout=5)
        output = result.stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    probes = []
    for line in output.splitlines():
        m = re.search(r'ID\s+([0-9a-fA-F]{4}):([0-9a-fA-F]{4})\s+(.*)', line)
        if not m:
            continue
        vid, pid, desc = m.group(1).lower(), m.group(2).lower(), m.group(3).strip()
        info = _USB_PROBE_MAP.get((vid, pid))
        if info:
            probes.append({"type": info[0], "product": info[1] or desc})
    return probes


def _detect_probe_windows() -> list:
    """Enumerate USB devices on Windows via ``Get-PnpDevice`` and match VID/PID."""
    ps_cmd = (
        "Get-PnpDevice -PresentOnly | "
        "Where-Object { $_.InstanceId -match 'USB' } | "
        "Select-Object -ExpandProperty InstanceId"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=10,
        )
        output = result.stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    probes = []
    seen_keys = set()
    for line in output.splitlines():
        ll = line.upper()
        vid_m = re.search(r'VID_([0-9A-F]{4})', ll)
        pid_m = re.search(r'PID_([0-9A-F]{4})', ll)
        if not vid_m or not pid_m:
            continue
        vid, pid = vid_m.group(1).lower(), pid_m.group(1).lower()
        key = (vid, pid)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        info = _USB_PROBE_MAP.get(key)
        if info:
            probes.append({"type": info[0], "product": info[1]})
    return probes


def detect_probe(openocd_path: str | None = None, timeout: int = 5) -> dict:
    """Auto-detect debug probes connected to this host.

    **Method A** (preferred, cross-platform): runs
    ``openocd -c 'adapter list' -c 'exit'`` and parses the adapter names.

    **Method B** (fallback when OpenOCD is absent): enumerates USB devices via
    ``lsusb`` (Linux/macOS) or ``Get-PnpDevice`` (Windows) and matches against
    known VID:PID values for ST-Link, CMSIS-DAP, and J-Link probes.

    Returns a dict with:
    - ``probes``: list of ``{type, product[, serial]}`` dicts.
    - ``suggested_probe``: the probe *type* string when exactly one probe is found.
    - ``method``: which detection method was used.
    """
    probes: list = []
    method = "none"

    exe = openocd_path or shutil.which("openocd")
    if exe:
        try:
            proc = subprocess.run(
                [exe, "-c", "adapter list", "-c", "exit"],
                capture_output=True, text=True, timeout=timeout,
            )
            raw = (proc.stdout or "") + (proc.stderr or "")
            probes = _parse_openocd_adapter_list(raw)
            method = "openocd"
        except (OSError, subprocess.TimeoutExpired):
            pass

    if not probes:
        sys_name = platform.system()
        if sys_name == "Windows":
            probes = _detect_probe_windows()
            method = "usb_windows"
        elif sys_name in ("Linux", "Darwin"):
            probes = _detect_probe_lsusb()
            method = "usb_lsusb"

    result: dict = {"probes": probes, "method": method}
    if len(probes) == 1:
        result["suggested_probe"] = probes[0]["type"]
    return result
