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
