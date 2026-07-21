import json

from mcp_server.openocd_config import (
    _classify_usb_probe,
    _detect_linux_sysfs,
    _parse_windows_pnp,
    detect_probe,
)


def test_classify_known_probe_families():
    assert _classify_usb_probe("0483", "374b", "ST-LINK/V2-1", "STMicroelectronics", "A")["type"] == "stlink"
    assert _classify_usb_probe("1366", "0105", "J-Link", "SEGGER", "B")["type"] == "jlink"
    assert _classify_usb_probe("0d28", "0204", "DAPLink CMSIS-DAP", "ARM", "C")["type"] == "cmsis-dap"
    assert _classify_usb_probe("0483", "5740", "STM32 Virtual COM Port", "STMicroelectronics", "D") is None


def test_windows_detection_preserves_two_identical_probes_by_serial():
    payload = json.dumps([
        {"InstanceId": r"USB\VID_0483&PID_374B\SERIAL_A", "FriendlyName": "ST-LINK/V2-1"},
        {"InstanceId": r"USB\VID_0483&PID_374B\SERIAL_B", "FriendlyName": "ST-LINK/V2-1"},
    ])

    probes = _parse_windows_pnp(payload)

    assert [probe["serial"] for probe in probes] == ["SERIAL_A", "SERIAL_B"]


def test_linux_sysfs_detection_reads_structured_usb_attributes(tmp_path):
    for name, serial in (("1-1", "SERIAL_A"), ("1-2", "SERIAL_B")):
        device = tmp_path / name
        device.mkdir()
        (device / "idVendor").write_text("0483\n", encoding="ascii")
        (device / "idProduct").write_text("374b\n", encoding="ascii")
        (device / "product").write_text("ST-LINK/V2-1\n", encoding="utf-8")
        (device / "manufacturer").write_text("STMicroelectronics\n", encoding="utf-8")
        (device / "serial").write_text(serial + "\n", encoding="ascii")

    probes = _detect_linux_sysfs(tmp_path)

    assert [probe["serial"] for probe in probes] == ["SERIAL_A", "SERIAL_B"]


def test_detect_probe_only_suggests_when_one_physical_probe_exists(tmp_path):
    device = tmp_path / "1-1"
    device.mkdir()
    (device / "idVendor").write_text("1366\n", encoding="ascii")
    (device / "idProduct").write_text("0105\n", encoding="ascii")
    (device / "product").write_text("J-Link\n", encoding="utf-8")
    (device / "manufacturer").write_text("SEGGER\n", encoding="utf-8")
    (device / "serial").write_text("J123\n", encoding="ascii")

    result = detect_probe(platform_name="Linux", sysfs_root=tmp_path)

    assert result["method"] == "linux_sysfs"
    assert result["suggested_probe"] == "jlink"
    assert result["probes"][0]["serial"] == "J123"


def test_detect_probe_reports_os_enumeration_failure():
    class Result:
        returncode = 1
        stdout = ""
        stderr = "Get-PnpDevice is unavailable"

    result = detect_probe(platform_name="Windows", runner=lambda *args, **kwargs: Result())

    assert result["count"] == 0
    assert result["method"] == "windows_pnp"
    assert result["error"] == "Get-PnpDevice is unavailable"
