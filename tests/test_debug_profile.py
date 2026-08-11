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


# FIX 3 (profile merge)
def test_debug_profile_store_replaces_a_nested_block_by_default():
    store = DebugProfileStore()
    store.update({"hub": {"guard": "confirm",
                          "map": {"3": {"key": "instance_id:A"}, "4": {"key": "instance_id:B"}}}})

    profile = store.update({"hub": {"map": {"3": {"label": "TC"}}}})

    # Replace is the only way to DROP a channel, and a stale hub.map entry is not
    # cosmetic: channel selection takes the first matching entry and the hub tools
    # then cut that port's power.
    assert profile["hub"] == {"map": {"3": {"label": "TC"}}}


# FIX 3 (profile merge)
def test_debug_profile_store_deep_merges_nested_blocks_when_asked():
    store = DebugProfileStore()
    store.update({"hub": {"guard": "confirm",
                          "map": {"3": {"key": "instance_id:A"}, "4": {"key": "instance_id:B"}}}})

    profile = store.update({"hub": {"map": {"3": {"label": "TC"}}}}, merge=True)

    assert profile["hub"]["map"]["3"] == {"key": "instance_id:A", "label": "TC"}
    assert profile["hub"]["map"]["4"] == {"key": "instance_id:B"}
    assert profile["hub"]["guard"] == "confirm"


# FIX 3 (profile merge)
def test_debug_profile_store_merge_replaces_lists_and_scalars():
    store = DebugProfileStore()
    store.update({"rtt": {"command": "RTTClient", "args": ["--device", "STM32L431"]}})

    profile = store.update({"rtt": {"args": ["--device", "STM32L151"]}}, merge=True)

    # A list has no key to merge on; a half-merged args list is a command line
    # nobody wrote.
    assert profile["rtt"] == {"command": "RTTClient", "args": ["--device", "STM32L151"]}


# FIX 3 (profile merge)
def test_debug_profile_store_merge_still_rejects_unknown_fields():
    store = DebugProfileStore()

    try:
        store.update({"merge": True}, merge=True)
    except ValueError as exc:
        assert "merge" in str(exc)
    else:
        raise AssertionError("Expected ValueError")

