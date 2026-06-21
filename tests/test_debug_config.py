from mcp_server.debug_config import (
    load_debug_config,
    save_debug_config,
    validate_debug_config,
)


def test_validate_debug_config_accepts_expected_project_shape():
    config = {
        "mcu": "STM32F407VG",
        "probe": "jlink",
        "server_type": "jlink",
        "server_args": ["-device", "STM32F407VG", "-if", "SWD"],
        "elf_path": "build/app.elf",
        "svd_path": "STM32F407.svd",
        "rtt": {"command": "JLinkRTTClient", "args": ["-Device", "STM32F407VG"]},
        "uart": {"port": "COM7", "baudrate": 115200, "timeout": 0.1},
    }

    result = validate_debug_config(config)

    assert result["valid"] is True
    assert result["errors"] == []
    assert result["warnings"] == []


def test_validate_debug_config_reports_unknown_server_type_and_missing_paths():
    result = validate_debug_config({
        "server_type": "blackmagic",
        "elf_path": "",
        "uart": {"baudrate": "fast"},
    })

    assert result["valid"] is False
    assert "server_type must be one of: jlink, openocd, stlink" in result["errors"]
    assert "elf_path must not be empty when provided" in result["errors"]
    assert "uart.baudrate must be an integer" in result["errors"]


def test_save_and_load_debug_config_round_trips_yaml(tmp_path):
    path = tmp_path / "debug.yaml"
    config = {
        "mcu": "STM32G474RE",
        "server_type": "openocd",
        "server_args": ["-f", "interface/stlink.cfg"],
        "uart": {"port": "COM4", "baudrate": 921600},
    }

    saved = save_debug_config(str(path), config)
    loaded = load_debug_config(str(path))

    assert saved["path"] == str(path)
    assert loaded["config"] == config
    assert loaded["validation"]["valid"] is True
