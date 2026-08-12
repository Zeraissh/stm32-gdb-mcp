"""GDB's own permission switches deny target writes by default.

Every read-only tool forwards a caller-supplied C expression to
-data-evaluate-expression, and a C expression can assign or call a function. Lexical
filtering loses that fight -- verified against arm-none-eabi-gdb 15.2.90, both
``sizeof(1) + (g = 888)`` and ``x/2i main + 0*(g = 1010)`` land the assignment.

GDB's switches do not lose: measured against an RSP stub, with them off the wire log
for those same expressions contains only read packets. The refusal happens inside
GDB's target layer, before a packet exists.
"""
import pytest
from conftest import FakeGdb

import mcp_server.gdb_client as gdb_client_module
from mcp_server.gdb_client import GdbClientManager, TargetWriteLockdownError


def _started(monkeypatch, gdb=None):
    fake = gdb if gdb is not None else FakeGdb()
    monkeypatch.setattr(gdb_client_module, "GdbController", lambda command: fake)
    client = GdbClientManager()
    client.start_gdb()
    return client, fake


def test_starting_gdb_denies_target_writes_registers_and_inferior_calls(monkeypatch):
    client, fake = _started(monkeypatch)

    assert fake.permissions == {
        "may-write-memory": "off",
        "may-write-registers": "off",
        "may-call-functions": "off",
    }
    assert client._write_window_depth == 0


def test_a_gdb_that_cannot_honour_the_lockdown_refuses_to_start(monkeypatch):
    # Fail-closed. A GDB that ignores these leaves every read-only tool able to write
    # target memory while the server believes it is guarded -- strictly worse than
    # not starting. Contrast mi-async and charset, which degrade on purpose.
    class Lying(FakeGdb):
        def write(self, command, timeout_sec=1.0):
            super().write(command, timeout_sec)
            if command.startswith("-gdb-show may-"):
                return [{"type": "result", "message": "done", "payload": {"value": "on"}}]
            return [{"type": "result", "message": "done", "payload": None}]

        def exit(self):
            self.exited = True

    with pytest.raises(TargetWriteLockdownError, match="NOT in force"):
        _started(monkeypatch, Lying())


def test_a_gdb_that_rejects_the_setting_outright_refuses_to_start(monkeypatch):
    class Refusing(FakeGdb):
        def write(self, command, timeout_sec=1.0):
            super().write(command, timeout_sec)
            if command.startswith("-gdb-set may-"):
                raise RuntimeError("Undefined command")
            return [{"type": "result", "message": "done", "payload": None}]

        def exit(self):
            self.exited = True

    with pytest.raises(TargetWriteLockdownError, match="Refusing to run"):
        _started(monkeypatch, Refusing())


def test_a_genuine_write_lifts_the_guard_and_puts_it_back(monkeypatch):
    client, fake = _started(monkeypatch)

    client.write_typed_memory("0x20000000", "0x1", width_bits=32)

    names = [c[0] for c in fake.commands]
    assert "-gdb-set may-write-memory on" in names
    assert names[-1] == "-gdb-set may-write-registers off"
    assert fake.permissions["may-write-memory"] == "off", "the window must close"
    assert client._write_window_depth == 0


def test_the_guard_goes_back_even_when_the_write_raises(monkeypatch):
    class Exploding(FakeGdb):
        def write(self, command, timeout_sec=1.0):
            if command.startswith("-data-write-memory-bytes"):
                super().write(command, timeout_sec)
                raise RuntimeError("target refused")
            return super().write(command, timeout_sec)

    client, fake = _started(monkeypatch, Exploding())

    with pytest.raises(RuntimeError, match="target refused"):
        client.write_typed_memory("0x20000000", "0x1", width_bits=32)

    assert fake.permissions["may-write-memory"] == "off"
    assert client._write_window_depth == 0


def test_a_failed_restore_never_masks_the_bodys_own_exception(monkeypatch):
    # An exception raised in `finally` REPLACES the body's, which would hide the real
    # failure behind a plumbing error. The session is torn down instead.
    class RestoreBreaks(FakeGdb):
        def __init__(self):
            super().__init__()
            self.exited = False
            self._body_failed = False

        def write(self, command, timeout_sec=1.0):
            if command.startswith("-data-write-memory-bytes"):
                super().write(command, timeout_sec)
                self._body_failed = True
                raise RuntimeError("the real failure")
            if self._body_failed and command == "-gdb-set may-write-memory off":
                super().write(command, timeout_sec)
                raise RuntimeError("pipe died")
            return super().write(command, timeout_sec)

        def exit(self):
            self.exited = True

    client, fake = _started(monkeypatch, RestoreBreaks())

    with pytest.raises(RuntimeError, match="the real failure"):
        client.write_typed_memory("0x20000000", "0x1", width_bits=32)

    # A session that cannot be re-locked is torn down: dead is safe, silently
    # unlocked is the vulnerability back.
    assert fake.exited is True
    assert client.gdb is None


def test_flashing_lifts_register_writes_too(monkeypatch):
    # After the last vFlashWrite GDB sets the PC to the ELF entry point with a P
    # packet, so with may-write-registers off a real flash completes and STILL
    # reports "Writing to registers is not allowed (regno 15)" -- a false failure.
    client, fake = _started(monkeypatch)

    client.load_firmware("fw.elf")

    names = [c[0] for c in fake.commands]
    assert "-gdb-set may-write-registers on" in names
    lift = names.index("-gdb-set may-write-registers on")
    download = names.index("-target-download")
    assert lift < download, "the lift must land BEFORE any erase reaches the target"
    assert fake.permissions["may-write-registers"] == "off"


def test_windows_nest_without_reopening_the_guard(monkeypatch):
    client, fake = _started(monkeypatch)

    with client.write_window("outer"):
        with client.write_window("inner"):
            assert fake.permissions["may-write-memory"] == "on"
        assert fake.permissions["may-write-memory"] == "on", "inner exit must not close it"

    assert fake.permissions["may-write-memory"] == "off"
    assert client._write_window_depth == 0


def test_may_call_functions_has_no_lift_path(monkeypatch):
    # An inferior call RESUMES the core to run target code. Nothing in this server
    # needs one, so the window never re-enables it -- not even for a flash.
    client, fake = _started(monkeypatch)

    with client.write_window("anything", registers=True):
        assert fake.permissions["may-call-functions"] == "off"

    assert "-gdb-set may-call-functions on" not in [c[0] for c in fake.commands]


def test_the_tracker_holds_the_gdb_lock_while_it_evaluates(monkeypatch):
    # The switches are GDB-process-global, so an unlocked poller would evaluate its
    # caller-supplied expression during someone else's write window.
    import inspect

    from mcp_server.tracker import VariableTracker

    source = inspect.getsource(VariableTracker._track_loop)
    assert "gdb_lock()" in source
    assert source.index("gdb_lock()") < source.index("read_variable")
