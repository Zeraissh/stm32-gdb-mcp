"""Per-channel write locks for the hub daemon.

One rack-wide lock made every write wait for every other: a 2 s power cycle on
channel 1 held up a data-line switch on channel 4 that has nothing to do with
it. Here a write locks only the channels it touches; ``ALL`` (exclusive
isolation, a port change, a discovery lease) conflicts with everything.

Ordering is FIFO among CONFLICTING requests: a waiter proceeds only when no
holder and no EARLIER waiter overlaps it. So a whole-rack request queued behind
a channel-1 cycle is not starved by a stream of later single-channel writes,
while writes that overlap nothing ahead of them go straight through.

Locks are owned by tokens, not threads: a discovery lease is taken by one RPC
and released by another, on different HTTP handler threads.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable
from typing import Any

ALL = None  # "every channel": conflicts with any request


def _overlap(a: frozenset[int] | None, b: frozenset[int] | None) -> bool:
    return a is None or b is None or bool(a & b)


class ChannelLocks:
    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._held: dict[object, tuple[frozenset[int] | None, dict]] = {}
        self._waiting: list[tuple[object, frozenset[int] | None]] = []

    def acquire(self, channels: Iterable[int] | None, info: dict, timeout: float) -> object | None:
        """Lock ``channels`` (``ALL`` for every one); a token, or None on timeout."""
        wanted = None if channels is None else frozenset(channels)
        ticket = object()
        deadline = time.monotonic() + max(timeout, 0.0)
        with self._cond:
            self._waiting.append((ticket, wanted))
            try:
                while True:
                    ahead = self._waiting[:self._waiting.index((ticket, wanted))]
                    blocked = any(_overlap(wanted, held) for held, _info in self._held.values()) or \
                        any(_overlap(wanted, queued) for _t, queued in ahead)
                    if not blocked:
                        self._held[ticket] = (wanted, dict(info, _t0=time.monotonic()))
                        return ticket
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    self._cond.wait(remaining)
            finally:
                self._waiting.remove((ticket, wanted))
                # Leaving the queue (either way) can unblock whoever was behind us.
                self._cond.notify_all()

    def release(self, token: object | None) -> None:
        if token is None:
            return
        with self._cond:
            self._held.pop(token, None)
            self._cond.notify_all()

    def holders(self, channels: Iterable[int] | None = ALL) -> list[dict]:
        """What currently holds any of ``channels``, oldest first, with run time."""
        wanted = None if channels is None else frozenset(channels)
        now = time.monotonic()
        with self._cond:
            held = list(self._held.values())
        out = []
        for chans, info in sorted(held, key=lambda item: item[1]["_t0"]):
            if not _overlap(wanted, chans):
                continue
            row: dict[str, Any] = {k: v for k, v in info.items() if not k.startswith("_")}
            row["channels"] = "all" if chans is None else sorted(chans)
            row["running_sec"] = round(now - info["_t0"], 1)
            out.append(row)
        return out

    def waiting(self) -> int:
        with self._cond:
            return len(self._waiting)
