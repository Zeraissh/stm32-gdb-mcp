"""Helpers shared between server.py's inline handlers and domain tool modules.

Never import server.py from here — callers pass session objects in explicitly.
"""

import logging
from typing import Any

from ..reliability import retry_call

_logger = logging.getLogger(__name__)


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
    """Load symbols from the profile's elf_path after a connect, if configured.

    After loading, runs a non-throwing compare-sections to check whether the ELF
    matches the target flash. A mismatch is logged at WARNING level so that
    downstream agents can see the discrepancy even when there is no MCP response
    carrier (this path is called from session setup, not from a tool handler).
    """
    elf_path = sess.debug_profile.get().get("elf_path")
    if not elf_path:
        return False
    try:
        sess.gdb_client.load_symbols(elf_path)
    except Exception:
        return False
    # Non-throwing consistency check: log a warning if symbols don't match flash.
    try:
        report = sess.gdb_client.compare_sections_report()
        if report["checked"]:
            if report["mismatched"]:
                _logger.warning(
                    "Auto-loaded symbols from %s do NOT match target flash — %d section(s) mismatched: %s. "
                    "Values read through these symbols will be meaningless. Re-flash or use the matching ELF.",
                    elf_path,
                    len(report["mismatched"]),
                    "; ".join(report["mismatched"]),
                )
        else:
            _logger.info(
                "Could not verify symbol-flash match for %s: %s", elf_path, report.get("reason", "unknown")
            )
    except Exception:
        # compare_sections_report is designed not to raise, but guard anyway.
        _logger.debug("compare_sections_report failed unexpectedly", exc_info=True)
    return True


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
