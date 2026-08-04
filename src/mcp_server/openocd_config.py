"""Resolve OpenOCD server_args from an MCU + probe, and locate bundled scripts.

UX fix: agents kept hunting the disk for `stlink.cfg` and drawing wrong
conclusions. OpenOCD resolves `-f interface/stlink.cfg` against its own bundled
`scripts/` directory, so the agent should never search for it. This maps a
common STM32 family + probe to the right config files and validates them against
the actual OpenOCD install.
"""

import json
import os
import platform
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

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

_STLINK_PIDS = {
    "3744", "3748", "374a", "374b", "374d", "374e", "374f", "3752", "3753", "3754",
}


def _classify_usb_probe(vid: str, pid: str, product: str = "", manufacturer: str = "", serial: str = ""):
    vid = (vid or "").lower().removeprefix("0x")
    pid = (pid or "").lower().removeprefix("0x")
    product = (product or "").strip()
    manufacturer = (manufacturer or "").strip()
    serial = (serial or "").strip()
    text = f"{manufacturer} {product}".lower()

    if vid == "0483" and pid in _STLINK_PIDS:
        probe_type = "stlink"
        default_product = "ST-Link"
    elif vid == "1366" or "j-link" in text or "jlink" in text:
        probe_type = "jlink"
        default_product = "J-Link"
    elif "cmsis-dap" in text or "cmsis dap" in text or "daplink" in text:
        probe_type = "cmsis-dap"
        default_product = "CMSIS-DAP"
    else:
        return None

    result = {
        "type": probe_type,
        "product": product or default_product,
        "vid": vid,
        "pid": pid,
    }
    if manufacturer:
        result["manufacturer"] = manufacturer
    if serial:
        result["serial"] = serial
    return result


def _deduplicate_probes(probes: list[dict]) -> list[dict]:
    unique = []
    seen = set()
    for probe in probes:
        key = (
            probe.get("type"),
            probe.get("serial") or probe.get("instance_id") or probe.get("location") or probe.get("product"),
        )
        if key not in seen:
            seen.add(key)
            unique.append(probe)
    return unique


def _parse_windows_pnp(payload: str) -> list[dict]:
    try:
        devices = json.loads(payload or "[]")
    except json.JSONDecodeError:
        return []
    if isinstance(devices, dict):
        devices = [devices]

    records: list[dict[str, Any]] = []
    for device in devices:
        instance_id = str(device.get("InstanceId") or "")
        upper = instance_id.upper()
        if not upper.startswith("USB\\"):
            continue  # HID\... re-enumerations of the same interface
        vid_match = re.search(r"VID_([0-9A-F]{4})", upper)
        pid_match = re.search(r"PID_([0-9A-F]{4})", upper)
        if not vid_match or not pid_match:
            continue
        tail = instance_id.rsplit("\\", 1)[-1] if "\\" in instance_id else ""
        records.append({
            "instance_id": instance_id,
            "vid": vid_match.group(1),
            "pid": pid_match.group(1),
            "name": str(device.get("FriendlyName") or ""),
            "manufacturer": str(device.get("Manufacturer") or ""),
            # A composite parent's tail is the device serial; an interface's tail is
            # a hub path like "6&39E7DCD8&1&0000" whose last field is the interface.
            "serial": "" if "&" in tail else tail,
            "hub": tail.rsplit("&", 1)[0] if "&" in tail else "",
            "is_interface": "&MI_" in upper,
        })

    # A CMSIS-DAP v2 probe is a USB composite device, and only its MI_00 interface
    # carries the name that identifies it ("Horco CMSIS-DAP v2") — the parent is
    # just "USB Composite Device". Skipping every &MI_ node to avoid counting one
    # probe several times therefore made such a probe invisible, and with an
    # ST-Link also plugged in, detect_probe reported a single ST-Link and
    # start_debug_session auto-selected it. Measured on hardware.
    serials: dict[tuple[str, str], list[str]] = {}
    for record in records:
        if record["serial"] and not record["is_interface"]:
            serials.setdefault((record["vid"], record["pid"]), []).append(record["serial"])

    probes = []
    for record in records:
        serial = record["serial"]
        if not serial and record["is_interface"]:
            # Borrow the parent's serial, but only when it is unambiguous — two
            # identical probes must never collapse into one entry.
            candidates = serials.get((record["vid"], record["pid"]), [])
            if len(candidates) == 1:
                serial = candidates[0]
        probe = _classify_usb_probe(
            record["vid"], record["pid"], record["name"], record["manufacturer"], serial,
        )
        if probe:
            probe["instance_id"] = record["instance_id"]
            if not serial and record["hub"]:
                # Distinguishes two unserialised probes on different ports.
                probe["location"] = record["hub"]
            probes.append(probe)
    return _deduplicate_probes(probes)


def _read_usb_attr(path: Path, name: str) -> str:
    try:
        return (path / name).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _detect_linux_sysfs(root: str | Path = "/sys/bus/usb/devices") -> list[dict]:
    root = Path(root)
    if not root.is_dir():
        return []
    probes = []
    for device in sorted(root.iterdir()):
        vid = _read_usb_attr(device, "idVendor")
        pid = _read_usb_attr(device, "idProduct")
        if not vid or not pid:
            continue
        probe = _classify_usb_probe(
            vid,
            pid,
            _read_usb_attr(device, "product"),
            _read_usb_attr(device, "manufacturer"),
            _read_usb_attr(device, "serial"),
        )
        if probe:
            probe["location"] = device.name
            probes.append(probe)
    return _deduplicate_probes(probes)


def _parse_macos_usb(payload: str) -> list[dict]:
    try:
        tree = json.loads(payload or "{}")
    except json.JSONDecodeError:
        return []
    probes = []

    def visit(value):
        if isinstance(value, dict):
            vid_match = re.search(r"0x([0-9a-fA-F]{4})", str(value.get("vendor_id") or ""))
            pid_match = re.search(r"0x([0-9a-fA-F]{4})", str(value.get("product_id") or ""))
            if vid_match and pid_match:
                probe = _classify_usb_probe(
                    vid_match.group(1),
                    pid_match.group(1),
                    str(value.get("_name") or ""),
                    str(value.get("manufacturer") or ""),
                    str(value.get("serial_num") or ""),
                )
                if probe:
                    probes.append(probe)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(tree)
    return _deduplicate_probes(probes)


# Host USB enumeration is far slower than it looks. Every presence-aware method on
# Windows walks the whole PnP device tree, and the cost scales with how many devices
# the machine has ever seen, not with how many are plugged in. Measured on a normal
# developer laptop with 21 present USB devices:
#
#   Get-PnpDevice -PresentOnly       8.5 - 9.0 s
#   Get-PnpDevice -Class USB         8.3 s
#   Get-CimInstance Win32_PnPEntity  8.5 s
#   pnputil /enum-devices /connected 9.9 s
#
# The old 10 s budget therefore sat right on top of the typical case and failed
# intermittently. Switching query style does not help - the cost is in the device
# tree walk. The registry under HKLM\SYSTEM\CurrentControlSet\Enum\USB is ~90 ms but
# lists devices that are merely known, not present, so it cannot answer "is a probe
# plugged in right now" without extra work.
#
# 45 s gives roughly 4x headroom over the measured worst case. This is a ceiling on
# a pathological hang, not an expected wait.
PROBE_DETECTION_TIMEOUT_S = 45


def detect_probe(platform_name: str | None = None, sysfs_root: str | Path = "/sys/bus/usb/devices", runner=None) -> dict:
    """Enumerate connected debug probes from OS USB device state."""
    system = platform_name or platform.system()
    runner = runner or subprocess.run
    probes = []
    method = "none"
    error = None
    try:
        if system == "Windows":
            command = (
                "Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -match '^USB\\\\VID_' } | "
                "Select-Object InstanceId,FriendlyName,Manufacturer | ConvertTo-Json -Compress"
            )
            result = runner(
                ["powershell", "-NoProfile", "-Command", command],
                capture_output=True,
                text=True,
                timeout=PROBE_DETECTION_TIMEOUT_S,
            )
            method = "windows_pnp"
            if result.returncode:
                error = (result.stderr or "Windows USB enumeration failed").strip()
            else:
                probes = _parse_windows_pnp(result.stdout)
        elif system == "Linux":
            probes = _detect_linux_sysfs(sysfs_root)
            method = "linux_sysfs"
        elif system == "Darwin":
            result = runner(
                ["system_profiler", "SPUSBDataType", "-json"],
                capture_output=True,
                text=True,
                timeout=PROBE_DETECTION_TIMEOUT_S,
            )
            method = "macos_system_profiler"
            if result.returncode:
                error = (result.stderr or "macOS USB enumeration failed").strip()
            else:
                probes = _parse_macos_usb(result.stdout)
        else:
            error = f"Unsupported host platform: {system}"
    except (OSError, subprocess.SubprocessError) as exc:
        error = str(exc)

    response = {"probes": probes, "count": len(probes), "method": method}
    if error:
        response["error"] = error
    if len(probes) == 1:
        response["suggested_probe"] = probes[0]["type"]
    return response


def _family_key(mcu: str) -> str:
    s = (mcu or "").lower().replace("stm32", "").strip()
    if len(s) >= 2 and s[0].isalpha() and s[1].isdigit():
        key = s[0] + s[1]
        if key in _TARGET_CFG:
            return key
    raise ValueError(f"Unknown or unsupported STM32 family for MCU {mcu!r}. Known: {sorted(_TARGET_CFG)}")


# Erased flash does NOT read 0xFF everywhere. The STM32L0/L1 flash technology
# erases to 0x0000_0000; the rest of the STM32 line erases to 0xFF. Measured on an
# STM32L151CCUx over CMSIS-DAP: OpenOCD reported "erased address 0x0803f000
# (length 4096) in 0.248094s" and the range read back all 0x00. Assuming 0xFF made
# every successful erase on those parts look like a failure.
_ERASED_BYTE_BY_FAMILY = {"l0": 0x00, "l1": 0x00}


def erased_byte_for(mcu: str | None) -> int | None:
    """The byte erased flash reads back as on ``mcu``, or None if the part is unknown."""
    try:
        return _ERASED_BYTE_BY_FAMILY.get(_family_key(mcu or ""), 0xFF)
    except ValueError:
        return None


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
