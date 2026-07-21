import json
import subprocess

import tomllib

from mcp_server import install_mcp

ENTRY = {
    "command": r"C:\Tools\stm32-gdb-mcp.exe",
    "args": [],
    "env": {"STM32_GDB_MCP_COMPACT": "1"},
}


def _codex_payload(entry=ENTRY):
    return json.dumps(
        {
            "name": "stm32-gdb-mcp",
            "transport": {
                "type": "stdio",
                "command": entry["command"],
                "args": entry["args"],
                "env": entry["env"],
            },
        }
    )


class FakeRunner:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        return self.responses.pop(0)


def _result(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def test_codex_toml_is_valid_on_windows(monkeypatch):
    monkeypatch.setattr(install_mcp, "server_entry", lambda wants_type: dict(ENTRY))

    parsed = tomllib.loads(install_mcp.render_codex_toml())

    server = parsed["mcp_servers"]["stm32-gdb-mcp"]
    assert server["command"] == ENTRY["command"]
    assert server["env"] == ENTRY["env"]


def test_codex_install_adds_and_verifies_missing_server(monkeypatch):
    monkeypatch.setattr(install_mcp, "server_entry", lambda wants_type: dict(ENTRY))
    runner = FakeRunner(
        [
            _result(1, stderr="not found"),
            _result(),
            _result(stdout=_codex_payload()),
        ]
    )

    assert install_mcp.install_codex(runner=runner, candidates=["codex"]) is True

    assert runner.calls[0][-3:] == ["get", "stm32-gdb-mcp", "--json"]
    assert "add" in runner.calls[1]
    assert runner.calls[2][-3:] == ["get", "stm32-gdb-mcp", "--json"]


def test_codex_install_is_idempotent_when_config_matches(monkeypatch):
    monkeypatch.setattr(install_mcp, "server_entry", lambda wants_type: dict(ENTRY))
    runner = FakeRunner([_result(stdout=_codex_payload())])

    assert install_mcp.install_codex(runner=runner, candidates=["codex"]) is True
    assert len(runner.calls) == 1


def test_codex_install_rejects_conflict_without_force(monkeypatch):
    monkeypatch.setattr(install_mcp, "server_entry", lambda wants_type: dict(ENTRY))
    conflict = {**ENTRY, "command": r"C:\Other\server.exe"}
    runner = FakeRunner([_result(stdout=_codex_payload(conflict))])

    assert install_mcp.install_codex(runner=runner, candidates=["codex"]) is False
    assert len(runner.calls) == 1


def test_codex_install_force_replaces_conflicting_server(monkeypatch):
    monkeypatch.setattr(install_mcp, "server_entry", lambda wants_type: dict(ENTRY))
    conflict = {**ENTRY, "command": r"C:\Other\server.exe"}
    runner = FakeRunner(
        [
            _result(stdout=_codex_payload(conflict)),
            _result(),
            _result(),
            _result(stdout=_codex_payload()),
        ]
    )

    assert install_mcp.install_codex(force=True, runner=runner, candidates=["codex"]) is True
    assert "remove" in runner.calls[1]
    assert "add" in runner.calls[2]


def test_codex_print_mode_does_not_mutate_configuration(monkeypatch, capsys):
    monkeypatch.setattr(install_mcp, "server_entry", lambda wants_type: dict(ENTRY))

    assert install_mcp.main(["codex", "--print"]) == 0
    assert tomllib.loads(capsys.readouterr().out)["mcp_servers"]["stm32-gdb-mcp"]["command"] == ENTRY["command"]
