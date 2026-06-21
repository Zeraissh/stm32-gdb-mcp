import pytest

from mcp_server.reset_strategy import resolve_reset_command


def test_openocd_default_preserves_existing_halt_and_run_commands():
    assert resolve_reset_command("openocd", halt=True) == {
        "server_type": "openocd",
        "strategy": "default",
        "command": "monitor reset halt",
    }
    assert resolve_reset_command("openocd", halt=False)["command"] == "monitor reset run"


def test_probe_specific_strategies_resolve_to_monitor_commands():
    assert resolve_reset_command("openocd", halt=True, strategy="software")["command"] == "monitor soft_reset_halt"
    assert resolve_reset_command("jlink", halt=True, strategy="under_reset")["command"] == "monitor reset halt"
    assert resolve_reset_command("stlink", halt=False, strategy="software")["command"] == "monitor reset run"


def test_custom_reset_command_overrides_strategy():
    result = resolve_reset_command("openocd", halt=True, strategy="software", command="monitor reset init")

    assert result == {
        "server_type": "openocd",
        "strategy": "custom",
        "command": "monitor reset init",
    }


def test_unknown_reset_strategy_raises_clear_error():
    with pytest.raises(ValueError, match="Unsupported reset strategy"):
        resolve_reset_command("openocd", halt=True, strategy="bad")
