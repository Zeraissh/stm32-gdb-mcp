"""Per-dispatch context handed to registered tool handlers.

server.py builds a fresh ToolContext from its module globals on every dispatch,
so tests that monkeypatch ``mcp_server.server`` attributes (gdb_client,
detect_probe, _dispatch_tool, ...) keep working exactly as before the
decomposition. Domain modules must never import server.py — everything they
need arrives through this object.
"""

from __future__ import annotations

import logging
import types
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from mcp.types import TextContent


@dataclass
class ToolContext:
    # Per-session object bundle (mirrors the locals the inline dispatch chain bound).
    session_id: str
    sess: Any  # the raw session namespace (`.serial`, `.gdb_port`, ...)
    gdb_manager: Any
    gdb_client: Any
    svd_parser: Any
    variable_tracker: Any
    debug_profile: Any
    freertos_inspector: Any
    memory_guard: Any
    rtt_log_reader: Any
    swo_log_reader: Any
    swo_file_reader: Any
    uart_log_reader: Any
    last_session: dict
    board: dict
    acceptance: dict
    loop: dict
    design: dict
    spec: dict

    # Server singletons.
    session_journal: Any
    session_manager: Any
    reported_issues: dict
    tool_catalog: Callable[[], dict]  # live view of the advertised-tool catalog
    logger: logging.Logger

    # Free functions that tests monkeypatch on mcp_server.server — always call
    # through here (ctx.fns.detect_probe()), never import them in a domain module.
    fns: types.SimpleNamespace

    # Re-enter the dispatch loop (MERGED forwarding, composites, run_pipeline).
    dispatch: Callable[[str, dict | None], list[TextContent]]

    # The default session bundle regardless of ctx.session_id (list/close_session).
    default_session: Callable[[], Any]
