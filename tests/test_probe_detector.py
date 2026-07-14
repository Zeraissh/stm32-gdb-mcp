"""Unit tests for probe auto-detection."""

import pytest
from unittest.mock import Mock, patch
from mcp_server.probe_detector import (
    DetectedProbe,
    detect_probes,
    _identify_probe,
    format_probe_list,
)


class TestDetectedProbe:
    """Tests for DetectedProbe dataclass."""

    def test_to_dict(self):
        probe = DetectedProbe(
            type="st-link",
            serial="ABC123",
            manufacturer="STMicroelectronics",
            product="ST-Link/V2",
            location="1:1",
        )
        d = probe.to_dict()
        assert d["type"] == "st-link"
        assert d["serial"] == "ABC123"
        assert d["manufacturer"] == "STMicroelectronics"
        assert d["product"] == "ST-Link/V2"
        assert d["location"] == "1:1"


class MockDevice:
    """Mock USB device for testing."""

    def __init__(self, idVendor, idProduct, iManufacturer=None, iProduct=None, iSerialNumber=None):
        self.idVendor = idVendor
        self.idProduct = idProduct
        self.iManufacturer = iManufacturer
        self.iProduct = iProduct
        self.iSerialNumber = iSerialNumber
        self.bus = 1
        self.address = 1

    def ctrl_transfer(self, *args, **kwargs):
        # Mock ctrl_transfer return value (string descriptor)
        return bytes([0, 0]) + b"Mock\x00String\x00".encode("utf-16-le")


class TestIdentifyProbe:
    """Tests for _identify_probe function."""

    def test_st_link_v2(self):
        device = MockDevice(0x0483, 0x3740)
        device.iManufacturer = 1
        device.iProduct = 2
        device.iSerialNumber = 3
        probe = _identify_probe(device)
        assert probe is not None
        assert probe.type == "st-link"
        assert probe.vid_pid == (0x0483, 0x3740)

    def test_st_link_v2_1(self):
        device = MockDevice(0x0483, 0x3741)
        probe = _identify_probe(device)
        assert probe is not None
        assert probe.type == "st-link"

    def test_st_link_v3(self):
        device = MockDevice(0x0483, 0x3748)
        probe = _identify_probe(device)
        assert probe is not None
        assert probe.type == "st-link"

    def test_jlink_segger(self):
        device = MockDevice(0x1366, 0x0101)
        probe = _identify_probe(device)
        assert probe is not None
        assert probe.type == "j-link"

    def test_cmsis_dap_by_product_string(self):
        device = MockDevice(0x0D28, 0x0204)  # mbed DAPLink
        device.iProduct = 2
        
        with patch('mcp_server.probe_detector._get_string') as mock_get_string:
            mock_get_string.side_effect = lambda d, idx: "mbed DAPLink" if idx == 2 else None
            probe = _identify_probe(device)
            assert probe is not None
            assert probe.type == "daplink"

    def test_cmsis_dap_generic(self):
        device = MockDevice(0xFFFF, 0xFFFF)  # Unknown VID:PID
        device.iProduct = 2
        
        with patch('mcp_server.probe_detector._get_string') as mock_get_string:
            mock_get_string.side_effect = lambda d, idx: "CMSIS-DAP v2" if idx == 2 else None
            probe = _identify_probe(device)
            assert probe is not None
            assert probe.type == "cmsis-dap"

    def test_unknown_device(self):
        device = MockDevice(0xFFFF, 0xFFFF)
        with patch('mcp_server.probe_detector._get_string') as mock_get_string:
            mock_get_string.return_value = None
            probe = _identify_probe(device)
            assert probe is None

    def test_device_with_serial_number(self):
        device = MockDevice(0x0483, 0x3740)
        device.iSerialNumber = 3
        
        with patch('mcp_server.probe_detector._get_serial_number') as mock_serial:
            mock_serial.return_value = "ABC123DEF456"
            probe = _identify_probe(device)
            assert probe is not None
            assert probe.serial == "ABC123DEF456"

    def test_exception_handling(self):
        device = Mock()
        device.idVendor = property(Mock(side_effect=Exception("USB error")))
        probe = _identify_probe(device)
        assert probe is None


class TestDetectProbes:
    """Tests for detect_probes function."""

    def test_no_probes_found(self):
        with patch('usb.core.find') as mock_find:
            mock_find.return_value = []
            probes = detect_probes()
            assert probes == []

    def test_single_st_link_found(self):
        device = MockDevice(0x0483, 0x3740)
        with patch('usb.core.find') as mock_find:
            mock_find.return_value = [device]
            probes = detect_probes()
            assert len(probes) == 1
            assert probes[0].type == "st-link"

    def test_multiple_probes_found(self):
        devices = [
            MockDevice(0x0483, 0x3740),  # ST-Link
            MockDevice(0x1366, 0x0101),  # J-Link
        ]
        with patch('usb.core.find') as mock_find:
            mock_find.return_value = devices
            probes = detect_probes()
            assert len(probes) == 2
            assert probes[0].type == "st-link"
            assert probes[1].type == "j-link"

    def test_usb_enumeration_error(self):
        with patch('usb.core.find') as mock_find:
            mock_find.side_effect = Exception("USB error")
            probes = detect_probes()
            assert probes == []

    def test_pyusb_not_installed(self):
        # If pyusb import fails, detect_probes should return empty list
        with patch.dict('sys.modules', {'usb': None, 'usb.core': None}):
            # Re-import to trigger ImportError
            probes = detect_probes()
            assert probes == []


class TestFormatProbeList:
    """Tests for format_probe_list function."""

    def test_no_probes(self):
        text = format_probe_list([])
        assert "No debug probes detected" in text

    def test_single_probe(self):
        probe = DetectedProbe(
            type="st-link",
            serial="ABC123",
            manufacturer="STMicroelectronics",
            product="ST-Link/V2",
            location="1:1",
        )
        text = format_probe_list([probe])
        assert "1 debug probe" in text
        assert "ST-LINK" in text or "ST-Link" in text.upper()
        assert "ABC123" in text

    def test_multiple_probes(self):
        probes = [
            DetectedProbe(
                type="st-link",
                serial="ABC123",
                manufacturer="STMicroelectronics",
                product="ST-Link/V2",
                location="1:1",
            ),
            DetectedProbe(
                type="j-link",
                serial="XYZ789",
                manufacturer="SEGGER",
                product="J-Link",
                location="1:2",
            ),
        ]
        text = format_probe_list(probes)
        assert "2 debug probes" in text
        assert "ST-LINK" in text or "1." in text
        assert "J-LINK" in text or "2." in text
        assert "ABC123" in text
        assert "XYZ789" in text

    def test_verbose_mode(self):
        probe = DetectedProbe(
            type="st-link",
            serial="ABC123",
            manufacturer="STMicroelectronics",
            product="ST-Link/V2",
            location="1:1",
        )
        verbose_text = format_probe_list([probe], verbose=True)
        non_verbose_text = format_probe_list([probe], verbose=False)
        # Verbose mode should include manufacturer
        assert "STMicroelectronics" in verbose_text or verbose_text != non_verbose_text


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
