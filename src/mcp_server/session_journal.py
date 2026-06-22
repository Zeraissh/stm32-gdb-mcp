"""Append-only journal of every tool call in a debug session.

Phase 2 priority #1 (determinism & reproducibility): every action is recorded
with a monotonic sequence number, timestamp, arguments, outcome, and a one-line
summary, under a stable run-id. This is the backbone for reproducing a bug, for
exporting a shareable debug report, and for the observability timeline.
"""

import itertools
import time
import uuid


class SessionJournal:
    def __init__(self):
        self.run_id = uuid.uuid4().hex[:12]
        self._seq = itertools.count(1)
        self.entries = []

    def record(self, tool, arguments, ok, summary=None, error=None, duration_ms=None):
        entry = {
            "seq": next(self._seq),
            "ts": time.time(),
            "run_id": self.run_id,
            "tool": tool,
            "arguments": arguments,
            "ok": bool(ok),
            "summary": summary,
            "error": error,
            "duration_ms": duration_ms,
        }
        self.entries.append(entry)
        return entry

    def get(self, limit=None):
        if limit is None:
            return list(self.entries)
        return self.entries[-limit:]

    def timeline(self):
        lines = []
        for e in self.entries:
            status = "ok" if e["ok"] else f"ERR:{e['error']}"
            detail = e["summary"] or ""
            lines.append(f"#{e['seq']:03d} {e['tool']} -> {status} {detail}".rstrip())
        return lines

    def clear(self):
        self.entries = []
