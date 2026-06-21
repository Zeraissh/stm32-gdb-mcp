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
        start = address
        end = address + (width_bits // 8) - 1

        if self.mode == "dry_run":
            return {"action": "simulated", "reason": "dry_run mode is active", "region": None}

        allow = self._match(self.allowed, start, end)
        if allow:
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
