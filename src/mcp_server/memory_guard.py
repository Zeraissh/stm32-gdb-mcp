"""Guardrails and audit trail for memory writes.

Letting the AI write target memory unsupervised is the line between "safe to
watch" and "safe to let go". This guard blocks writes into regions that can
brick a board or silently change debug behavior (option bytes/system memory,
the independent and window watchdog key registers) unless explicitly allowed,
supports a ``dry_run`` mode that simulates every write, and keeps an
append-only audit log of every mutating action.

Addresses below are STM32 Cortex-M common defaults; ``set_policy`` lets a
project widen or narrow them.
"""

DEFAULT_PROTECTED_REGIONS = [
    {"name": "option_bytes_system_memory", "start": 0x1FFF0000, "end": 0x1FFFFFFF},
    {"name": "iwdg", "start": 0x40003000, "end": 0x400033FF},
    {"name": "wwdg", "start": 0x40002C00, "end": 0x40002FFF},
]


def _overlaps(region, start, end):
    return start <= region["end"] and end >= region["start"]


class MemoryWriteGuard:
    def __init__(self):
        self.mode = "enforce"  # "enforce" | "dry_run"
        self.protected = [dict(r) for r in DEFAULT_PROTECTED_REGIONS]
        self.allowed = []
        self.audit_log = []

    def set_policy(self, mode=None, add_allow=None, add_protected=None):
        if mode is not None:
            if mode not in ("enforce", "dry_run"):
                raise ValueError("mode must be 'enforce' or 'dry_run'")
            self.mode = mode
        if add_allow:
            self.allowed.extend(dict(r) for r in add_allow)
        if add_protected:
            self.protected.extend(dict(r) for r in add_protected)
        return self.policy()

    def policy(self):
        return {"mode": self.mode, "protected": self.protected, "allowed": self.allowed}

    def _match(self, regions, start, end):
        for region in regions:
            if _overlaps(region, start, end):
                return region
        return None

    def evaluate(self, address: int, width_bits: int = 32) -> dict:
        return self._decide(address, address + (width_bits // 8) - 1)

    def evaluate_range(self, address: int, length: int) -> dict:
        """Same decision as ``evaluate``, for a byte range rather than one write.

        Flash erase works on ranges, and an erase that clips a protected region
        is at least as destructive as a write into it (issue #42).
        """
        if length < 1:
            raise ValueError("length must be >= 1")
        return self._decide(address, address + length - 1)

    def _decide(self, start: int, end: int) -> dict:
        if self.mode == "dry_run":
            return {"action": "simulated", "reason": "dry_run mode is active", "region": None}

        # An allow only wins when it CONTAINS the whole span. Merely touching one
        # was safe while every caller was a 1-4 byte write, which cannot straddle
        # two regions; a flash-erase range can, and overlap semantics would let a
        # range that clips an allowed region unlock a protected region inside the
        # very same span.
        allow = self._match(self.allowed, start, end)
        if allow and allow["start"] <= start and end <= allow["end"]:
            return {"action": "write", "reason": "explicitly allowed", "region": allow}

        protected = self._match(self.protected, start, end)
        if protected:
            return {
                "action": "blocked",
                "reason": f"address is in protected region '{protected['name']}'",
                "region": protected,
            }

        return {"action": "write", "reason": "not in any protected region", "region": None}

    def audit(self, tool: str, address: str, value, decision: dict) -> dict:
        entry = {
            "tool": tool,
            "address": address,
            "value": value,
            "action": decision["action"],
            "reason": decision["reason"],
        }
        self.audit_log.append(entry)
        return entry

    def get_audit_log(self, limit: int | None = None):
        if limit is None:
            return list(self.audit_log)
        return self.audit_log[-limit:]
