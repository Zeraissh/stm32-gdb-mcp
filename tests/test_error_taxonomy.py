from mcp_server.error_taxonomy import classify_error


def test_gdb_timeout_maps_to_unresponsive_with_halt_suggestion():
    c = classify_error("Did not get response from gdb after 1.0 seconds")

    assert c["code"] == "target_unresponsive"
    assert c["retryable"] is True
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


def test_unknown_error_falls_back():
    c = classify_error("something totally unexpected")

    assert c["code"] == "tool_execution_error"
    assert c["retryable"] is False
