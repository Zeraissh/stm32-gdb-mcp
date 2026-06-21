from pathlib import Path

import yaml

ALLOWED_SERVER_TYPES = {"openocd", "stlink", "jlink"}
TOP_LEVEL_FIELDS = {
    "mcu",
    "board",
    "probe",
    "server_type",
    "server_args",
    "elf_path",
    "svd_path",
    "project_root",
    "rtt",
    "uart",
    "reset",
    "hil",
    "notes",
}


def load_debug_config(path: str) -> dict:
    config_path = Path(path)
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        data = {}
    return {
        "path": str(config_path),
        "config": data,
        "validation": validate_debug_config(data),
    }


def save_debug_config(path: str, config: dict) -> dict:
    validation = validate_debug_config(config)
    config_path = Path(path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return {
        "path": str(config_path),
        "validation": validation,
    }


def validate_debug_config(config: dict) -> dict:
    errors = []
    warnings = []

    if not isinstance(config, dict):
        return {
            "valid": False,
            "errors": ["config must be an object"],
            "warnings": [],
        }

    unknown = sorted(set(config) - TOP_LEVEL_FIELDS)
    for field in unknown:
        warnings.append(f"unknown top-level field ignored by MCP: {field}")

    server_type = config.get("server_type")
    if server_type is not None and server_type not in ALLOWED_SERVER_TYPES:
        errors.append("server_type must be one of: jlink, openocd, stlink")

    _validate_non_empty_string(config, "elf_path", errors)
    _validate_non_empty_string(config, "svd_path", errors)
    _validate_non_empty_string(config, "project_root", errors)

    server_args = config.get("server_args")
    if server_args is not None and not _is_string_list(server_args):
        errors.append("server_args must be a list of strings")

    _validate_rtt(config.get("rtt"), errors)
    _validate_uart(config.get("uart"), errors)
    _validate_reset(config.get("reset"), errors)
    _validate_hil(config.get("hil"), errors)

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
    }


def _validate_non_empty_string(config: dict, field: str, errors: list[str]):
    value = config.get(field)
    if value is not None and (not isinstance(value, str) or not value.strip()):
        errors.append(f"{field} must not be empty when provided")


def _validate_rtt(rtt, errors: list[str]):
    if rtt is None:
        return
    if not isinstance(rtt, dict):
        errors.append("rtt must be an object")
        return
    command = rtt.get("command")
    if command is not None and (not isinstance(command, str) or not command.strip()):
        errors.append("rtt.command must not be empty when provided")
    args = rtt.get("args")
    if args is not None and not _is_string_list(args):
        errors.append("rtt.args must be a list of strings")


def _validate_uart(uart, errors: list[str]):
    if uart is None:
        return
    if not isinstance(uart, dict):
        errors.append("uart must be an object")
        return
    port = uart.get("port")
    if port is not None and (not isinstance(port, str) or not port.strip()):
        errors.append("uart.port must not be empty when provided")
    baudrate = uart.get("baudrate")
    if baudrate is not None and not isinstance(baudrate, int):
        errors.append("uart.baudrate must be an integer")
    timeout = uart.get("timeout")
    if timeout is not None and not isinstance(timeout, (int, float)):
        errors.append("uart.timeout must be a number")


def _validate_reset(reset, errors: list[str]):
    if reset is None:
        return
    if not isinstance(reset, dict):
        errors.append("reset must be an object")
        return
    strategy = reset.get("strategy")
    if strategy is not None and not isinstance(strategy, str):
        errors.append("reset.strategy must be a string")
    command = reset.get("command")
    if command is not None and (not isinstance(command, str) or not command.strip()):
        errors.append("reset.command must not be empty when provided")
    halt = reset.get("halt")
    if halt is not None and not isinstance(halt, bool):
        errors.append("reset.halt must be a boolean")


def _validate_hil(hil, errors: list[str]):
    if hil is None:
        return
    if not isinstance(hil, dict):
        errors.append("hil must be an object")
        return
    for field in ("flash", "halt", "read_cpuid", "read_dbgmcu_idcode"):
        value = hil.get(field)
        if value is not None and not isinstance(value, bool):
            errors.append(f"hil.{field} must be a boolean")


def _is_string_list(value):
    return isinstance(value, list) and all(isinstance(item, str) for item in value)
