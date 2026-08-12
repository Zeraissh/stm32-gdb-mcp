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


def test_a_strategy_that_really_differs_is_never_called_an_alias():
    # This used to assert `"note" not in resolved`, using the absence of any note as a
    # proxy for "not falsely called an alias" -- true while the alias note was the only
    # note there was. 'software' now carries a separate, accurate warning about
    # SYSRESETREQ, so the assertion is narrowed to the claim it was actually protecting:
    # a strategy that resolves to its own command must never be told it is an alias.
    resolved = resolve_reset_command("openocd", halt=True, strategy="software")

    assert resolved["command"] == "monitor soft_reset_halt"
    assert "resolves to the same command" not in resolved.get("note", "")


def test_software_reset_warns_that_rcc_csr_will_not_record_it():
    # Measured on an STM32L151 while proving a cold boot: strategy="software" left
    # RCC_CSR at 0x0C000000 because soft_reset_halt never asserts SYSRESETREQ, while
    # strategy="default" moved it to 0x1C000000. Without this note the cold reset that
    # follows looks like it did nothing -- the sticky flag it should clear was never
    # set in the first place, so the standard cold-boot proof silently fails.
    resolved = resolve_reset_command("openocd", halt=True, strategy="software")

    assert resolved["command"] == "monitor soft_reset_halt"
    assert "SYSRESETREQ" in resolved["note"]
    assert "RCC_CSR" in resolved["note"]


def test_the_soft_reset_warning_only_fires_where_soft_reset_halt_is_actually_used():
    # stlink and jlink map "software" onto monitor reset halt, so the warning would be
    # false there -- and they get the alias note instead, which is the true statement.
    for server in ("stlink", "jlink"):
        resolved = resolve_reset_command(server, halt=True, strategy="software")
        assert resolved["command"] == "monitor reset halt"
        assert "SYSRESETREQ" not in resolved.get("note", "")

    # halt=False on openocd resolves to monitor reset run, not soft_reset_halt.
    assert "SYSRESETREQ" not in resolve_reset_command(
        "openocd", halt=False, strategy="software").get("note", "")
