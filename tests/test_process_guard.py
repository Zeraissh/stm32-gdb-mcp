"""The guard that keeps OpenOCD/GDB from outliving the MCP server.

The OS-level half (a killed parent reaping its children) cannot be asserted from
inside this process, so it is verified out-of-process; see the module docstring of
process_guard and the PR that added it. What is covered here is everything that
can go wrong in Python: the handle plumbing reporting failure, the shutdown hooks
running once, and signal handlers chaining rather than swallowing.
"""

import signal
import sys

import pytest

from mcp_server import process_guard


def test_install_reports_a_working_mechanism_on_this_platform():
    result = process_guard.install()

    assert result, "install must always report what it achieved"
    if sys.platform == "win32":
        # A silent "unavailable" here means every killed server leaks a GDB
        # server holding the probe -- the failure this module exists to prevent.
        assert result.startswith("windows job object"), result


def test_install_is_idempotent():
    assert process_guard.install() == process_guard.install()


def test_child_preexec_is_none_off_linux():
    hook = process_guard.child_preexec()
    if sys.platform.startswith("linux"):
        assert callable(hook)
    else:
        # subprocess rejects preexec_fn on Windows, so it must be None there.
        assert hook is None


def test_shutdown_hooks_run_once_even_if_triggered_twice(monkeypatch):
    calls = []
    monkeypatch.setattr(process_guard, "_shutdown_hooks", [])
    monkeypatch.setattr(process_guard, "_shutdown_done", False)
    monkeypatch.setattr(process_guard, "atexit", _NoopAtexit())
    monkeypatch.setattr(process_guard.signal, "signal", lambda *a: None)

    process_guard.register_shutdown(lambda: calls.append("a"))
    process_guard._run_shutdown()
    process_guard._run_shutdown()

    assert calls == ["a"], "teardown must not run twice"


def test_a_failing_hook_does_not_stop_the_others(monkeypatch):
    calls = []
    monkeypatch.setattr(process_guard, "_shutdown_hooks", [])
    monkeypatch.setattr(process_guard, "_shutdown_done", False)
    monkeypatch.setattr(process_guard, "atexit", _NoopAtexit())
    monkeypatch.setattr(process_guard.signal, "signal", lambda *a: None)

    process_guard.register_shutdown(_raise)
    process_guard._shutdown_hooks.append(lambda: calls.append("second"))
    process_guard._run_shutdown()

    assert calls == ["second"], "one broken hook must not strand the rest"


@pytest.mark.skipif(not hasattr(signal, "SIGTERM"), reason="no SIGTERM on this platform")
def test_registering_shutdown_installs_a_signal_handler(monkeypatch):
    installed = {}
    monkeypatch.setattr(process_guard, "_shutdown_hooks", [])
    monkeypatch.setattr(process_guard, "_shutdown_done", False)
    monkeypatch.setattr(process_guard, "atexit", _NoopAtexit())
    monkeypatch.setattr(process_guard.signal, "signal",
                        lambda sig, handler: installed.setdefault(sig, handler))

    process_guard.register_shutdown(lambda: None)

    assert signal.SIGTERM in installed
    assert callable(installed[signal.SIGTERM])


class _NoopAtexit:
    def register(self, func):
        return func


def _raise():
    raise RuntimeError("hook exploded")
