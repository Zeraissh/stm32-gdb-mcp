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


# --- measured on hardware: a CMSIS-DAP v2 probe is a USB composite device ---

# Verbatim from Get-PnpDevice with a Horco CMSIS-DAP v2 and an ST-Link V2 both
# plugged in. Only the MI_00 interface carries the name that identifies the
# probe; the parent it hangs off is called "USB Composite Device".
_DAPLINK_AND_STLINK = json.dumps([
    {"InstanceId": r"USB\VID_FAED&PID_4870&MI_00\6&39E7DCD8&1&0000",
     "FriendlyName": "Horco CMSIS-DAP v2", "Manufacturer": "WinUSB Device"},
    {"InstanceId": r"USB\VID_FAED&PID_4870&MI_01\6&39E7DCD8&1&0001",
     "FriendlyName": "USB Input Device"},
    {"InstanceId": r"USB\VID_FAED&PID_4870\132765404453",
     "FriendlyName": "USB Composite Device"},
    {"InstanceId": r"USB\VID_FAED&PID_4870&MI_02\6&39E7DCD8&1&0002",
     "FriendlyName": "USB Serial Device (COM4)"},
    {"InstanceId": r"HID\VID_FAED&PID_4870&MI_01\7&1872EE86&0&0000",
     "FriendlyName": "HID-compliant vendor-defined device"},
    {"InstanceId": r"USB\VID_0483&PID_3748\000000000001",
     "FriendlyName": "STM32 STLink", "Manufacturer": "STMicroelectronics"},
])


def test_a_composite_cmsis_dap_probe_is_detected_alongside_an_stlink():
    probes = _parse_windows_pnp(_DAPLINK_AND_STLINK)

    by_type = {p["type"]: p for p in probes}
    assert set(by_type) == {"cmsis-dap", "stlink"}, probes
    assert by_type["cmsis-dap"]["product"] == "Horco CMSIS-DAP v2"
    # Dropping every &MI_ node hid this probe entirely, so detect_probe reported a
    # single ST-Link and start_debug_session auto-selected the wrong one.
    assert len(probes) == 2


def test_a_composite_probe_takes_its_serial_from_the_parent_node():
    probes = _parse_windows_pnp(_DAPLINK_AND_STLINK)

    dap = next(p for p in probes if p["type"] == "cmsis-dap")
    # The interface node has no serial of its own; OpenOCD needs one to target a
    # specific board with `adapter serial`.
    assert dap["serial"] == "132765404453"


def test_the_other_interfaces_of_a_composite_probe_do_not_become_probes():
    probes = _parse_windows_pnp(_DAPLINK_AND_STLINK)

    # MI_01 (HID) and MI_02 (COM port) belong to the same physical probe.
    assert [p["type"] for p in probes].count("cmsis-dap") == 1


def test_two_identical_unserialised_probes_are_not_collapsed():
    payload = json.dumps([
        {"InstanceId": r"USB\VID_FAED&PID_4870&MI_00\6&AAAA&1&0000",
         "FriendlyName": "Horco CMSIS-DAP v2"},
        {"InstanceId": r"USB\VID_FAED&PID_4870&MI_00\6&BBBB&1&0000",
         "FriendlyName": "Horco CMSIS-DAP v2"},
    ])

    probes = _parse_windows_pnp(payload)

    # No unambiguous parent serial to borrow, so they stay apart on hub path.
    assert len(probes) == 2
    assert len({p["location"] for p in probes}) == 2
