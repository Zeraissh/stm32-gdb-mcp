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
