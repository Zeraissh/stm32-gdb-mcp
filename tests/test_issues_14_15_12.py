"""Unit tests for issues #14, #15, and #12.

#15  Keil build_firmware ignores requested target
#14  read_variable and read_memory omit returned values (empty-string guard)
#12  Auto-detect probe type (detect_probe tool)
"""

import re
import unittest.mock as mock

import pytest

from mcp_server.build import parse_keil_built_target, resolve_build_command
from mcp_server.gdb_decode import decode_evaluated_value, decode_memory_bytes
from mcp_server.openocd_config import detect_probe


# ---------------------------------------------------------------------------
# Issue #15: Keil -t target
# ---------------------------------------------------------------------------

def test_keil_command_includes_target_when_given():
    cmd = resolve_build_command("keil", project="fw.uvprojx", target="Release", uv4_path="UV4.exe")
    assert "-t" in cmd
    idx = cmd.index("-t")
    assert cmd[idx + 1] == "Release"


def test_keil_command_omits_t_flag_when_no_target():
    cmd = resolve_build_command("keil", project="fw.uvprojx", uv4_path="UV4.exe")
    assert "-t" not in cmd


def test_keil_target_placed_before_log_option():
    cmd = resolve_build_command("keil", project="fw.uvprojx", target="Debug", log_path="b.log", uv4_path="UV4.exe")
    # -t Debug must appear before -o b.log
    assert cmd.index("-t") < cmd.index("-o")


def test_parse_keil_built_target_extracts_build_target():
    log = "Build target 'Release'\n** 0 Errors, 0 Warnings\n"
    assert parse_keil_built_target(log) == "Release"


def test_parse_keil_built_target_extracts_rebuild_target():
    log = "Rebuild target 'Debug'\n** 0 Errors, 0 Warnings\n"
    assert parse_keil_built_target(log) == "Debug"


def test_parse_keil_built_target_returns_none_on_no_match():
    assert parse_keil_built_target("No target line here") is None
    assert parse_keil_built_target("") is None
    assert parse_keil_built_target(None) is None


# ---------------------------------------------------------------------------
# Issue #14: empty-string guard in decode_memory_bytes / decode_evaluated_value
# ---------------------------------------------------------------------------

def _result(key, value):
    return [{"type": "result", "message": "done", "payload": {key: value}}]


# decode_memory_bytes

def test_decode_memory_bytes_returns_none_on_empty_contents():
    records = [{"type": "result", "payload": {"memory": [{"contents": ""}]}}]
    assert decode_memory_bytes(records) is None


def test_decode_memory_bytes_returns_none_on_whitespace_contents():
    records = [{"type": "result", "payload": {"memory": [{"contents": "   "}]}}]
    assert decode_memory_bytes(records) is None


def test_decode_memory_bytes_still_returns_valid_contents():
    records = [{"type": "result", "payload": {"memory": [{"contents": "deadbeef"}]}}]
    assert decode_memory_bytes(records) == "deadbeef"


def test_decode_memory_bytes_falls_through_empty_to_next_record():
    """Empty first record should not short-circuit; valid second record wins."""
    records = [
        {"type": "result", "payload": {"memory": [{"contents": ""}]}},
        {"type": "result", "payload": {"memory": [{"contents": "cafebabe"}]}},
    ]
    assert decode_memory_bytes(records) == "cafebabe"


# decode_evaluated_value

def test_decode_evaluated_value_returns_none_on_empty_string():
    records = [{"type": "result", "payload": {"value": ""}}]
    assert decode_evaluated_value(records) is None


def test_decode_evaluated_value_returns_none_on_whitespace():
    records = [{"type": "result", "payload": {"value": "  "}}]
    assert decode_evaluated_value(records) is None


def test_decode_evaluated_value_still_returns_valid_value():
    records = [{"type": "result", "payload": {"value": "0x1234"}}]
    assert decode_evaluated_value(records) == "0x1234"


def test_decode_evaluated_value_skips_empty_falls_through_to_valid():
    records = [
        {"type": "result", "payload": {"value": ""}},
        {"type": "result", "payload": {"value": "42"}},
    ]
    assert decode_evaluated_value(records) == "42"


def test_decode_evaluated_value_skips_error_records():
    records = [
        {"type": "result", "message": "error", "payload": {"msg": "No symbol"}},
        {"type": "result", "payload": {"value": "7"}},
    ]
    assert decode_evaluated_value(records) == "7"


# ---------------------------------------------------------------------------
# Issue #12: detect_probe
# ---------------------------------------------------------------------------

def test_detect_probe_uses_openocd_when_available():
    fake_output = (
        "Open On-Chip Debugger\n"
        " 0: ST-Link v2  vid=0x0483 pid=0x374b serial=ABC123\n"
    )
    with mock.patch("subprocess.run") as m_run:
        m_run.return_value = mock.Mock(stdout=fake_output, stderr="", returncode=0)
        with mock.patch("shutil.which", return_value="/usr/bin/openocd"):
            result = detect_probe()
    assert result["method"] == "openocd"
    assert len(result["probes"]) == 1
    assert result["probes"][0]["type"] == "stlink"
    assert result.get("suggested_probe") == "stlink"


def test_detect_probe_returns_cmsis_dap():
    fake_output = "  0: CMSIS-DAP DAPLink vid=0x0d28\n"
    with mock.patch("subprocess.run") as m_run:
        m_run.return_value = mock.Mock(stdout=fake_output, stderr="", returncode=0)
        with mock.patch("shutil.which", return_value="/usr/bin/openocd"):
            result = detect_probe()
    assert result["probes"][0]["type"] == "cmsis-dap"


def test_detect_probe_no_suggested_when_multiple():
    fake_output = (
        "  0: ST-Link v2\n"
        "  1: CMSIS-DAP DAPLink\n"
    )
    with mock.patch("subprocess.run") as m_run:
        m_run.return_value = mock.Mock(stdout=fake_output, stderr="", returncode=0)
        with mock.patch("shutil.which", return_value="/usr/bin/openocd"):
            result = detect_probe()
    assert len(result["probes"]) == 2
    assert "suggested_probe" not in result


def test_detect_probe_returns_empty_when_nothing_found():
    with mock.patch("subprocess.run") as m_run:
        m_run.return_value = mock.Mock(stdout="", stderr="", returncode=0)
        with mock.patch("shutil.which", return_value="/usr/bin/openocd"):
            result = detect_probe()
    assert result["probes"] == []
    assert "suggested_probe" not in result


def test_detect_probe_falls_back_to_usb_when_openocd_absent():
    with mock.patch("shutil.which", return_value=None):
        with mock.patch("mcp_server.openocd_config._detect_probe_lsusb", return_value=[
            {"type": "stlink", "product": "ST-Link/V2"}
        ]):
            with mock.patch("mcp_server.openocd_config._detect_probe_windows", return_value=[]):
                import platform
                with mock.patch.object(platform, "system", return_value="Linux"):
                    result = detect_probe()
    assert result["method"] == "usb_lsusb"
    assert result["probes"][0]["type"] == "stlink"
    assert result.get("suggested_probe") == "stlink"
