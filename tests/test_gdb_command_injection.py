"""A caller string must never be able to smuggle a second GDB command.

pygdbmi writes the command verbatim plus one trailing newline, so an embedded
newline splits one command into two and GDB executes the remainder as a fresh CLI
line. GDB's CLI has ``shell``, so that is host command execution -- reachable
through tools annotated readOnlyHint=True, which call_read forwards without an
approval prompt.

Verified against arm-none-eabi-gdb 15.2.90 before this guard existed: feeding it
``list main\\nshell echo pwned > <file>`` ran both commands and created the file.
"""
import pytest

from mcp_server.gdb_client import GdbClientManager

PAYLOAD = 'main\nshell echo pwned'


class RecordingGdb:
    """Stands in for pygdbmi's IoManager and records anything that reaches the pipe."""

    def __init__(self):
        self.writes = []

    def write(self, cmd, timeout_sec=None, raise_error_on_timeout=True):
        self.writes.append(cmd)
        return []


@pytest.fixture
def client():
    manager = GdbClientManager()
    manager.gdb = RecordingGdb()
    return manager


@pytest.mark.parametrize("payload", [
    "main\nshell echo pwned",
    "main\r\nshell echo pwned",
    "main\rshell echo pwned",
    "main\x00shell echo pwned",
])
def test_execute_command_refuses_every_control_character_that_splits_a_command(client, payload):
    with pytest.raises(ValueError, match="may not contain a newline or NUL"):
        client.execute_command(f"list {payload}")

    assert client.gdb.writes == [], "nothing may reach the pipe once the guard fires"


def test_execute_cli_command_is_covered_by_the_same_guard(client):
    # It forwards to execute_command; this pins that it keeps doing so rather than
    # growing its own write path.
    with pytest.raises(ValueError, match="may not contain a newline or NUL"):
        client.execute_cli_command(f"list {PAYLOAD}")

    assert client.gdb.writes == []


def test_quoting_does_not_launder_a_newline(client):
    # gdb_expr escapes backslash and quote for argv splitting; a raw newline passes
    # straight through it, which is why the guard cannot live in the quoting helper.
    from mcp_server.gdb_client import gdb_expr

    quoted = gdb_expr(PAYLOAD)
    assert "\n" in quoted, "gdb_expr does not neutralise newlines -- it is not a guard"

    with pytest.raises(ValueError):
        client.execute_command(f"-data-evaluate-expression {quoted}")


@pytest.mark.parametrize("method,args", [
    ("read_variable", (PAYLOAD,)),
    ("read_memory", (PAYLOAD, 4)),
    ("read_typed_memory", (PAYLOAD, 32)),
])
def test_caller_strings_cannot_smuggle_a_command_through_the_client(client, method, args):
    # Parametrised over the client methods that interpolate a caller string, so a new
    # one that forgets to go through execute_command shows up as a missing refusal.
    fn = getattr(client, method, None)
    if fn is None:
        pytest.skip(f"{method} not present on this build")

    with pytest.raises(ValueError, match="may not contain a newline or NUL"):
        fn(*args)

    assert client.gdb.writes == []


def test_ordinary_commands_still_go_through(client):
    # The guard must not become a formatting rule: everything legitimate still passes.
    client.execute_command("-data-evaluate-expression \"*(unsigned long *)0x08006000\"")
    client.execute_command("info functions ^HAL_")

    assert len(client.gdb.writes) == 2
