import json

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
