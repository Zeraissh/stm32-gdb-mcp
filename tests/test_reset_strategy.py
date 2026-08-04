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


def test_a_strategy_that_is_only_an_alias_says_so():
    # under_reset resolves to the same command as default for every backend, so
    # selecting it changes nothing — silently, until now.
    resolved = resolve_reset_command("openocd", halt=True, strategy="under_reset")

    assert resolved["command"] == "monitor reset halt"
    assert "'under_reset' resolves to the same command as ['default']" in resolved["note"]
    assert "connect_assert_srst" in resolved["note"]


def test_asking_for_the_default_carries_no_alias_note():
    assert "note" not in resolve_reset_command("openocd", halt=True)


def test_a_strategy_that_really_differs_carries_no_note():
    resolved = resolve_reset_command("openocd", halt=True, strategy="software")

    assert resolved["command"] == "monitor soft_reset_halt"
    assert "note" not in resolved
