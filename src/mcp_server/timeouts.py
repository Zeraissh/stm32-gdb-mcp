"""Centralized, overridable timeouts.

Phase 2 priority #1 (determinism) + #2 (reliability): one named place for every
GDB operation timeout, instead of magic numbers scattered across call sites. The
agent can widen them once for a slow/flaky probe, and because ``set_timeouts`` is
a journaled tool call, a replayed session uses the same values — deterministically.
"""

DEFAULTS = {
    "default": 1.0,
    "connect": 5.0,
    "reset": 5.0,
    "halt": 5.0,
    "memory": 2.0,
    "registers": 2.0,
    "source": 2.0,
    "run": 10.0,
    "download": 60.0,
}


class TimeoutConfig:
    def __init__(self):
        self.values = dict(DEFAULTS)

    def get(self, name: str) -> float:
        return self.values.get(name, self.values["default"])

    def set(self, overrides: dict) -> dict:
        validated = {}
        for name, value in (overrides or {}).items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"timeout '{name}' must be a positive number, got {value!r}")
            validated[name] = float(value)
        self.values.update(validated)
        return self.as_dict()

    def as_dict(self) -> dict:
        return dict(self.values)
