"""Auto-detect connected debug probes (ST-Link, CMSIS-DAP, J-Link) via USB enumeration.

Enumerates connected USB devices and identifies debug probes by VID/PID or product string,
extracting their type, serial number, and manufacturer info.
"""

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Known debug probe identifiers
_PROBE_SIGNATURES = {
    # ST-Link (various versions)
    (0x0483, 0x3740): "st-link",
    (0x0483, 0x3741): "st-link",
    (0x0483, 0x3742): "st-link",
    (0x0483, 0x3744): "st-link",
    (0x0483, 0x3745): "st-link",
    (0x0483, 0x3746): "st-link",
    (0x0483, 0x3748): "st-link",
    # J-Link (SEGGER)
    (0x1366, 0x0101): "j-link",
    (0x1366, 0x0102): "j-link",
    (0x1366, 0x0103): "j-link",
    (0x1366, 0x0104): "j-link",
    (0x1366, 0x0105): "j-link",
    (0x1366, 0x0107): "j-link",
    (0x1366, 0x0108): "j-link",
    (0x1366, 0x1010): "j-link",
    (0x1366, 0x1011): "j-link",
    (0x1366, 0x1012): "j-link",
    (0x1366, 0x1014): "j-link",
    (0x1366, 0x1015): "j-link",
    (0x1366, 0x1020): "j-link",
    (0x1366, 0x1051): "j-link",
    (0x1366, 0x1055): "j-link",
    (0x1366, 0x1061): "j-link",
    (0x1366, 0x1070): "j-link",
    (0x1366, 0x1075): "j-link",
    (0x1366, 0x1080): "j-link",
    (0x1366, 0x1200): "j-link",
}


@dataclass
class DetectedProbe:
    """A detected debug probe."""
    type: str  # "st-link", "cmsis-dap", "j-link"
    serial: str | None  # Serial number if available
    manufacturer: str | None  # Manufacturer string
    product: str | None  # Product string
    location: str | None  # Bus/port location for identification
    vid_pid: tuple[int, int] | None = None  # (VID, PID)

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "type": self.type,
            "serial": self.serial,
            "manufacturer": self.manufacturer,
            "product": self.product,
            "location": self.location,
        }


def detect_probes() -> list[DetectedProbe]:
    """Enumerate connected USB devices and return detected debug probes.

    Returns:
        List of DetectedProbe objects, or empty list if none found or USB access unavailable.
    """
    probes = []

    try:
        import usb.core
        import usb.util
    except ImportError:
        logger.warning("pyusb not available; probe detection disabled")
        return probes

    try:
        # Enumerate all USB devices
        devices = usb.core.find(find_all=True)
        for device in devices:
            probe = _identify_probe(device)
            if probe:
                probes.append(probe)
    except Exception as e:
        logger.warning(f"USB enumeration failed: {e}")

    return probes


def _identify_probe(device) -> DetectedProbe | None:
    """Check if a USB device is a known debug probe."""
    try:
        vid = device.idVendor
        pid = device.idProduct
    except Exception:
        return None

    # Check by VID:PID
    if (vid, pid) in _PROBE_SIGNATURES:
        probe_type = _PROBE_SIGNATURES[(vid, pid)]
        serial = _get_serial_number(device)
        manufacturer = _get_string(device, device.iManufacturer)
        product = _get_string(device, device.iProduct)
        location = _get_device_location(device)
        return DetectedProbe(
            type=probe_type,
            serial=serial,
            manufacturer=manufacturer,
            product=product,
            location=location,
            vid_pid=(vid, pid),
        )

    # Check by product string for CMSIS-DAP/DAPLink
    product = _get_string(device, device.iProduct)
    if product:
        product_lower = product.lower()
        if "cmsis-dap" in product_lower or "daplink" in product_lower:
            serial = _get_serial_number(device)
            manufacturer = _get_string(device, device.iManufacturer)
            location = _get_device_location(device)
            # Determine subtype from product string
            probe_type = "cmsis-dap"
            if "daplink" in product_lower:
                probe_type = "daplink"
            return DetectedProbe(
                type=probe_type,
                serial=serial,
                manufacturer=manufacturer,
                product=product,
                location=location,
                vid_pid=(vid, pid),
            )

    return None


def _get_serial_number(device) -> str | None:
    """Extract serial number from device."""
    try:
        if device.iSerialNumber:
            return _get_string(device, device.iSerialNumber)
    except Exception:
        pass
    return None


def _get_string(device, index: int) -> str | None:
    """Get a USB string descriptor."""
    if not index:
        return None
    try:
        return device.ctrl_transfer(
            0x80, 0x06, (0x0300 | index), 0x0409, 255
        ).tobytes()[2:].decode("utf-16-le", "ignore").rstrip("\x00")
    except Exception:
        return None


def _get_device_location(device) -> str | None:
    """Get device location (bus:port) for identification."""
    try:
        if hasattr(device, "bus") and hasattr(device, "address"):
            return f"{device.bus}:{device.address}"
    except Exception:
        pass
    return None


def format_probe_list(probes: list[DetectedProbe], verbose: bool = True) -> str:
    """Format probe list for human-readable output."""
    if not probes:
        return "No debug probes detected."

    lines = []
    if len(probes) == 1:
        lines.append(f"Found 1 debug probe: {probes[0].type.upper()}")
    else:
        lines.append(f"Found {len(probes)} debug probes:")

    for i, probe in enumerate(probes, 1):
        line = f"  {i}. {probe.type.upper()}"
        if probe.serial:
            line += f" (serial: {probe.serial})"
        if probe.manufacturer and verbose:
            line += f" — {probe.manufacturer}"
        if probe.product and verbose and probe.product != probe.manufacturer:
            line += f" {probe.product}"
        lines.append(line)

    return "\n".join(lines)
