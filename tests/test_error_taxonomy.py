import pytest

from mcp_server.error_taxonomy import (
    classify_error,
    measured_target_voltage,
    refine_target_unreachable,
)


def test_gdb_timeout_maps_to_unresponsive_with_halt_suggestion():
    c = classify_error("Did not get response from gdb after 1.0 seconds")

    assert c["code"] == "target_unresponsive"
    assert c["retryable"] is False
    assert "halt_execution" in c["suggested_next_actions"]


def test_no_session_maps_to_start_session():
    c = classify_error("GDB is not running.")

    assert c["code"] == "no_session"
    assert "start_debug_session" in c["suggested_next_actions"]


def test_no_symbols_suggests_flashing():
    c = classify_error('No symbol "g_state" in current context.')

    assert c["code"] == "no_symbols"
    assert "flash_firmware" in c["suggested_next_actions"]


def test_memory_access_error_suggests_halt():
    c = classify_error("Cannot access memory at address 0x0")

    assert c["code"] == "memory_access"
    assert "halt_execution" in c["suggested_next_actions"]


def test_probe_open_failure_is_retryable_with_recover_suggestion():
    c = classify_error("Error: open failed")

    assert c["code"] == "probe_busy"
    assert c["retryable"] is True
    assert "recover_session" in c["suggested_next_actions"]


def test_openocd_missing_config_is_a_non_retryable_config_error():
    # The exact field error: openocd started with no -f config args.
    msg = ("GDB server failed to start. Logs: ... embedded:startup.tcl:72: Error: "
           "Can't find openocd.cfg ... Error: Debug Adapter has to be specified, "
           "see \"adapter driver\" command")
    c = classify_error(msg)

    # Must NOT be misclassified as a retryable probe issue — retrying won't help.
    assert c["code"] == "invalid_target_config"
    assert c["retryable"] is False
    assert "load_debug_config" in c["suggested_next_actions"]


def test_u535_target_failure_is_not_misreported_or_retried():
    c = classify_error(
        "Error: init mode failed (unable to connect to target)\n"
        "Error: target stm32u5x.cpu examination failed\n"
        "Error: open failed"
    )

    assert c["code"] == "target_unreachable"
    assert c["retryable"] is False
    assert "recover_session" not in c["suggested_next_actions"]


def test_debug_authentication_failure_is_actionable_and_not_retried():
    c = classify_error("STM32U5 device is locked. Debug Authentication is required.")

    assert c["code"] == "debug_auth_required"
    assert c["retryable"] is False


def test_missing_backend_executable_is_not_retried():
    c = classify_error("[WinError 2] The system cannot find the file specified: 'openocd'")

    assert c["code"] == "tool_missing"
    assert c["retryable"] is False


def test_unknown_error_falls_back():
    c = classify_error("something totally unexpected")

    assert c["code"] == "tool_execution_error"
    assert c["retryable"] is False


def test_bad_elf_path_is_not_mistaken_for_a_missing_host_tool():
    # GDB says "No such file or directory" for a bad ELF path too; routing that to
    # "install a missing toolchain" sent agents debugging the wrong thing.
    result = classify_error("load_symbols(C:/proj/fw.elf) failed: fw.elf: No such file or directory.")

    assert result["code"] == "elf_load_failed"
    assert "debug_profile(action=set, elf_path=...)" in result["suggested_next_actions"]


def test_missing_gdb_executable_still_classifies_as_a_missing_tool():
    result = classify_error("[WinError 2] The system cannot find the file specified")

    assert result["code"] == "tool_missing"


def test_flash_download_failure_is_retryable_with_reset_guidance():
    result = classify_error("flash download(fw.elf) failed: Error erasing flash with vFlashErase packet")

    assert result["code"] == "flash_failed"
    assert result["retryable"] is True
    assert "reset_target" in result["suggested_next_actions"]


def test_flash_mismatch_tells_the_agent_the_device_runs_other_code():
    result = classify_error("verify_flash(fw.elf) failed: target flash does not match the ELF — MIS-MATCHED!")

    assert result["code"] == "flash_mismatch"


def test_implausible_register_read_is_not_reported_as_target_state():
    c = classify_error(
        "core register read is implausible: xPSR=0x0 has the Thumb bit (bit 24) clear, "
        "which cannot happen on a halted Cortex-M")

    assert c["code"] == "register_read_implausible"
    assert c["retryable"] is True
    assert "halt_execution" in c["suggested_next_actions"]
    assert "failed read" in c["hint"]


def test_empty_register_read_shares_the_implausible_classification():
    c = classify_error("core register read returned no registers at all")

    assert c["code"] == "register_read_implausible"


# --- issue #36: a dead link during load_symbols is a session fault, not a bad ELF ---

def test_remote_failure_reply_is_a_session_fault_even_under_a_load_symbols_label():
    c = classify_error(
        "load_symbols(D:/Obj/AGS_Normal_BL.axf) failed: Could not read registers; "
        "remote failure reply '0E'")

    assert c["code"] == "connection_lost"
    assert c["retryable"] is True
    assert c["suggested_next_actions"][0] == "recover_session"
    # The cure was stop/start, not inspecting the path.
    assert "inspect_project" not in c["suggested_next_actions"]


def test_a_genuinely_bad_elf_path_is_still_an_elf_load_failure():
    c = classify_error("load_symbols(fw.axf) failed: fw.axf: No such file or directory.")

    assert c["code"] == "elf_load_failed"
    assert c["retryable"] is False


# --- issue #35: do not advise a power check the log already disproves ---

_ATTACH_LOG = (
    "Info : STLINK V2J37S7 (API v2) VID:PID 0483:3748\n"
    "Info : Target voltage: 3.167938\n"
    "Error: init mode failed (unable to connect to the target)\n"
)


def test_measured_voltage_is_read_off_the_server_log():
    assert measured_target_voltage(_ATTACH_LOG) == pytest.approx(3.167938)
    assert measured_target_voltage("no voltage here") is None


def test_a_powered_board_is_not_told_to_check_its_power():
    refined = refine_target_unreachable(classify_error(_ATTACH_LOG), _ATTACH_LOG)

    assert refined["code"] == "target_unreachable"
    assert "check target power/SWD wiring/reset state" not in refined["suggested_next_actions"]
    assert refined["suggested_next_actions"]  # never left empty
    assert "the board IS powered" in refined["hint"]
    assert "3.17 V" in refined["hint"]
    # The inference that cost the reporter two wrong statements to their user.
    assert "says NOTHING about whether the CPU is executing" in refined["hint"]
    assert "connect_assert_srst" in refined["hint"]
    assert refined["measured_target_voltage"] == pytest.approx(3.167938)


def test_an_unpowered_board_keeps_the_power_advice():
    log = "Info : Target voltage: 0.041000\nError: init mode failed (unable to connect to the target)\n"

    refined = refine_target_unreachable(classify_error(log), log)

    assert "check target power/SWD wiring/reset state" in refined["suggested_next_actions"]


def test_no_voltage_reading_leaves_the_classification_alone():
    log = "Error: init mode failed (unable to connect to the target)\n"
    original = classify_error(log)

    assert refine_target_unreachable(original, log) == original


def test_refinement_only_applies_to_target_unreachable():
    original = classify_error("Did not get response from gdb after 1.0 seconds")

    assert refine_target_unreachable(original, _ATTACH_LOG) == original
