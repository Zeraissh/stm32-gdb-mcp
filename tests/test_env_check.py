import json

import pytest

from mcp_server import env_check


def _available(monkeypatch, names):
    monkeypatch.setattr(
        env_check,
        "_which",
        lambda executable: f"/tools/{executable}" if executable in names else None,
    )


def test_environment_is_ready_with_gdb_and_one_backend(monkeypatch, capsys):
    _available(monkeypatch, {"arm-none-eabi-gdb", "openocd"})

    assert env_check.check_env() is True

    output = capsys.readouterr().out
    assert "OpenOCD" in output
    assert "J-Link GDB Server" in output
    assert "ready" in output.lower()


def test_environment_is_not_ready_without_gdb(monkeypatch):
    _available(monkeypatch, {"openocd"})

    assert env_check.check_env() is False


def test_environment_is_not_ready_without_any_backend(monkeypatch):
    _available(monkeypatch, {"arm-none-eabi-gdb"})

    assert env_check.check_env() is False


def test_json_mode_reports_available_backends_and_exit_code(monkeypatch, capsys):
    _available(monkeypatch, {"arm-none-eabi-gdb", "st-util"})

    assert env_check.main(["--json"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["ready"] is True
    assert report["gdb"]["found"] is True
    assert report["available_backends"] == ["stlink"]


def test_json_mode_returns_nonzero_when_environment_is_not_ready(monkeypatch, capsys):
    _available(monkeypatch, set())

    assert env_check.main(["--json"]) == 1
    assert json.loads(capsys.readouterr().out)["ready"] is False


def test_environment_report_surfaces_package_and_entrypoint_drift(monkeypatch):
    found = {
        "arm-none-eabi-gdb": "/tools/gdb",
        "openocd": "/tools/openocd",
        "stm32-gdb-mcp": "/old/stm32-gdb-mcp",
    }
    monkeypatch.setattr(env_check, "_which", found.get)
    monkeypatch.setattr(env_check.metadata, "version", lambda _name: "0.3.0")

    report = env_check.environment_report()

    installation = report["installation"]
    assert installation["module_version"] == env_check.MODULE_VERSION
    assert installation["distribution_version"] == "0.3.0"
    assert installation["version_match"] is False
    assert installation["console_scripts"]["stm32-gdb-mcp"]["found"] is True
    assert installation["console_scripts"]["stm32-gdb-mcp-check-env"]["found"] is False
    assert installation["warnings"]


def test_version_flag_reports_module_version(capsys):
    assert env_check.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == env_check.MODULE_VERSION


def test_gdb_multiarch_is_accepted_when_arm_none_eabi_gdb_is_missing(monkeypatch):
    monkeypatch.delenv(env_check.GDB_ENV_VAR, raising=False)
    _available(monkeypatch, {"gdb-multiarch", "openocd"})

    report = env_check.environment_report()

    assert report["ready"] is True
    assert report["gdb"]["path"] == "/tools/gdb-multiarch"


def test_arm_none_eabi_gdb_wins_over_gdb_multiarch(monkeypatch):
    monkeypatch.delenv(env_check.GDB_ENV_VAR, raising=False)
    _available(monkeypatch, {"arm-none-eabi-gdb", "gdb-multiarch"})

    assert env_check.resolve_gdb() == "/tools/arm-none-eabi-gdb"


def test_gdb_env_override_is_used_and_not_silently_replaced(monkeypatch):
    _available(monkeypatch, {"arm-none-eabi-gdb", "my-gdb"})
    monkeypatch.setenv(env_check.GDB_ENV_VAR, "my-gdb")
    assert env_check.resolve_gdb() == "/tools/my-gdb"

    # A broken override must surface as missing, not fall back to another GDB.
    monkeypatch.setenv(env_check.GDB_ENV_VAR, "missing-gdb")
    assert env_check.resolve_gdb() is None


@pytest.mark.parametrize("platform", ["windows", "macos", "linux"])
def test_every_platform_gets_install_commands_for_gdb_and_a_backend(platform):
    assert env_check.install_commands(env_check.GDB, platform)
    assert any(env_check.install_commands(info, platform) for info in env_check.BACKENDS.values())


def test_not_ready_output_prints_this_platforms_install_commands(monkeypatch, capsys):
    monkeypatch.delenv(env_check.GDB_ENV_VAR, raising=False)
    monkeypatch.setattr(env_check, "host_platform", lambda: "macos")
    _available(monkeypatch, set())

    assert env_check.check_env() is False

    output = capsys.readouterr().out
    assert "brew install --cask gcc-arm-embedded" in output
    assert "brew install open-ocd" in output
    assert "scoop" not in output
    assert "apt install" not in output


def test_json_report_carries_platform_and_install_commands(monkeypatch, capsys):
    monkeypatch.delenv(env_check.GDB_ENV_VAR, raising=False)
    monkeypatch.setattr(env_check, "host_platform", lambda: "windows")
    _available(monkeypatch, set())

    env_check.main(["--json"])

    report = json.loads(capsys.readouterr().out)
    assert report["platform"] == "windows"
    assert "scoop install gcc-arm-none-eabi" in report["gdb"]["install_commands"]
    assert report["backends"]["openocd"]["install_commands"] == ["scoop install openocd"]
    assert report["backends"]["jlink"]["install_commands"] == []
