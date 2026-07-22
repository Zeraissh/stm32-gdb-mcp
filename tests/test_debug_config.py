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


def test_validate_debug_config_accepts_reset_and_hil_sections():
    result = validate_debug_config({
        "server_type": "openocd",
        "reset": {"strategy": "under_reset", "halt": True},
        "hil": {
            "read_cpuid": True,
            "read_dbgmcu_idcode": True,
            "flash": False,
            "expected_core": "Cortex-M4",
            "expected_device": "STM32L43",
        },
    })

    assert result["valid"] is True
    assert result["errors"] == []


def test_validate_debug_config_rejects_invalid_reset_and_hil_sections():
    result = validate_debug_config({
        "reset": {"strategy": 123, "halt": "yes"},
        "hil": {"flash": "sometimes", "expected_core": ""},
    })

    assert result["valid"] is False
    assert "reset.strategy must be a string" in result["errors"]
    assert "reset.halt must be a boolean" in result["errors"]
    assert "hil.flash must be a boolean" in result["errors"]
    assert "hil.expected_core must be a non-empty string" in result["errors"]


def test_load_resolves_runtime_paths_relative_to_config_file(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    path = config_dir / "debug.yaml"
    path.write_text(
        "\n".join([
            "serial: 066BFF",
            "elf_path: ../build/app.elf",
            "svd_path: svd/device.svd",
            "project_root: ..",
            "swo:",
            "  file: logs/swo.log",
        ]),
        encoding="utf-8",
    )

    loaded = load_debug_config(str(path))

    assert loaded["path"] == str(path.resolve())
    assert loaded["config"]["elf_path"] == str((config_dir / "../build/app.elf").resolve())
    assert loaded["config"]["svd_path"] == str((config_dir / "svd/device.svd").resolve())
    assert loaded["config"]["project_root"] == str(tmp_path.resolve())
    assert loaded["config"]["swo"]["file"] == str((config_dir / "logs/swo.log").resolve())
    assert loaded["validation"] == {"valid": True, "errors": [], "warnings": []}


def test_validate_debug_config_rejects_invalid_serial_and_swo():
    result = validate_debug_config({
        "serial": "",
        "swo": {"file": 123, "args": "--raw"},
    })

    assert result["valid"] is False
    assert "serial must not be empty when provided" in result["errors"]
    assert "swo.file must be a string" in result["errors"]
    assert "swo.args must be a list of strings" in result["errors"]
