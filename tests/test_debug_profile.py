from mcp_server.debug_profile import DebugProfileStore


def test_debug_profile_store_updates_known_fields_and_ignores_none():
    store = DebugProfileStore()

    profile = store.update({
        "mcu": "STM32F407VG",
        "server_type": "openocd",
        "server_args": ["-f", "interface/stlink.cfg"],
        "elf_path": None,
    })

    assert profile["mcu"] == "STM32F407VG"
    assert profile["server_type"] == "openocd"
    assert profile["server_args"] == ["-f", "interface/stlink.cfg"]
    assert "elf_path" not in profile


def test_debug_profile_store_rejects_unknown_fields():
    store = DebugProfileStore()

    try:
        store.update({"unexpected": "value"})
    except ValueError as exc:
        assert "unexpected" in str(exc)
    else:
        raise AssertionError("Expected ValueError")


def test_debug_profile_store_retains_probe_and_logging_defaults():
    store = DebugProfileStore()
    defaults = {
        "serial": "066BFF",
        "rtt": {"command": "RTTClient", "args": ["--device", "STM32L431"]},
        "uart": {"port": "COM7", "baudrate": 921600, "timeout": 0.2},
        "swo": {"file": "logs/swo.log"},
    }

    assert store.update(defaults) == defaults
