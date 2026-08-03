"""Helpers shared between server.py's inline handlers and domain tool modules.

Never import server.py from here — callers pass session objects in explicitly.
"""

from typing import Any

from ..reliability import retry_call


def core_state(gdb_client: Any) -> str | None:
    """"running"/"halted" from the state GDB already reported, or None if unknown.

    Costs no target traffic: GdbClientManager tracks this from GDB's own
    *running/*stopped async records. Reads that carry it let an agent notice a
    stopped core immediately, instead of inferring it hours later from some
    unrelated instrument going quiet (issue #33).
    """
    getter = getattr(gdb_client, "is_running", None)
    if getter is None:
        return None
    try:
        running = getter()
    except Exception:
        return None
    if running is None:
        return None
    return "running" if running else "halted"


def autoload_symbols(sess: Any) -> bool:
    """Load symbols from the profile's elf_path after a connect, if configured."""
    elf_path = sess.debug_profile.get().get("elf_path")
    if not elf_path:
        return False
    try:
        sess.gdb_client.load_symbols(elf_path)
        return True
    except Exception:
        return False


def recover_current_session(gdb_client: Any, gdb_manager: Any, last_session: dict, sess: Any) -> dict:
    if not last_session.get("server_type"):
        raise RuntimeError("No prior session to recover; call start_debug_session first.")
    for teardown in (gdb_client.stop_gdb, gdb_manager.stop):
        try:
            teardown()
        except Exception:
            pass

    port = retry_call(
        lambda: gdb_manager.start(last_session["server_type"], last_session["server_args"]),
        attempts=3,
        backoff_base=0.8,
    )
    gdb_client.start_gdb()
    resp = gdb_client.connect("localhost", port)
    symbols = autoload_symbols(sess)
    return {
        "message": "Session recovered",
        "server_type": last_session["server_type"],
        "port": port,
        "symbols_loaded": symbols,
        "raw_response": resp,
    }
